"""No provider/network/DB access: all requests use httpx.MockTransport."""
import base64
import copy
import json
import traceback
from unittest.mock import Mock

import httpx
import pytest

from buenapro_worker.prod2 import luna_transport as subject


def response(*, status="completed", text='{"ok":true}', usage=True):
    return {"id": "resp_synthetic", "status": status,
            "output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "role": "assistant", "status": status,
                        "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 500,
                      "input_tokens_details": {"cached_tokens": 200},
                      "output_tokens_details": {"reasoning_tokens": 300},
                      "total_tokens": 1500} if usage else None}


def generate(client, **kwargs):
    return client.generate(instructions="Read every input and return one JSON object.",
                           contents=[{"type": "input_text", "text": "All document text."}],
                           **kwargs)


def test_complete_request_preserves_full_input_and_uses_supported_luna_parameters():
    requests = []
    parts = [{"type": "input_text", "text": "FRAGMENTO 1/2 offset=0"},
             subject.luna_pdf_part(b"%PDF-full first fragment", "part-0001.pdf"),
             {"type": "input_text", "text": "FRAGMENTO 2/2 offset=100"},
             subject.luna_pdf_part(b"%PDF-full second fragment", "part-0002.pdf")]
    original = copy.deepcopy(parts)
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response())
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = subject.LunaResponsesClient("synthetic-key", client=http)
        raw = client.generate(instructions="Return JSON using this unchanged schema.", contents=parts)
    assert raw == response()
    assert len(requests) == 1 and str(requests[0].url) == subject.RESPONSES_URL
    assert requests[0].method == "POST"
    assert requests[0].headers["authorization"] == "Bearer synthetic-key"
    payload = json.loads(requests[0].content)
    assert payload == {"model": "gpt-6-luna", "instructions": "Return JSON using this unchanged schema.",
                       "input": [{"role": "user", "content": original}],
                       "reasoning": {"effort": "low"}, "store": False,
                       "service_tier": "default", "max_output_tokens": 65536,
                       "text": {"format": {"type": "json_object"}}}
    assert parts == original
    assert requests[0].extensions["timeout"] == {"connect": 10, "read": 300, "write": 60, "pool": 10}
    for unsupported in ("temperature", "top_p", "tools", "background", "stream", "previous_response_id"):
        assert unsupported not in payload


def test_output_budget_is_explicitly_configurable():
    received = []
    def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json=response())
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        generate(subject.LunaResponsesClient("synthetic", client=http), max_output_tokens=128000)
    assert received[0]["max_output_tokens"] == 128000


@pytest.mark.parametrize("kwargs", [{"max_output_tokens": 0}, {"max_output_tokens": True},
                                    {"max_output_tokens": 128001}, {"max_output_tokens": 2.0},
                                    {"model": "gpt-6-astra"}, {"on_response": 1}])
def test_invalid_generation_limits_or_model_never_dispatch(kwargs):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Local rejection required"))) as http:
        with pytest.raises(subject.LunaTransportError) as caught:
            generate(subject.LunaResponsesClient("synthetic", client=http), **kwargs)
    assert caught.value.code == "invalid_request" and not caught.value.request_dispatched


@pytest.mark.parametrize("api_key", [None, "", " key", "key\n", "key with spaces", "keyé"])
def test_invalid_credential_is_rejected_without_echoing_it(api_key):
    with pytest.raises(subject.LunaTransportError) as caught:
        subject.LunaResponsesClient(api_key)
    assert str(caught.value) == "Luna transport failed (invalid_api_key)"


def test_owned_http_client_explicitly_disables_retries_and_environment_proxy(monkeypatch):
    transport_factory, client_factory = Mock(), Mock()
    monkeypatch.setattr(subject.httpx, "HTTPTransport", transport_factory)
    monkeypatch.setattr(subject.httpx, "Client", client_factory)
    with subject.LunaResponsesClient("synthetic"):
        pass
    transport_factory.assert_called_once_with(retries=0)
    client_factory.assert_called_once_with(timeout=subject.REQUEST_TIMEOUT, follow_redirects=False,
        trust_env=False, transport=transport_factory.return_value)
    client_factory.return_value.close.assert_called_once()


def test_injected_client_is_not_closed_and_closed_wrapper_cannot_generate():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as http:
        wrapper = subject.LunaResponsesClient("synthetic", client=http)
        wrapper.close()
        wrapper.close()
        assert not http.is_closed
        with pytest.raises(subject.LunaTransportError, match="client_closed"):
            generate(wrapper)


def test_audit_receives_raw_http_body_before_json_parsing():
    audits = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not JSON"))) as http:
        with pytest.raises(subject.LunaTransportError) as caught:
            generate(subject.LunaResponsesClient("synthetic", client=http),
                     on_response=lambda item: audits.append((item.status_code, item.content)))
    assert audits == [(200, b"not JSON")]
    assert caught.value.code == "invalid_response_json"
    assert caught.value.status_code == 200 and caught.value.response_received


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500, 503])
def test_http_status_audited_once_with_safe_error_and_without_redirect_or_retry(status):
    requests, audits = [], []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="PRIVATE BODY synthetic-key",
                              headers={"location": "https://private.invalid/credential"})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        with pytest.raises(subject.LunaTransportError) as caught:
            generate(subject.LunaResponsesClient("synthetic-key", client=http),
                     on_response=lambda item: audits.append(item.content))
    assert len(requests) == len(audits) == 1
    assert caught.value.code == "http_status" and caught.value.status_code == status
    assert caught.value.request_dispatched and caught.value.response_received
    assert "PRIVATE" not in str(caught.value) and "synthetic-key" not in str(caught.value)


def test_raising_response_hook_preserves_known_http_status_and_body():
    audits = []
    def check(item):
        item.raise_for_status()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, text="body")),
                      event_hooks={"response": [check]}) as http:
        with pytest.raises(subject.LunaTransportError) as caught:
            generate(subject.LunaResponsesClient("synthetic", client=http),
                     on_response=lambda item: audits.append(item.content))
    assert caught.value.status_code == 429 and audits == [b"body"]


@pytest.mark.parametrize("error_type,code", [(httpx.ConnectTimeout, "http_timeout"),
    (httpx.ReadTimeout, "http_timeout"), (httpx.WriteTimeout, "http_timeout"),
    (httpx.ConnectError, "http_transport_error"), (httpx.ReadError, "http_transport_error")])
def test_transport_failures_remain_uncertain_and_do_not_leak_credentials_or_retry(error_type, code):
    requests, audits = [], []
    def fail(request):
        requests.append(request)
        raise error_type("PRIVATE URL https://secret.invalid/key=synthetic-key", request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        with pytest.raises(subject.LunaTransportError) as caught:
            generate(subject.LunaResponsesClient("synthetic-key", client=http), on_response=audits.append)
    assert len(requests) == 1 and audits == []
    assert caught.value.code == code and caught.value.request_dispatched
    assert caught.value.status_code is None and not caught.value.response_received
    rendered = "".join(traceback.format_exception(caught.value))
    assert "PRIVATE URL" not in rendered and "secret.invalid" not in rendered
    assert "key=synthetic-key" not in rendered


def test_failed_audit_does_not_parse_or_retry_and_has_safe_status():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="malformed")
    def broken_audit(_):
        raise RuntimeError("PRIVATE credential")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(subject.LunaTransportError) as caught:
            generate(subject.LunaResponsesClient("synthetic", client=http), on_response=broken_audit)
    assert len(requests) == 1 and caught.value.code == "response_audit_failed"
    assert caught.value.status_code == 200 and caught.value.response_received
    assert "PRIVATE credential" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("body", [[], None, "secret", 10])
def test_non_object_http_json_is_audited_then_rejected(body):
    audits = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(body)))) as http:
        with pytest.raises(subject.LunaTransportError, match="invalid_response_json"):
            generate(subject.LunaResponsesClient("synthetic", client=http),
                     on_response=lambda item: audits.append(item.content))
    assert len(audits) == 1


def test_completed_parser_uses_full_text_and_ignores_reasoning_item():
    raw = response()
    raw["output"][1]["content"] = [{"type": "output_text", "text": '{"first":'},
                                   {"type": "output_text", "text": '1,"last":2}'}]
    parsed = subject.parse_luna_response(raw)
    assert parsed.text == '{"first":1,"last":2}' and parsed.completed and parsed.usage_known
    assert not parsed.truncated and parsed.refusal is None
    assert parsed.usage == subject.LunaUsage(1000, 500, 200, 300, 1500)
    assert parsed.usage.cost_usd == pytest.approx(.000332)
    assert "first" not in repr(parsed)


def test_truncation_preserves_partial_output_and_known_usage():
    raw = response(status="incomplete", text='{"partial":')
    raw["incomplete_details"] = {"reason": "max_output_tokens"}
    parsed = subject.parse_luna_response(raw)
    assert parsed.truncated and not parsed.completed and parsed.usage_known
    assert parsed.text == '{"partial":' and parsed.usage.output_tokens == 500
    assert subject.parse_luna_usage(raw).cost_usd == pytest.approx(.000332)


def test_refusal_retains_usage_and_is_not_completion():
    raw = response()
    raw["output"][1]["content"] = [{"type": "refusal", "refusal": "Private refusal explanation"}]
    parsed = subject.parse_luna_response(raw)
    assert parsed.refusal == "Private refusal explanation" and parsed.text == ""
    assert not parsed.completed and not parsed.truncated and parsed.usage_known
    assert "Private refusal" not in repr(parsed)


def test_content_filter_incomplete_is_not_output_limit_truncation():
    raw = response(status="incomplete", text="")
    raw["incomplete_details"] = {"reason": "content_filter"}
    parsed = subject.parse_luna_response(raw)
    assert not parsed.completed and not parsed.truncated
    assert parsed.incomplete_reason == "content_filter"


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress"])
def test_other_non_complete_statuses_are_explicit_and_preserve_usage(status):
    raw = response(status=status)
    raw["output"] = []
    raw["error"] = {"message": "PRIVATE provider details"}
    parsed = subject.parse_luna_response(raw)
    assert parsed.status == status and not parsed.completed and not parsed.truncated
    assert parsed.usage_known and "PRIVATE" not in repr(parsed)


@pytest.mark.parametrize("edit,code", [
    (lambda raw: raw.update(status="unrecognized"), "invalid_response"),
    (lambda raw: raw.update(status=["completed"]), "invalid_response"),
    (lambda raw: raw.update(output=None), "invalid_response"),
    (lambda raw: raw.update(output=[]), "missing_output_text"),
    (lambda raw: raw.update(incomplete_details="PRIVATE"), "invalid_response"),
    (lambda raw: raw.update(incomplete_details={"reason": "PRIVATE URL"}), "invalid_response"),
    (lambda raw: raw.update(incomplete_details={"reason": ["max_output_tokens"]}), "invalid_response"),
    (lambda raw: raw.update(incomplete_details={"reason": "max_output_tokens"}), "invalid_response"),
    (lambda raw: raw.update(error={"message": "PRIVATE failure"}), "invalid_response"),
    (lambda raw: raw["output"][1].update(type="function_call"), "unexpected_output"),
    (lambda raw: raw["output"][1].update(status="incomplete"), "invalid_response"),
    (lambda raw: raw["output"][1]["content"][0].update(text=None), "invalid_response"),
])
def test_malformed_response_does_not_prevent_independent_usage_read(edit, code):
    raw = response()
    edit(raw)
    assert subject.parse_luna_usage(raw).input_tokens == 1000
    with pytest.raises(subject.LunaTransportError) as caught:
        subject.parse_luna_response(raw)
    assert caught.value.code == code and "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("edit", [
    lambda usage: usage.pop("input_tokens"), lambda usage: usage.update(input_tokens=True),
    lambda usage: usage.update(output_tokens=-1), lambda usage: usage.update(output_tokens=1.2),
    lambda usage: usage.update(input_tokens_details=None),
    lambda usage: usage["input_tokens_details"].update(cached_tokens=1001),
    lambda usage: usage["input_tokens_details"].update(cached_tokens=True),
    lambda usage: usage["output_tokens_details"].update(reasoning_tokens=501),
    lambda usage: usage["output_tokens_details"].update(reasoning_tokens="300"),
    lambda usage: usage.update(total_tokens=1501),
])
def test_invalid_usage_is_unknown_instead_of_zero_or_invented_cost(edit):
    raw = response()
    edit(raw["usage"])
    assert subject.parse_luna_usage(raw) is None
    assert not subject.parse_luna_response(raw).usage_known


def test_absent_usage_is_unknown_but_explicit_zero_tokens_are_known():
    assert subject.parse_luna_usage(response(usage=False)) is None
    raw = response()
    raw["usage"] = {"input_tokens": 0, "output_tokens": 0}
    assert subject.parse_luna_usage(raw) == subject.LunaUsage(0, 0)
    assert subject.parse_luna_usage(raw).cost_usd == 0


@pytest.mark.parametrize("input_tokens,output_tokens,cached,cost", [
    (1000, 500, 0, .00035), (1000, 500, 1000, .00026),
    (272000, 1000, 72000, .02122),
    (272001, 1000, 72000, .0421902),
    (300000, 1000, 300000, .00675),
])
def test_standard_cost_cache_and_full_request_long_context_multipliers(input_tokens, output_tokens, cached, cost):
    assert subject.estimate_luna_cost(input_tokens, output_tokens, cached) == pytest.approx(cost)


@pytest.mark.parametrize("tokens", [(True, 1, 0), (1, True, 0), (1, 1, True),
                                    (-1, 1, 0), (1, -1, 0), (1, 1, -1),
                                    (1.0, 1, 0), (1, 1, 2)])
def test_invalid_cost_counts_rejected(tokens):
    with pytest.raises(subject.LunaTransportError, match="invalid_usage"):
        subject.estimate_luna_cost(*tokens)


def test_reasoning_usage_is_billed_once_in_output():
    usage = subject.parse_luna_usage(response())
    assert usage.output_tokens == 500 and usage.reasoning_tokens == 300
    assert usage.cost_usd == pytest.approx(subject.estimate_luna_cost(1000, 500, 200))
    assert usage.cost_usd != subject.estimate_luna_cost(1000, 800, 200)


def test_pdf_parts_encode_exact_original_and_reject_non_pdf_or_path_names():
    content = b"%PDF-1.7\nfull original document\x00"
    part = subject.luna_pdf_part(content, "document.pdf")
    assert part["detail"] == "high"
    assert base64.b64decode(part["file_data"].removeprefix(subject.PDF_DATA_PREFIX)) == content
    for invalid in (b"Not PDF", bytearray(content)):
        with pytest.raises(subject.LunaTransportError, match="invalid_pdf_input"):
            subject.luna_pdf_part(invalid)
    for name in ("document.docx", "../document.pdf", "C:\\document.pdf", "private\n.pdf"):
        with pytest.raises(subject.LunaTransportError, match="invalid_pdf_input"):
            subject.luna_pdf_part(content, name)


def test_pdf_file_limit_is_decimal_mb_and_exclusive(monkeypatch):
    assert subject.MAX_PDF_BYTES == 50_000_000
    monkeypatch.setattr(subject, "MAX_PDF_BYTES", 20)
    assert subject.luna_pdf_part(b"%PDF-" + b"x" * 14)
    with pytest.raises(subject.LunaTransportError, match="pdf_file_too_large"):
        subject.luna_pdf_part(b"%PDF-" + b"x" * 15)


def test_combined_pdf_request_limit_is_inclusive_and_checked_before_dispatch(monkeypatch):
    monkeypatch.setattr(subject, "MAX_PDF_BYTES", 20)
    first = subject.luna_pdf_part(b"%PDF-xxxxx", "first.pdf")
    second = subject.luna_pdf_part(b"%PDF-yyyyy", "second.pdf")
    assert sum(subject._inline_pdf_size(part["file_data"]) for part in [first, second]) == 20
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response())
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = subject.LunaResponsesClient("synthetic", client=http)
        client.generate(instructions="Return JSON.", contents=[first, second])
        with pytest.raises(subject.LunaTransportError, match="pdf_request_too_large"):
            client.generate(instructions="Return JSON.", contents=[first, second, first])
    assert len(seen) == 1


def test_oversize_base64_is_rejected_before_decoding(monkeypatch):
    monkeypatch.setattr(subject, "MAX_PDF_BYTES", 20)
    monkeypatch.setattr(subject.base64, "b64decode", lambda *_a, **_kw: pytest.fail("No decoding expected"))
    with pytest.raises(subject.LunaTransportError, match="pdf_file_too_large"):
        subject._inline_pdf_size("A" * 100)


def test_raw_base64_and_omitted_detail_are_allowed_without_mutating_caller():
    part = subject.luna_pdf_part(b"%PDF-full", "source.pdf")
    part["file_data"] = part["file_data"].removeprefix(subject.PDF_DATA_PREFIX)
    part.pop("detail")
    copied = copy.deepcopy(part)
    normalized = subject._contents([part])
    assert normalized[0]["detail"] == "high" and part == copied


@pytest.mark.parametrize("part", [
    {"type": "input_file", "file_id": "file-private"},
    {"type": "input_file", "file_url": "https://private.invalid"},
    {"type": "input_file", "filename": "d.pdf", "file_data": "INVALID BASE64"},
    {"type": "input_file", "filename": "d.pdf", "file_data": "data:text/plain;base64,AA=="},
    {"type": "input_file", "filename": "d.pdf", "file_data": None},
    {**subject.luna_pdf_part(b"%PDF-full"), "detail": "low"},
    {**subject.luna_pdf_part(b"%PDF-full"), "file_id": "file-private"},
    {"type": "input_image", "image_url": "https://private.invalid"},
    {"type": "input_text", "text": ""},
    {"type": "input_text", "text": "full", "unexpected": "value"},
])
def test_unsupported_or_malformed_content_never_dispatches(part):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as http:
        with pytest.raises(subject.LunaTransportError) as caught:
            subject.LunaResponsesClient("synthetic", client=http).generate(
                instructions="Return JSON.", contents=[part])
    assert not caught.value.request_dispatched


def test_json_mode_requires_instruction_and_does_not_modify_effective_prompt():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response())
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = subject.LunaResponsesClient("synthetic", client=http)
        with pytest.raises(subject.LunaTransportError, match="json_instruction_required"):
            client.generate(instructions="Read all pages.", contents=[{"type": "input_text", "text": "Full text"}])
        client.generate(instructions="Read all pages.", contents=[{"type": "input_text", "text": "Return JSON."}])
    assert len(seen) == 1 and seen[0]["instructions"] == "Read all pages."
