"""Offline selected Luna contract: complete inputs, one reservation, durable paid output."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from buenapro_worker.extraction.gemini import TruncatedResponseError
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import pdf_preparation, selected_luna as subject
from buenapro_worker.prod2.extraction_policy import QUALIFICATION_FOCUS_POLICY
from buenapro_worker.prod2.input_policy import NATIVE_TEXT_OR_PDF_POLICY, input_snapshot
from buenapro_worker.prod2.luna_transport import LunaTransportError, estimate_luna_cost


def prepared(folder, *, partition=False, context=None):
    writer = PdfWriter()
    for number in range(5 if partition else 1):
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(b"%" + bytes([65 + number]) * 2000 + b"\n")
        page[NameObject("/Contents")] = writer._add_object(stream)
    stream = io.BytesIO()
    writer.write(stream)
    content = stream.getvalue()
    code = "00000000-0000-4000-8000-000000000001"
    path = folder / "source.pdf"
    path.write_bytes(content)
    row = {"candidate_id": "loose:" + code, "document_uuid": code, "source_kind": "loose",
        "filename": "requirements.pdf", "type": "Bases", "stage": "Convocatoria",
        "published_at": "07/10/2026 12:00", "source_url": "https://example.invalid/official/" + code,
        "original_sha256": hashlib.sha256(content).hexdigest()}
    return pdf_preparation.prepare_selected_document_set(folder, [row], {row["candidate_id"]: path},
        {"official_inventory": [row], **(context or {})},
        max_analysis_bytes=5000 if partition else 45_000_000, optimize_lossless=False)


def body(text=None, *, usage=True, status="completed", reason=None, refusal=None):
    content = [{"type": "refusal", "refusal": refusal}] if refusal else [
        {"type": "output_text", "text": text if text is not None else
         json.dumps({"summary": {"descripcion_corta": "Software"}})}]
    return {"id": "resp-offline", "object": "response", "model": "gpt-6-luna", "status": status,
        "instructions": "DO NOT SAVE RAW INSTRUCTIONS",
        "input": [{"type": "input_file", "file_data": "DO NOT SAVE BASE64 INPUT"}],
        "output": [{"id": "msg-offline", "type": "message", "role": "assistant",
                    "status": "completed" if status == "completed" else "incomplete", "content": content}],
        "incomplete_details": {"reason": reason} if reason else None,
        "usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
                  "input_tokens_details": {"cached_tokens": 400},
                  "output_tokens_details": {"reasoning_tokens": 50}} if usage else None}


def settings():
    return SimpleNamespace(openai_api_key="UNIT_ONLY_DO_NOT_SAVE_KEY", prod2_llm1_provider="openai",
                           gemini_model="gemini-3.1-flash-lite")


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    fixture = SimpleNamespace(client=client, response_body=body(), http_status=200)
    def generate(**kwargs):
        response = httpx.Response(fixture.http_status, json=fixture.response_body)
        kwargs["on_response"](response)
        if fixture.http_status != 200:
            raise LunaTransportError("http_status", status_code=fixture.http_status,
                                     request_dispatched=True, response_received=True)
        return fixture.response_body
    client.generate.side_effect = generate
    fixture.constructor = MagicMock(return_value=client)
    monkeypatch.setattr(subject, "LunaResponsesClient", fixture.constructor)
    monkeypatch.setattr(pdf_preparation, "prepare_pdf", lambda path, *args, **kwargs: path)
    return fixture


def test_single_pdf_no_gemini_client_one_reservation_bound_prompt_and_durable_output(sdk, tmp_path, monkeypatch):
    from buenapro_worker.prod2 import selected_extractor
    gemini_constructor = MagicMock(side_effect=AssertionError("Unexpected Gemini initialization"))
    monkeypatch.setattr(selected_extractor.genai, "Client", gemini_constructor)
    bundle = prepared(tmp_path)
    callback = MagicMock()
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path, callback,
                                       request_hash="a" * 64) as extractor:
        result = extractor.extract(bundle["pdf_path"].read_bytes())
        assert result.model == "gpt-6-luna" and result.requires_human_review
        assert (result.input_tokens, result.output_tokens) == (1000, 200)
        assert result.cost_usd == estimate_luna_cost(1000, 200, 400)
        call = sdk.client.generate.call_args.kwargs
        assert call["model"] == "gpt-6-luna" and call["max_output_tokens"] == 65536
        assert call["instructions"] == subject.provider_instruction(extractor.prompts["tdr"],
                                                                    extractor._response_schema("tdr"))
        assert callback.call_count == sdk.client.generate.call_count == 1
        audit = extractor.attempts[0]
        assert audit["request_hash"] == "a" * 64
        assert audit["prompt_hash"] == canonical_hash(call["instructions"])
        assert audit["provider"] == "openai" and audit["reasoning_effort"] == "low"
        assert audit["provider_request"] == subject.LUNA_REQUEST_POLICY
        assert audit["transport_policy"] == subject.LUNA_SELECTED_TRANSPORT_POLICY
        assert audit["status"] == "success" and audit["provider_usage_known"]
        assert audit["usage"]["reasoning_tokens"] == 50 and audit["output_tokens"] == 200
        assert audit["raw_response"]["output"][0]["content"][0]["text"]
        assert extractor.remote_cleanup_pending == 0
    gemini_constructor.assert_not_called()
    sdk.client.close.assert_called_once()
    assert len(list(tmp_path.glob("openai-selected-*.started.private.json"))) == 1
    assert len(list(tmp_path.glob("openai-selected-*.response.private.json"))) == 1
    for p in tmp_path.glob("openai-selected-*.private.json"):
        assert p.stat().st_mode & 0o777 == 0o600
        saved = p.read_text()
        assert "UNIT_ONLY_DO_NOT_SAVE_KEY" not in saved
        assert "DO NOT SAVE RAW INSTRUCTIONS" not in saved
        assert "DO NOT SAVE BASE64 INPUT" not in saved
        assert "data:application/pdf;base64," not in saved


def test_all_inline_parts_have_original_bytes_global_offsets_one_shared_call(sdk, tmp_path):
    bundle = prepared(tmp_path, partition=True)
    assert len(bundle["chunks"]) > 1
    original = subject.SelectedLunaExtractor(settings(), bundle, tmp_path)
    clone = copy.copy(original)
    clone.prompts = {**clone.prompts, "tdr": clone.prompts["tdr"] + " CLONE CONTEXT"}
    clone.extract(bundle["pdf_path"].read_bytes())
    call = sdk.client.generate.call_args.kwargs
    assert "CLONE CONTEXT" in call["instructions"]
    assert "CLONE CONTEXT" not in original.prompts["tdr"]
    contents = call["contents"]
    assert len(contents) == 2 * len(bundle["chunks"])
    for n, chunk in enumerate(bundle["chunks"]):
        instruction, file = contents[2*n:2*n+2]
        assert instruction["type"] == "input_text"
        assert f"{chunk['start_page']}..{chunk['end_page']}" in instruction["text"]
        assert f"offset={chunk['page_offset']}" in instruction["text"]
        assert file["type"] == "input_file" and file["detail"] == "high"
        assert base64.b64decode(file["file_data"].split(",", 1)[1]) == chunk["pdf_path"].read_bytes()
    assert len(original.attempts) == 1
    with pytest.raises(subject.SelectedTransportError, match="already used"):
        original.extract(bundle["pdf_path"].read_bytes())
    original.close()
    clone.close()
    sdk.client.generate.assert_called_once()
    sdk.client.close.assert_called_once()


@pytest.mark.parametrize("kind", ["json", "schema", "truncated", "refusal", "incomplete", "empty", "malformed"])
def test_failures_preserve_raw_paid_output_and_usage_before_parse_without_retry(sdk, tmp_path, kind):
    bundle = prepared(tmp_path)
    changes = {
        "json": body("PARTIAL INVALID JSON"),
        "schema": body('{"payment":{"tipo":"not-a-type"}}'),
        "truncated": body('PARTIAL INVALID JSON', status="incomplete", reason="max_output_tokens"),
        "refusal": body(refusal="Cannot extract"),
        "incomplete": body('{"summary":{}}', status="incomplete", reason="content_filter"),
        "empty": body("{}"),
        "malformed": {**body(), "output": "invalid"},
    }
    sdk.response_body = changes[kind]
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(Exception) as error:
            extractor.extract(bundle["pdf_path"].read_bytes())
        if kind == "truncated":
            assert isinstance(error.value, TruncatedResponseError)
        audit = extractor.attempts[0]
        assert audit["status"] == "response_parse_failure"
        assert audit["provider_usage_known"] and audit["cost_usd"] > 0
        assert audit["output_tokens"] == 200
        receipt = json.loads(next(tmp_path.glob("openai-selected-*.response.private.json")).read_text())
        assert receipt["status"] == "response_received" and receipt["provider_usage_known"]
        assert receipt["raw_response"] is not None
        if kind == "truncated":
            assert receipt["incomplete_reason"] == "max_output_tokens"
            assert receipt["raw_response"]["output"][0]["content"][0]["text"] == 'PARTIAL INVALID JSON'
        if kind == "refusal":
            assert receipt["refusal"] == "Cannot extract"
    sdk.client.generate.assert_called_once()


def test_usage_record_committed_before_application_json_parser(sdk, tmp_path, monkeypatch):
    bundle = prepared(tmp_path)
    def parse(_text):
        receipt = json.loads(next(tmp_path.glob("openai-selected-*.response.private.json")).read_text())
        assert receipt["provider_usage_known"] and receipt["raw_response"]["output"]
        raise ValueError("EXPECTED PARSE FAILURE")
    monkeypatch.setattr(subject, "parse_json_response", parse)
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(ValueError, match="EXPECTED"):
            extractor.extract(bundle["pdf_path"].read_bytes())
    sdk.client.generate.assert_called_once()


@pytest.mark.parametrize("usage", [None, {"input_tokens": True, "output_tokens": 100},
    {"input_tokens": 100, "output_tokens": -1},
    {"input_tokens": 100, "output_tokens": 50, "total_tokens": 1},
    {"input_tokens": 100, "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 51}}])
def test_unknown_or_malformed_usage_never_becomes_zero_cost_success(sdk, tmp_path, usage):
    bundle = prepared(tmp_path)
    sdk.response_body = {**body(), "usage": usage}
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(subject.SelectedTransportError, match="unknown token usage"):
            extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert audit["cost_usd"] is None and not audit["provider_usage_known"]
        assert audit["raw_response"]["output"]
    sdk.client.generate.assert_called_once()


def test_timeout_unknown_no_retry_and_exception_message_not_saved(sdk, tmp_path):
    bundle = prepared(tmp_path)
    sdk.client.generate.side_effect = TimeoutError("SECRET EXCEPTION BODY")
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(TimeoutError):
            extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert audit["status"] == "error_unknown" and audit["cost_usd"] is None
        assert audit["model_call_started"] and not audit["provider_usage_known"]
    sdk.client.generate.assert_called_once()
    assert all("SECRET EXCEPTION BODY" not in p.read_text() for p in tmp_path.glob("openai-selected-*"))


def test_budget_rejection_is_zero_unspent_before_request(sdk, tmp_path):
    bundle = prepared(tmp_path)
    def reject():
        raise RuntimeError("quota exhausted")
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path, reject) as extractor:
        with pytest.raises(RuntimeError, match="quota"):
            extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert audit["status"] == "budget_rejected" and audit["cost_usd"] == 0
        assert not audit["model_call_started"]
    sdk.client.generate.assert_not_called()
    assert not list(tmp_path.glob("openai-selected-*.started.private.json"))


@pytest.mark.parametrize("mutation", ["analysis_bytes", "chunk_bytes", "range", "omission", "source_sha", "scope"])
def test_changed_prepared_selection_fails_before_client_creation(sdk, tmp_path, mutation):
    bundle = prepared(tmp_path, partition=True)
    if mutation == "analysis_bytes":
        bundle["analysis_bytes"] += 1
    elif mutation == "chunk_bytes":
        bundle["chunks"][0]["pdf_path"].write_bytes(b"%PDF-WRONG")
    elif mutation == "range":
        bundle["chunks"][0]["page_offset"] = 2
    elif mutation == "omission":
        bundle["chunks"].pop()
    elif mutation == "source_sha":
        bundle["analysis_sha256"] = "f"*64
    else:
        bundle["document_scope"] = "unsupported"
    with pytest.raises(subject.SelectedTransportError):
        subject.SelectedLunaExtractor(settings(), bundle, tmp_path)
    sdk.constructor.assert_not_called()


def test_pdf_or_chunk_changed_after_constructor_never_dispatched(sdk, tmp_path):
    bundle = prepared(tmp_path)
    content = bundle["pdf_path"].read_bytes()
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        bundle["chunks"][0]["pdf_path"].write_bytes(b"%PDF-CHANGED")
        with pytest.raises(subject.SelectedTransportError):
            extractor.extract(content)
        assert extractor.attempts[0]["status"] == "transport_failed"
        assert extractor.attempts[0]["cost_usd"] == 0
    sdk.client.generate.assert_not_called()


def test_aggregate_inline_limit_never_omits_chunks_or_makes_another_call(sdk, tmp_path, monkeypatch):
    bundle = prepared(tmp_path, partition=True)
    maximum = sum(chunk["bytes"] for chunk in bundle["chunks"])
    monkeypatch.setattr(subject, "MAX_PDF_BYTES", maximum)
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(subject.SelectedTransportError, match="inline byte limit"):
            extractor.extract(bundle["pdf_path"].read_bytes())
        assert extractor.attempts[0]["cost_usd"] == 0
    sdk.client.generate.assert_not_called()


def test_focused_scope_retains_requirements_rejects_deprecated_fields(sdk, tmp_path):
    bundle = prepared(tmp_path, context={"llm1_scope_policy": QUALIFICATION_FOCUS_POLICY})
    sdk.response_body = body(json.dumps({"summary": {"descripcion_corta": "Software"},
        "requirements": {"provider": [{"tipo": "economic_experience", "descripcion": "Experience", "monto": 1000}]}}))
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        result = extractor.extract(bundle["pdf_path"].read_bytes())
        assert result.extraction.requirements.provider[0].monto == 1000
        assert not hasattr(result.extraction, "payment")
        assert result.prompt_version.startswith("prod2_qualification_focus_")
        assert result.schema_version.startswith("prod2_qualification_focus_")
    sdk.response_body = body('{"payment":{},"summary":{"descripcion_corta":"Software"}}')
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(ValueError, match="out-of-scope payment"):
            extractor.extract(bundle["pdf_path"].read_bytes())


def test_native_text_sends_exact_verified_full_text_not_pdf(sdk, tmp_path, monkeypatch):
    from buenapro_worker.prod2 import local_text
    bundle = prepared(tmp_path, context={"llm1_input_policy": NATIVE_TEXT_OR_PDF_POLICY})
    # Prepare requires its input proof after construction; build with the
    # same strict policy fields used by the local-only readiness module.
    exact = "GLOBAL PAGE 1\nCOMPLETE VERIFIED DOCUMENT TEXT"
    diagnostic = {"policy_version": local_text.LOCAL_TEXT_POLICY, "route": "candidate_text",
        "coverage_complete": True, "page_count": 1, "pages": [{"global_page": 1}],
        "pdf_sha256_before": bundle["analysis_sha256"], "pdf_sha256_after": bundle["analysis_sha256"],
        "text_sha256": hashlib.sha256(exact.encode()).hexdigest(), "text_bytes": len(exact.encode()),
        "cache_key": "b"*64}
    bundle["llm1_input"] = input_snapshot(diagnostic, bundle)
    verified = MagicMock(return_value=exact)
    monkeypatch.setattr(local_text, "read_verified_text", verified)
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path, local_text_result=diagnostic) as extractor:
        extractor.extract(bundle["pdf_path"].read_bytes())
        contents = sdk.client.generate.call_args.kwargs["contents"]
        assert contents == [{"type": "input_text", "text": exact}]
        assert extractor.attempts[0]["llm1_input"] == bundle["llm1_input"]
    verified.assert_called_once()


@pytest.mark.parametrize("request_hash", ["short", True, "G"*64, "a"*65])
def test_invalid_request_hash_rejected_before_client(sdk, tmp_path, request_hash):
    bundle = prepared(tmp_path)
    with pytest.raises(subject.SelectedTransportError, match="request hash"):
        subject.SelectedLunaExtractor(settings(), bundle, tmp_path, request_hash=request_hash)
    sdk.constructor.assert_not_called()


def test_count_tokens_never_generates_and_closed_reader_stays_closed(sdk, tmp_path):
    bundle = prepared(tmp_path)
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(subject.SelectedTransportError, match="token-count"):
            extractor.count_tokens(bundle["pdf_path"].read_bytes())
    with pytest.raises(subject.SelectedTransportError, match="closed"):
        extractor.extract(bundle["pdf_path"].read_bytes())
    sdk.client.generate.assert_not_called()


@pytest.mark.parametrize("mode", ["success", "invalid_json", "http_error"])
def test_real_transport_http_callback_binds_fixed_request_and_unknown_failures(sdk, tmp_path, monkeypatch, mode):
    from buenapro_worker.prod2.luna_transport import LunaResponsesClient
    bundle = prepared(tmp_path)
    calls = []
    def handle(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-6-luna" and payload["reasoning"] == {"effort": "low"}
        assert payload["max_output_tokens"] == 65536 and payload["store"] is False
        assert payload["service_tier"] == "default"
        assert payload["text"]["format"] == {"type": "json_object"}
        if mode == "invalid_json":
            return httpx.Response(200, text="UNPARSEABLE PROVIDER BODY")
        if mode == "http_error":
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded",
                "message": "DO NOT STORE PRIVATE ERROR MESSAGE"}})
        return httpx.Response(200, json=body())
    http = httpx.Client(transport=httpx.MockTransport(handle))
    real = LunaResponsesClient(settings().openai_api_key, client=http)
    monkeypatch.setattr(subject, "LunaResponsesClient", lambda _key: real)
    try:
        with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
            if mode == "success":
                assert extractor.extract(bundle["pdf_path"].read_bytes()).model == "gpt-6-luna"
            else:
                with pytest.raises(LunaTransportError):
                    extractor.extract(bundle["pdf_path"].read_bytes())
                audit = extractor.attempts[0]
                assert audit["model_call_started"] and not audit["provider_usage_known"]
                assert audit["cost_usd"] is None
                assert audit["response_body_sha256"] and audit["response_body_bytes"] > 0
                assert audit["http_status"] == (429 if mode == "http_error" else 200)
                if mode == "invalid_json":
                    assert audit["raw_response"] is None
                else:
                    assert audit["raw_response"]["error"] == {"code": "rate_limit_exceeded"}
        assert len(calls) == 1
        assert len(list(tmp_path.glob("openai-selected-*.response.private.json"))) == 1
        assert all("DO NOT STORE PRIVATE ERROR" not in p.read_text()
                   for p in tmp_path.glob("openai-selected-*.private.json"))
    finally:
        http.close()


def test_response_projection_never_copies_opaque_input_echoes(sdk, tmp_path):
    bundle = prepared(tmp_path)
    sdk.response_body["incomplete_details"] = {"file_data": "OPAQUE SECRET INPUT"}
    sdk.response_body["usage"]["input_tokens_details"]["file_data"] = "OPAQUE SECRET INPUT"
    sdk.response_body["usage"]["output_tokens_details"]["raw_input"] = "OPAQUE SECRET INPUT"
    sdk.response_body["output"].insert(0, {"type": "reasoning", "id": "reason-offline",
                                         "encrypted_content": "OPAQUE SECRET INPUT"})
    with subject.SelectedLunaExtractor(settings(), bundle, tmp_path) as extractor:
        extractor.extract(bundle["pdf_path"].read_bytes())
    assert all("OPAQUE SECRET INPUT" not in p.read_text()
               for p in tmp_path.glob("openai-selected-*.private.json"))
