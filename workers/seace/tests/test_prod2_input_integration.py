"""Complete local input -> single mock generation -> attributable receipt."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from buenapro_worker.normalization.facets import canonical_hash, derive_summary
from buenapro_worker.prod2 import analysis, input_policy, pdf_preparation, selected_extractor
from buenapro_worker.prod2.extraction_policy import (
    QUALIFICATION_FOCUS_POLICY, prompt_version_for_scope, schema_version_for_scope,
)
from test_prod2_local_text import pdf
from test_prod2_selected_extractor import sdk, settings, response
from test_prod2_selected_completion import selected_fixture, sync
from test_prod2_llm1_completion import finish


def native_prepared(tmp_path, *, image=False):
    data = pdf([("Experiencia del postor: 10000 soles. Personal tecnico. Consorcio permitido.", .8 if image else 0)])
    path = tmp_path / "original.pdf"
    path.write_bytes(data)
    code = "00000000-0000-4000-8000-000000000001"
    row = {"candidate_id": "loose:" + code, "document_uuid": code, "source_kind": "loose",
        "filename": "bases.pdf", "type": "Bases", "stage": "Convocatoria",
        "published_at": "06/10/2026 12:00", "source_url": "https://example.invalid/" + code,
        "original_sha256": hashlib.sha256(data).hexdigest()}
    context = {"official_inventory": [row], "llm1_scope_policy": QUALIFICATION_FOCUS_POLICY,
               "llm1_input_policy": input_policy.NATIVE_TEXT_OR_PDF_POLICY}
    prepared = pdf_preparation.prepare_selected_document_set(tmp_path, [row], {row["candidate_id"]: path},
        context, optimize_lossless=False)
    return input_policy.prepare_input(prepared, tmp_path)


def context(prepared):
    return {key: prepared[key] for key in ("document_scope", "document_set", "document_set_hash",
        "document_set_identity", "selection_context", "llm1_input")}


@pytest.mark.parametrize("image", [False, True])
def test_real_local_decision_binds_single_generation_prompt_schema_and_receipt(sdk, tmp_path, image):
    prepared, diagnostic = native_prepared(tmp_path, image=image)
    route = "pdf" if image else "text"
    assert prepared["llm1_input"]["route"] == route
    before = prepared["pdf_path"].read_bytes()
    callback = MagicMock()
    args = dict(object_type="service", manifest=prepared["source_document_manifest"], document_context=context(prepared))
    digest = analysis.extraction_request_hash(settings(), before, **args)
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
            callback, local_text_result=diagnostic) as reader:
        bundle = analysis.extract_document(settings(), before, **args, extractor=reader)
        call = sdk.client.models.generate_content.call_args.kwargs
        assert sdk.client.models.generate_content.call_count == callback.call_count == 1
        sdk.client.files.upload.assert_not_called()
        if route == "text":
            assert len(call["contents"]) == 1 and "PAGINA GLOBAL=1" in call["contents"][0].text
            assert call["contents"][0].inline_data is None and call["contents"][0].file_data is None
        else:
            assert call["contents"][1].inline_data.data == before
        props = call["config"].response_schema.model_json_schema()["properties"]
        assert "payment" not in props
        assert canonical_hash(call["config"].system_instruction) == bundle["prompt_hash"]
        assert reader.attempts[0]["llm1_input"] == prepared["llm1_input"]
    assert bundle["cache_key"] == "prod2:llm1:" + digest
    assert bundle["prompt_version"] == prompt_version_for_scope("bases_service", prepared["selection_context"])
    assert bundle["schema_version"] == schema_version_for_scope("bases_service", prepared["selection_context"])
    assert bundle["raw_extraction_json"]["llm1_input"] == bundle["llm1_input"] == prepared["llm1_input"]
    assert bundle["summary_json"]["extraction_coverage"]["not_evaluated"] == ["payment", "requirements.proposal_documents"]
    assert bundle["summary_json"]["tipo_pago"] is None
    assert prepared["pdf_path"].read_bytes() == before


@pytest.mark.parametrize("mutation", ["text", "proof", "source", "missing_diagnostic"])
def test_changed_local_input_is_rejected_before_model_reservation(sdk, tmp_path, mutation):
    prepared, diagnostic = native_prepared(tmp_path)
    callback = MagicMock()
    if mutation == "text": diagnostic["text_path"].write_text("changed")
    if mutation == "proof": prepared["llm1_input"]["text_sha256"] = "f" * 64
    if mutation == "source": prepared["pdf_path"].write_bytes(b"%PDF-changed")
    if mutation == "missing_diagnostic": diagnostic = None
    with pytest.raises(ValueError if mutation == "text" else (ValueError, selected_extractor.SelectedTransportError)):
        with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
                callback, local_text_result=diagnostic) as reader:
            reader.extract(prepared["pdf_path"].read_bytes())
    callback.assert_not_called()
    sdk.client.models.generate_content.assert_not_called()


def test_text_parse_failure_never_retries_pdf_and_keeps_usage_receipt(sdk, tmp_path):
    prepared, diagnostic = native_prepared(tmp_path)
    sdk.client.models.generate_content.return_value = response(text="invalid json")
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
            local_text_result=diagnostic) as reader:
        with pytest.raises(json.JSONDecodeError):
            reader.extract(prepared["pdf_path"].read_bytes())
        assert reader.attempts[0]["status"] == "response_parse_failure"
        assert reader.attempts[0]["provider_usage_known"]
    assert sdk.client.models.generate_content.call_count == 1
    sdk.client.files.upload.assert_not_called()


@pytest.mark.parametrize("extra", [{"payment": None}, {"requirements": {"proposal_documents": []}}])
def test_provider_scope_violation_is_not_silently_erased(sdk, tmp_path, extra):
    prepared, diagnostic = native_prepared(tmp_path)
    sdk.client.models.generate_content.return_value = response(text=json.dumps({"summary": {"descripcion_corta": "Software"}, **extra}))
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
            local_text_result=diagnostic) as reader:
        with pytest.raises(ValueError, match="out-of-scope"):
            reader.extract(prepared["pdf_path"].read_bytes())
    assert sdk.client.models.generate_content.call_count == 1


def focused_receipt(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    selection = extraction["selection_context"]
    selection.update(llm1_scope_policy=QUALIFICATION_FOCUS_POLICY,
                     llm1_input_policy=input_policy.NATIVE_TEXT_OR_PDF_POLICY)
    manifest = extraction["raw_extraction_json"]["source_document_manifest"]
    proof = {"policy_version": input_policy.NATIVE_TEXT_OR_PDF_POLICY, "route": "text",
        "analysis_sha256": extraction["analysis_sha256"], "page_count": manifest[-1]["end_page"],
        "text_sha256": "f" * 64, "text_bytes": 100, "local_cache_key": "e" * 64,
        "diagnostic_sha256": "d" * 64, "source_manifest_hash": canonical_hash(manifest)}
    extraction.update(llm1_input=proof, prompt_version=prompt_version_for_scope("bases_service", selection),
        schema_version=schema_version_for_scope("bases_service", selection))
    extraction["raw_extraction_json"].update(llm1_input=deepcopy(proof), extraction_coverage={
        "policy_version": QUALIFICATION_FOCUS_POLICY, "not_evaluated": ["payment", "requirements.proposal_documents"]})
    sync(extraction, conn)
    return conn, extraction


def test_focused_receipt_persists_with_bound_input_and_keeps_authority_review(monkeypatch):
    conn, extraction = focused_receipt(monkeypatch)
    before = deepcopy(extraction)
    saved = finish(conn, extraction)
    assert saved["projection_pending"] and saved["requires_human_review"]
    assert len(conn.persist_calls) == 1 and extraction == before
    assert derive_summary(extraction["raw_extraction_json"])["tipo_pago"] is None


@pytest.mark.parametrize("mutation", ["rawproof", "paidproof", "pages", "version", "payment", "proposal", "coverage"])
def test_invalid_focused_receipt_cannot_persist(monkeypatch, mutation):
    from buenapro_worker.prod2.llm1_completion import Llm1CompletionError
    conn, extraction = focused_receipt(monkeypatch)
    if mutation == "rawproof": extraction["raw_extraction_json"]["llm1_input"]["text_sha256"] = "1" * 64
    if mutation == "paidproof": conn.reservation["result_json"]["llm1_input"]["text_sha256"] = "1" * 64
    if mutation == "pages": extraction["llm1_input"]["page_count"] += 1
    if mutation == "version": extraction["schema_version"] = "legacy"
    if mutation == "payment": extraction["raw_extraction_json"]["payment"] = {}
    if mutation == "proposal": extraction["raw_extraction_json"]["requirements"] = {"proposal_documents": []}
    if mutation == "coverage": extraction["raw_extraction_json"].pop("extraction_coverage")
    with pytest.raises(Llm1CompletionError): finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates


@pytest.mark.parametrize("remove_input_policy", [False, True])
def test_new_policy_requires_exact_selected_reader_before_any_client_or_generation(sdk, tmp_path, monkeypatch, remove_input_policy):
    prepared, diagnostic = native_prepared(tmp_path)
    params = dict(object_type="service", manifest=prepared["source_document_manifest"], document_context=context(prepared))
    generic = MagicMock()
    monkeypatch.setattr(analysis, "GeminiExtractor", generic)
    with pytest.raises(ValueError, match="exactly bound"):
        analysis.extract_document(settings(), prepared["pdf_path"].read_bytes(), **params)
    generic.assert_not_called()
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
            local_text_result=diagnostic) as reader:
        changed = deepcopy(params)
        changed["document_context"]["selection_context"].pop("llm1_scope_policy")
        if remove_input_policy:
            changed["document_context"]["selection_context"].pop("llm1_input_policy")
            changed["document_context"].pop("llm1_input")
        changed["document_context"]["document_set_identity"]["selection_context"] = deepcopy(changed["document_context"]["selection_context"])
        changed["document_context"]["document_set_hash"] = canonical_hash(changed["document_context"]["document_set_identity"])
        with pytest.raises(ValueError, match="exactly bound"):
            analysis.extract_document(settings(), prepared["pdf_path"].read_bytes(), **changed, extractor=reader)
    sdk.client.models.generate_content.assert_not_called()
