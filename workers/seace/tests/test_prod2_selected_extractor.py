"""Mock SDK only: one generation, Files ownership, offsets and paid failure audit."""
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors as genai_errors
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from buenapro_worker.extraction.gemini import TruncatedResponseError, estimate_cost
from buenapro_worker.prod2 import pdf_preparation, selected_extractor as subject


def prepared(folder, *, partition=False, cache_folder=None, context=None):
    writer = PdfWriter()
    for number in range(5 if partition else 1):
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(b"%" + bytes([65 + number]) * 2000 + b"\n")
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()
    code = "00000000-0000-4000-8000-000000000001"
    path = folder / "source.pdf"
    path.write_bytes(content)
    row = {"candidate_id": "loose:" + code, "document_uuid": code, "source_kind": "loose",
        "filename": "requirements.pdf", "type": "Bases", "stage": "Convocatoria",
        "published_at": "06/10/2026 12:00", "source_url": "https://example.invalid/official/" + code,
        "original_sha256": hashlib.sha256(content).hexdigest()}
    return pdf_preparation.prepare_selected_document_set(folder, [row], {row["candidate_id"]: path},
        {"official_inventory": [row], **(context or {})}, max_analysis_bytes=5000 if partition else 45_000_000,
        optimize_lossless=False, cache_folder=cache_folder)


def response(text=None, *, usage=True, truncated=False):
    return SimpleNamespace(text=text if text is not None else json.dumps({"summary": {"descripcion_corta": "Software"}}),
        usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=200,
            total_token_count=1200, thoughts_token_count=0, cached_content_token_count=0) if usage else None,
        candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")] if truncated else [])


@pytest.fixture
def sdk(monkeypatch):
    client, uploads = MagicMock(), []
    def upload(*, file, config):
        uploads.append(file.read())
        index = len(uploads)
        assert config.mime_type == "application/pdf"
        return SimpleNamespace(name=f"files/mock-{index}", uri=f"https://mock.invalid/files/mock-{index}", state="ACTIVE")
    client.files.upload.side_effect = upload
    client.models.generate_content.return_value = response()
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr(subject.genai, "Client", constructor)
    monkeypatch.setattr(pdf_preparation, "prepare_pdf", lambda path, *args, **kwargs: path)
    return SimpleNamespace(client=client, uploads=uploads, constructor=constructor)


def settings():
    return SimpleNamespace(gemini_api_key="fake-unit-key", gemini_model="gemini-3.1-flash-lite",
        gemini_fallback_model="gemini-2.5-flash")


def test_small_pdf_inline_reserves_once_and_audits_before_parse(sdk, tmp_path):
    bundle = prepared(tmp_path)
    callback = MagicMock()
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, callback) as extractor:
        result = extractor.extract(bundle["pdf_path"].read_bytes())
        assert result.model == settings().gemini_model
        assert result.input_tokens == 1000 and result.output_tokens == 200
        assert callback.call_count == sdk.client.models.generate_content.call_count == 1
        sdk.client.files.upload.assert_not_called()
        contents = sdk.client.models.generate_content.call_args.kwargs["contents"]
        assert len(contents) == 2 and "offset=0" in contents[0].text
        assert contents[1].inline_data.data == bundle["pdf_path"].read_bytes()
        audit = extractor.attempts[0]
        assert audit["status"] == "success" and audit["provider_usage_known"]
        assert audit["elapsed_seconds"] >= 0
        assert audit["cost_usd"] == estimate_cost(result.model, 1000, 200)
        assert audit["remote_cleanup_pending"] == 0
    sdk.client.close.assert_called_once()
    assert sdk.constructor.call_args.kwargs["http_options"].retry_options.attempts == 1
    assert len(list(tmp_path.glob("gemini-selected-*.response.private.json"))) == 1
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.glob("gemini-selected-*.private.json"))


@pytest.mark.parametrize("object_type", ["good", "service"])
@pytest.mark.parametrize("qualification", [False, True])
def test_opt_in_commercial_prompt_sent_once_matches_reserved_identity_in_legacy_and_focused_scope(
        sdk, tmp_path, object_type, qualification):
    from buenapro_worker.normalization.facets import canonical_hash
    from buenapro_worker.prod2.analysis import _document_input, extract_document
    context = {"llm1_commercial_taxonomy": "commercial_taxonomy_v1"}
    if qualification:
        context["llm1_scope_policy"] = "prod2_qualification_focus_v1"
    bundle = prepared(tmp_path, context=context)
    attribution = {key: bundle[key] for key in (
        "document_scope", "document_set", "document_set_hash", "document_set_identity", "selection_context")}
    manifest, content = bundle["source_document_manifest"], bundle["pdf_path"].read_bytes()
    raw = {"summary": {"descripcion_corta": "Clasificación pendiente"}, "commercial_classification": {
        "taxonomy_version": "commercial_taxonomy_v1", "primary_area": "sin_clasificar",
        "secondary_areas": [], "work_types": [], "specialties": [],
        "support_status": "needs_review", "evidence": []}}
    if object_type == "good":
        raw["goods"] = {"items": []}
    sdk.client.models.generate_content.return_value = response(json.dumps(raw))
    original_sha = bundle["document_set"][0]["original_sha256"]
    inputs = _document_input(settings(), content, object_type, original_sha, manifest, attribution)
    reserve = MagicMock()
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, reserve) as extractor:
        result = extract_document(settings(), content, object_type=object_type,
            original_sha256=original_sha, manifest=manifest, document_context=attribution, extractor=extractor)
    prompt = sdk.client.models.generate_content.call_args.kwargs["config"].system_instruction
    assert prompt.count("CLASIFICACION_COMERCIAL=") == 1
    assert canonical_hash(prompt) == inputs["prompt_hash"] == result["prompt_hash"]
    assert result["cache_key"] == "prod2:llm1:" + canonical_hash(inputs)
    assert reserve.call_count == sdk.client.models.generate_content.call_count == 1


def test_all_transport_parts_upload_in_one_request_with_global_offsets_and_cleanup_across_copy(sdk, tmp_path):
    bundle = prepared(tmp_path, partition=True)
    count = len(bundle["chunks"])
    assert count > 1
    def reserve():
        assert sdk.client.files.upload.call_count == count
        sdk.client.models.generate_content.assert_not_called()
    original = subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, reserve)
    reader = copy.copy(original)
    reader.prompts = {**reader.prompts, "tdr": reader.prompts["tdr"] + " CLONED CONTEXT"}
    reader.extract(bundle["pdf_path"].read_bytes())
    call = sdk.client.models.generate_content.call_args.kwargs
    assert call["model"] == settings().gemini_model
    assert "CLONED CONTEXT" in call["config"].system_instruction
    assert "CLONED CONTEXT" not in original.prompts["tdr"]
    assert len(call["contents"]) == count * 2
    for number, chunk in enumerate(bundle["chunks"]):
        instruction, data = call["contents"][number * 2:number * 2 + 2]
        assert f"{chunk['start_page']}..{chunk['end_page']}" in instruction.text
        assert f"offset={chunk['page_offset']}" in instruction.text
        assert data.file_data.file_uri.endswith(f"mock-{number + 1}")
        assert sdk.uploads[number] == chunk["pdf_path"].read_bytes()
    assert sdk.client.models.generate_content.call_count == 1
    assert sdk.client.files.delete.call_count == count
    assert original.remote_cleanup_pending == 0 and len(original.attempts) == 1
    with pytest.raises(subject.SelectedTransportError, match="already used"):
        original.extract(bundle["pdf_path"].read_bytes())
    original.close()


def test_single_pdf_above_inline_threshold_uses_files_api(sdk, monkeypatch, tmp_path):
    bundle = prepared(tmp_path)
    monkeypatch.setattr(subject, "INLINE_MAX_BYTES", 1)
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        extractor.extract(bundle["pdf_path"].read_bytes())
    assert sdk.client.files.upload.call_count == sdk.client.files.delete.call_count == 1
    assert sdk.client.models.generate_content.call_args.kwargs["contents"][1].file_data


def test_shared_cache_pdf_is_allowed_but_audit_remains_in_current_run_folder(sdk, tmp_path):
    run = tmp_path / "current-run"
    run.mkdir()
    bundle = prepared(run, cache_folder=tmp_path / "shared-cache")
    assert not bundle["pdf_path"].is_relative_to(run)
    with subject.SelectedGeminiExtractor(settings(), bundle, run) as extractor:
        extractor.extract(bundle["pdf_path"].read_bytes())
    assert len(list(run.glob("gemini-selected-*.outcome.private.json"))) == 1
    assert not list((tmp_path / "shared-cache").glob("gemini-selected-*"))


def test_thinking_tokens_are_included_in_result_receipt_and_failed_parse_cost(sdk, tmp_path):
    bundle = prepared(tmp_path)
    successful = response()
    successful.usage_metadata.thoughts_token_count = 300
    successful.candidates = [SimpleNamespace(finish_reason="STOP")]
    sdk.client.models.generate_content.return_value = successful
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        result = extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert result.output_tokens == audit["output_tokens"] == 500
        assert result.input_tokens == audit["input_tokens"] == 1000
        assert result.cost_usd == audit["cost_usd"] == estimate_cost(result.model, 1000, 500)
        assert audit["usage"]["candidates_token_count"] == 200
        assert audit["usage"]["thoughts_token_count"] == 300
        assert audit["finish_reason"] == "STOP"
    successful.text = "bad JSON"
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(ValueError):
            extractor.extract(bundle["pdf_path"].read_bytes())
        assert extractor.attempts[0]["output_tokens"] == 500
        assert extractor.attempts[0]["cost_usd"] == estimate_cost(result.model, 1000, 500)


@pytest.mark.parametrize("kind", ["json", "schema", "truncated"])
def test_response_parse_failure_preserves_usage_and_cost_without_retry(sdk, tmp_path, kind):
    bundle = prepared(tmp_path)
    sdk.client.models.generate_content.return_value = response(
        "invalid JSON" if kind == "json" else '{"payment":{"tipo":"invalid"}}' if kind == "schema" else None,
        truncated=kind == "truncated")
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(Exception) as error:
            extractor.extract(bundle["pdf_path"].read_bytes())
        if kind == "truncated":
            assert isinstance(error.value, TruncatedResponseError)
        audit = extractor.attempts[0]
        assert audit["status"] == "response_parse_failure"
        assert audit["input_tokens"] == 1000 and audit["output_tokens"] == 200
        assert audit["cost_usd"] == estimate_cost(settings().gemini_model, 1000, 200)
        receipt = json.loads(next(tmp_path.glob("gemini-selected-*.response.private.json")).read_text())
        text = sdk.client.models.generate_content.return_value.text
        assert receipt["response_text"] == text
        assert receipt["response_text_bytes"] == len(text.encode("utf-8"))
        assert receipt["response_text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert "response_text" not in audit
        assert receipt["response_text_status"] == "captured"
        if kind == "json":
            assert audit["parse_diagnostic"] == {"kind": "json", "position": 0, "line": 1, "column": 1}
        elif kind == "schema":
            assert audit["parse_diagnostic"]["kind"] == "schema"
            assert audit["parse_diagnostic"]["errors"][0]["location"] == ["payment", "tipo"]
        else:
            assert "parse_diagnostic" not in audit
    assert sdk.client.models.generate_content.call_count == 1
    assert sdk.client.models.generate_content.call_args.kwargs["model"] != settings().gemini_fallback_model


def test_generated_text_receipt_precedes_parsing_and_body_stays_private(sdk, monkeypatch, tmp_path):
    from buenapro_worker.extraction import gemini
    bundle = prepared(tmp_path)
    text = '{"summary":{"descripcion_corta":"PRIVATE GENERATED RESULT: revisión"}} trailing'
    actual = response(text)
    actual.input = "DO NOT SAVE SDK INPUT"
    actual.candidates = [SimpleNamespace(finish_reason="STOP", inline_data=b"DO NOT SAVE IMAGE")]
    sdk.client.models.generate_content.return_value = actual
    original_parse = gemini.parse_json_response
    def parse(value):
        receipt = json.loads(next(tmp_path.glob("gemini-selected-*.response.private.json")).read_text())
        assert receipt["response_text"] == value == text
        assert receipt["response_text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert receipt["response_text_bytes"] == len(text.encode("utf-8"))
        assert receipt["provider_usage_known"] and receipt["finish_reason"] == "STOP"
        return original_parse(value)
    monkeypatch.setattr(gemini, "parse_json_response", parse)
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(json.JSONDecodeError) as caught:
            extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert audit["parse_diagnostic"] == {"kind": "json", "position": caught.value.pos,
            "line": caught.value.lineno, "column": caught.value.colno}
        assert "PRIVATE GENERATED RESULT" not in json.dumps(audit)
    for file in tmp_path.glob("gemini-selected-*.private.json"):
        content = file.read_text()
        assert "DO NOT SAVE SDK INPUT" not in content and "DO NOT SAVE IMAGE" not in content
        if not file.name.endswith(".response.private.json"):
            assert "PRIVATE GENERATED RESULT" not in content
        assert file.stat().st_mode & 0o777 == 0o600
    assert sdk.client.models.generate_content.call_count == 1


def test_validation_diagnostics_never_include_values_messages_or_unknown_keys():
    from buenapro_worker.extraction.schemas import TdrExtractionV2
    error = ValidationError.from_exception_data("PRIVATE TITLE", [{"type": "int_parsing",
        "loc": ("requirements", "PRIVATE KEY", 2), "input": "PRIVATE VALUE"}])
    diagnostic = subject._parse_diagnostic(error, TdrExtractionV2)
    assert diagnostic == {"kind": "schema", "error_count": 1, "details_truncated": False,
        "errors": [{"location": ["requirements", "<key>", 2], "type": "int_parsing"}]}
    assert "PRIVATE" not in json.dumps(diagnostic)


@pytest.mark.parametrize("value", [None, 123, MagicMock(), "\ud800"])
def test_unavailable_or_non_text_sdk_projection_does_not_replace_usage(value):
    assert subject._response_text_snapshot(SimpleNamespace(text=value)) == {
        "response_text": None, "response_text_status": "unavailable",
        "response_text_sha256": None, "response_text_bytes": None}


def test_raising_sdk_text_property_is_audited_as_unavailable():
    class Response:
        @property
        def text(self):
            raise ValueError("PRIVATE SDK MESSAGE")
    assert subject._response_text_snapshot(Response())["response_text_status"] == "unavailable"


def test_timeout_is_billing_unknown_not_zero_and_never_repeated(sdk, tmp_path):
    bundle = prepared(tmp_path, partition=True)
    sdk.client.models.generate_content.side_effect = TimeoutError("DO NOT STORE PRIVATE RESPONSE")
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(TimeoutError):
            extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert audit["status"] == "error_unknown" and audit["cost_usd"] is None
        assert audit["model_call_started"] and not audit["provider_usage_known"]
    assert sdk.client.models.generate_content.call_count == 1
    assert sdk.client.files.delete.call_count == len(bundle["chunks"])
    assert all("DO NOT STORE" not in path.read_text() for path in tmp_path.glob("gemini-selected-*.private.json"))


@pytest.mark.parametrize("error,code,status", [
    (lambda: genai_errors.ClientError(404, {"error": {"code": 404, "message": "DO NOT STORE", "status": "NOT_FOUND"}}),
     404, "NOT_FOUND"),
    (lambda: genai_errors.ServerError(503, {"error": {"code": 503, "message": "DO NOT STORE", "status": "UNAVAILABLE"}}),
     503, "UNAVAILABLE"),
])
def test_an_http_error_answered_by_gemini_is_a_known_free_rejection(sdk, tmp_path, error, code, status):
    """28/10: a retired model (404) and saturation (503) left the case 'uncertain' and blocked it."""
    bundle = prepared(tmp_path)
    sdk.client.models.generate_content.side_effect = error()
    observed = []
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, after_attempt=observed.append) as extractor:
        with pytest.raises(subject.SelectedProviderRejected) as rejected:
            extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
    assert (rejected.value.code, rejected.value.status) == (code, status)
    assert audit["status"] == "provider_rejected" and audit["cost_usd"] == 0 and audit["provider_usage_known"]
    assert (audit["http_status"], audit["provider_error_code"]) == (code, status)
    assert observed[0]["status"] == "provider_rejected" and observed[0]["provider_usage_known"] is True
    assert sdk.client.models.generate_content.call_count == 1  # Never repeated here.
    assert all("DO NOT STORE" not in path.read_text() for path in tmp_path.glob("gemini-selected-*.private.json"))


@pytest.mark.parametrize("limits,expected", [({}, (24576, 180000)),
    ({"max_output_tokens": 65536, "timeout_ms": 300000}, (65536, 300000))])
def test_generation_limits_are_explicit_audited_and_keep_one_sdk_attempt(sdk, tmp_path, limits, expected):
    bundle = prepared(tmp_path)
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, **limits) as extractor:
        extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert (audit["max_output_tokens"], audit["timeout_ms"]) == expected
    assert sdk.constructor.call_args.kwargs["http_options"].timeout == expected[1]
    assert sdk.constructor.call_args.kwargs["http_options"].retry_options.attempts == 1
    assert sdk.client.models.generate_content.call_args.kwargs["config"].max_output_tokens == expected[0]
    assert sdk.client.models.generate_content.call_count == 1


@pytest.mark.parametrize("limits", [{"max_output_tokens": True}, {"max_output_tokens": 100000},
    {"timeout_ms": True}, {"timeout_ms": 600000}, {"max_output_tokens": 65536.0}])
def test_unsupported_generation_limits_fail_before_client_creation(sdk, tmp_path, limits):
    bundle = prepared(tmp_path)
    with pytest.raises(subject.SelectedTransportError, match="explicit supported"):
        subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, **limits)
    sdk.constructor.assert_not_called()


def test_unknown_usage_is_recorded_and_blocks_false_zero_success(sdk, tmp_path):
    bundle = prepared(tmp_path)
    sdk.client.models.generate_content.return_value = response(usage=False)
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(subject.SelectedTransportError, match="unknown token usage"):
            extractor.extract(bundle["pdf_path"].read_bytes())
        assert extractor.attempts[0]["cost_usd"] is None
        assert not extractor.attempts[0]["provider_usage_known"]
    assert sdk.client.models.generate_content.call_count == 1


def test_budget_rejection_prevents_generation_and_cleans_uploaded_files(sdk, tmp_path):
    bundle = prepared(tmp_path, partition=True)
    def reject():
        raise RuntimeError("model quota exhausted")
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, reject) as extractor:
        with pytest.raises(RuntimeError, match="quota"):
            extractor.extract(bundle["pdf_path"].read_bytes())
        assert extractor.attempts[0]["status"] == "budget_rejected"
        assert extractor.attempts[0]["cost_usd"] == 0
        assert not extractor.attempts[0]["model_call_started"]
    sdk.client.models.generate_content.assert_not_called()
    assert sdk.client.files.delete.call_count == len(bundle["chunks"])


def test_readiness_poll_is_bounded_and_processing_failure_never_generates(sdk, monkeypatch, tmp_path):
    bundle = prepared(tmp_path, partition=True)
    sdk.client.files.upload.side_effect = None
    sdk.client.files.upload.return_value = SimpleNamespace(name="files/processing", uri="https://mock.invalid/file", state="PROCESSING")
    times = iter([0, 61])
    monkeypatch.setattr(subject.time, "monotonic", lambda: next(times))
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(subject.SelectedTransportError, match="readable"):
            extractor.extract(bundle["pdf_path"].read_bytes())
        assert extractor.attempts[0]["status"] == "transport_failed"
    sdk.client.models.generate_content.assert_not_called()
    sdk.client.files.get.assert_not_called()
    sdk.client.files.delete.assert_called_once_with(name="files/processing")


def test_processing_files_are_readied_before_budget_reservation_and_generation(sdk, monkeypatch, tmp_path):
    bundle = prepared(tmp_path)
    monkeypatch.setattr(subject, "INLINE_MAX_BYTES", 1)
    monkeypatch.setattr(subject.time, "sleep", lambda duration: None)
    sdk.client.files.upload.side_effect = None
    sdk.client.files.upload.return_value = SimpleNamespace(name="files/processing", uri=None,
        state=subject.types.FileState.PROCESSING)
    sdk.client.files.get.return_value = SimpleNamespace(name="files/processing",
        uri="https://mock.invalid/ready", state=subject.types.FileState.ACTIVE)
    def reserve():
        sdk.client.files.get.assert_called_once_with(name="files/processing")
        sdk.client.models.generate_content.assert_not_called()
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path, reserve) as extractor:
        extractor.extract(bundle["pdf_path"].read_bytes())
    assert sdk.client.models.generate_content.call_count == 1
    sdk.client.files.delete.assert_called_once_with(name="files/processing")


def test_failed_second_upload_cleans_known_file_and_records_unknown_creation(sdk, tmp_path):
    bundle = prepared(tmp_path, partition=True)
    sdk.client.files.upload.side_effect = [
        SimpleNamespace(name="files/known", uri="https://mock.invalid/known", state="ACTIVE"),
        TimeoutError("not for audit")]
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        with pytest.raises(TimeoutError):
            extractor.extract(bundle["pdf_path"].read_bytes())
        audit = extractor.attempts[0]
        assert audit["remote_cleanup_pending"] == 0 and audit["remote_cleanup_unknown"]
        assert not audit["model_call_started"] and audit["cost_usd"] == 0
    sdk.client.files.delete.assert_called_once_with(name="files/known")
    sdk.client.models.generate_content.assert_not_called()


def test_failed_cleanup_retains_shared_ownership_for_close_retry_and_audit(sdk, tmp_path):
    bundle = prepared(tmp_path, partition=True)
    calls = {}
    def delete(*, name):
        calls[name] = calls.get(name, 0) + 1
        if calls[name] == 1:
            raise TimeoutError()
    sdk.client.files.delete.side_effect = delete
    original = subject.SelectedGeminiExtractor(settings(), bundle, tmp_path)
    copy.copy(original).extract(bundle["pdf_path"].read_bytes())
    assert original.remote_cleanup_pending == len(bundle["chunks"])
    original.close()
    assert original.remote_cleanup_pending == 0
    receipt = json.loads(next(tmp_path.glob("gemini-selected-*.cleanup.private.json")).read_text())
    assert receipt["remote_cleanup_pending"] == 0


def test_tampered_prepared_hash_or_ranges_block_before_client_and_changed_part_before_generate(sdk, tmp_path):
    bundle = prepared(tmp_path)
    wrong = copy.deepcopy(bundle)
    wrong["document_set_identity"]["selection_context"] = {"changed": True}
    with pytest.raises(subject.SelectedTransportError, match="identity"):
        subject.SelectedGeminiExtractor(settings(), wrong, tmp_path)
    sdk.constructor.assert_not_called()
    wrong = copy.deepcopy(bundle)
    wrong["chunks"][0]["page_offset"] = 9
    with pytest.raises(subject.SelectedTransportError, match="ranges"):
        subject.SelectedGeminiExtractor(settings(), wrong, tmp_path)
    sdk.constructor.assert_not_called()
    with subject.SelectedGeminiExtractor(settings(), bundle, tmp_path) as extractor:
        bundle["chunks"][0]["pdf_path"].write_bytes(b"changed")
        with pytest.raises(subject.SelectedTransportError, match="bytes changed"):
            extractor.extract(bundle["pdf_path"].read_bytes())
    sdk.client.models.generate_content.assert_not_called()
