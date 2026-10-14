"""Retrieved evidence is partial, text-only, and immutable before generation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import analysis, input_policy, selected_extractor, selected_luna
from buenapro_worker.prod2.extraction_policy import QUALIFICATION_FOCUS_POLICY
from buenapro_worker.prod2.luna_policy import luna_settings, provider_instruction
from test_prod2_input_integration import native_prepared, context, focused_receipt
from test_prod2_selected_extractor import sdk, settings, response
from test_prod2_llm1_completion import finish, NOW
from test_prod2_selected_completion import sync


def result_for(prepared, path):
    text = "Experiencia del postor: 10000 soles. Personal tecnico. Consorcio permitido."
    row = prepared["source_document_manifest"][0]
    block = {"block_id": "block-0001", "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "global_page": 1,
        "page_start": 1, "page_end": 1, "section_id": "qualification", "table_id": None,
        "locations": [{"source_page": 1, "candidate_id": row["candidate_id"],
            "document_uuid": row["document_uuid"], "source_sha256": row["original_sha256"]}]}
    transport = "BLOQUE=block-0001 PAGINA GLOBAL=1\n" + text
    path = Path(path).resolve()
    path.write_text(transport)
    result = {"policy_version": input_policy.RETRIEVAL_POLICY_VERSION,
        "route": "retrieved_text", "analysis_sha256": prepared["analysis_sha256"],
        "page_count": prepared["page_count"],
        "source_manifest_hash": canonical_hash(prepared["source_document_manifest"]),
        "corpus_hash": "a" * 64, "retrieval_hash": "b" * 64,
        "context_sha256": hashlib.sha256(transport.encode()).hexdigest(),
        "context_bytes": len(transport.encode()), "context_tokens": 35, "source_tokens": 100,
        "context_text": transport, "context_text_path": path,
        "selected_chunk_ids": ["c" * 64], "selected_blocks": [block],
        "coverage_by_field": {"provider_requirements": {"seeds": 1, "verified_complete": False}},
        "indexed_complete": True, "citations": block["locations"],
        "needs_review": True, "reasons": ["partial_context_requires_review"]}
    result["durable_proof"] = {"version": "prod2_embedding_durable_proof_v1", "corpus_id": 1,
        "retrieval_id": 2, "corpus_hash": result["corpus_hash"],
        "retrieval_hash": result["retrieval_hash"], "context_sha256": result["context_sha256"],
        "corpus_payload_sha256": "d" * 64, "retrieval_payload_sha256": "e" * 64,
        "chunk_count": 1, "blocks_persisted": True, "vectors_persisted": True,
        "pg_verified": True, "verified_at": "2026-10-08T22:00:00Z"}
    return result


def retrieved_prepared(tmp_path):
    native, _ = native_prepared(tmp_path)
    native.pop("llm1_input")
    native["selection_context"]["llm1_input_policy"] = input_policy.EMBEDDING_RETRIEVED_TEXT_POLICY
    native["document_set_identity"]["selection_context"] = deepcopy(native["selection_context"])
    native["document_set_hash"] = native["selection_hash"] = canonical_hash(native["document_set_identity"])
    # Preparation location itself belongs to the new identity.
    old = native["pdf_path"].parent
    new = old.parent / ("selected-analysis-" + native["selection_hash"])
    old.rename(new)
    native["pdf_path"] = new / "analysis.pdf"
    for chunk in native["chunks"]:
        chunk["pdf_path"] = new / Path(chunk["pdf_path"]).name
    result = result_for(native, tmp_path / "context.private.txt")
    native["retrieval_input_result"] = result
    return input_policy.prepare_input(native, tmp_path)


def visual_review_prepared(tmp_path):
    prepared, result = retrieved_prepared(tmp_path)
    prepared["selection_context"][input_policy.VISUAL_REVIEW_POLICY_KEY] = input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY
    prepared["document_set_identity"]["selection_context"] = deepcopy(prepared["selection_context"])
    prepared["document_set_hash"] = prepared["selection_hash"] = canonical_hash(prepared["document_set_identity"])
    old = prepared["pdf_path"].parent
    new = old.parent / ("selected-analysis-" + prepared["selection_hash"])
    old.rename(new)
    prepared["pdf_path"] = new / "analysis.pdf"
    for chunk in prepared["chunks"]:
        chunk["pdf_path"] = new / Path(chunk["pdf_path"]).name
    return prepared, result


def test_visual_review_v2_is_explicit_and_keeps_v1_prompt_and_response_schema(tmp_path):
    prepared, _ = retrieved_prepared(tmp_path)
    old = deepcopy(prepared["selection_context"])
    new = {**old, input_policy.VISUAL_REVIEW_POLICY_KEY: input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY}
    prompt1 = input_policy.prompt_for_input("bases_service", old, "LEGACY")
    prompt2 = input_policy.prompt_for_input("bases_service", new, "LEGACY")
    assert prompt2.startswith(prompt1) and "REVISION_VISUAL=" not in prompt1
    assert "Cobertura de texto NO certifica lectura visual completa" in prompt2
    assert "NO es por ello requisito obligatorio" in prompt2
    assert input_policy.prompt_version_for_input("bases_service", old).endswith("prompt_v1")
    assert input_policy.prompt_version_for_input("bases_service", new).endswith("prompt_v2")
    assert canonical_hash(input_policy.schema_for_input("bases_service", old).model_json_schema()) == canonical_hash(
        input_policy.schema_for_input("bases_service", new).model_json_schema())
    assert input_policy.schema_version_for_input("bases_service", old) == input_policy.schema_version_for_input("bases_service", new)
    assert input_policy.prompt_for_input("bases_service", old, "LEGACY") == prompt1


@pytest.mark.parametrize("provider", ["gemini", "luna"])
def test_visual_review_v2_effective_paid_prompt_hash_and_canonical_review(sdk, monkeypatch, tmp_path, provider):
    prepared, result = visual_review_prepared(tmp_path)
    data = prepared["pdf_path"].read_bytes()
    args = {"object_type": "service", "manifest": prepared["source_document_manifest"], "document_context": context(prepared)}
    if provider == "gemini":
        s = settings()
        with selected_extractor.SelectedGeminiExtractor(s, prepared, tmp_path, local_text_result=result) as reader:
            bundle = analysis.extract_document(s, data, **args, extractor=reader)
        prompt = sdk.client.models.generate_content.call_args.kwargs["config"].system_instruction
        assert canonical_hash(prompt) == bundle["prompt_hash"]
        assert bundle["prompt_version"].endswith("prompt_v2") and bundle["schema_version"].endswith("schema_v1")
        assert bundle["requires_human_review"]
        sdk.client.files.upload.assert_not_called()
    else:
        client = MagicMock()
        monkeypatch.setattr(selected_luna, "LunaResponsesClient", lambda _: client)
        s = luna_settings(settings())
        s.openai_api_key = "synthetic-key"
        inputs = analysis._document_input(s, data, "service", None, prepared["source_document_manifest"], context(prepared))
        with selected_luna.SelectedLunaExtractor(s, prepared, tmp_path, local_text_result=result) as reader:
            prompt = reader.prompts["bases_service"] + analysis._document_set_instruction(prepared["source_document_manifest"], context(prepared))
            actual = provider_instruction(prompt, reader._response_schema("bases_service"))
            assert canonical_hash(actual) == inputs["prompt_hash"]
            assert reader._prompt_version("bases_service").endswith("prompt_v2")
            assert reader._schema_version("bases_service").endswith("schema_v1")
            assert reader._contents(data, mime="application/pdf") == [{"type": "input_text", "text": result["context_text"]}]
            client.generate.assert_not_called()
    assert "REVISION_VISUAL=prod2_ocr_text_layout_review_v2" in prompt


@pytest.mark.parametrize("bad", [None, "unsupported", True])
def test_visual_review_policy_rejects_unknown_values_before_model(bad):
    with pytest.raises(ValueError):
        input_policy.get_input_policy({input_policy.INPUT_POLICY_KEY: input_policy.EMBEDDING_RETRIEVED_TEXT_POLICY,
            input_policy.VISUAL_REVIEW_POLICY_KEY: bad})
    with pytest.raises(ValueError):
        input_policy.get_input_policy({input_policy.INPUT_POLICY_KEY: input_policy.NATIVE_TEXT_OR_PDF_POLICY,
            input_policy.VISUAL_REVIEW_POLICY_KEY: input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY})


def test_retrieved_text_only_generation_exact_prompt_and_new_identity(sdk, tmp_path):
    prepared, result = retrieved_prepared(tmp_path)
    callback = MagicMock()
    data = prepared["pdf_path"].read_bytes()
    args = {"object_type": "service", "manifest": prepared["source_document_manifest"],
        "document_context": context(prepared)}
    expected = analysis.extraction_request_hash(settings(), data, **args)
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
            callback, local_text_result=result) as reader:
        bundle = analysis.extract_document(settings(), data, **args, extractor=reader)
        call = sdk.client.models.generate_content.call_args.kwargs
        assert callback.call_count == sdk.client.models.generate_content.call_count == 1
        assert len(call["contents"]) == 1
        assert call["contents"][0].text == result["context_text"]
        assert call["contents"][0].inline_data is None and call["contents"][0].file_data is None
        assert "evidence_citations" in call["config"].response_schema.model_json_schema()["properties"]
        assert "NO el texto completo" in call["config"].system_instruction
        assert canonical_hash(call["config"].system_instruction) == bundle["prompt_hash"]
        sdk.client.files.upload.assert_not_called()
    assert bundle["cache_key"] == "prod2:llm1:" + expected
    assert bundle["prompt_version"] == "prod2_qualification_retrieved_bases_service_prompt_v1"
    assert bundle["schema_version"] == "prod2_qualification_retrieved_bases_service_schema_v1"
    assert bundle["requires_human_review"]
    assert bundle["raw_extraction_json"]["retrieval_context_text"] == result["context_text"]
    assert bundle["raw_extraction_json"]["retrieval_evidence_audit"]["claims_exhaustive_read"] is False


@pytest.mark.parametrize("mutation", ["file", "block_text", "source", "proof", "missing_proof", "route"])
def test_changed_retrieval_stops_before_provider(sdk, tmp_path, mutation):
    prepared, result = retrieved_prepared(tmp_path)
    callback = MagicMock()
    if mutation == "file": result["context_text_path"].write_text("changed")
    if mutation == "block_text": result["selected_blocks"][0]["text"] += " forged"
    if mutation == "source": result["selected_blocks"][0]["locations"][0]["source_sha256"] = "f" * 64
    if mutation == "proof": result["durable_proof"]["context_sha256"] = "f" * 64
    if mutation == "missing_proof": result.pop("durable_proof")
    if mutation == "route": prepared["llm1_input"]["route"] = "pdf"
    with pytest.raises((ValueError, selected_extractor.SelectedTransportError)):
        with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
                callback, local_text_result=result) as reader:
            reader.extract(prepared["pdf_path"].read_bytes())
    callback.assert_not_called()
    sdk.client.models.generate_content.assert_not_called()
    sdk.client.files.upload.assert_not_called()


def test_partial_quote_false_literal_guard_preserves_paid_participation(sdk, tmp_path):
    prepared, result = retrieved_prepared(tmp_path)
    raw = {"summary": {"descripcion_corta": "Software"}, "participation": {
        "consorcio": {"status": "prohibited", "clause": "Consorcio prohibido.", "page": 1}},
        "evidence_citations": [{"field_path": "participation.consorcio", "block_id": "block-0001",
            "quote": "Consorcio prohibido.", "page": 1}]}
    sdk.client.models.generate_content.return_value = response(text=json.dumps(raw))
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
            local_text_result=result) as reader:
        bundle = analysis.extract_document(settings(), prepared["pdf_path"].read_bytes(),
            object_type="service", manifest=prepared["source_document_manifest"],
            document_context=context(prepared), extractor=reader)
    audit = bundle["raw_extraction_json"]["retrieval_evidence_audit"]
    assert not audit["citations_valid"] and audit["paid_participation"] == raw["participation"]
    assert bundle["raw_extraction_json"]["participation"]["consorcio"]["status"] == "not_identified"
    assert sdk.client.models.generate_content.call_count == 1
    receipts = list(tmp_path.glob("gemini-selected-*.response.private.json"))
    assert len(receipts) == 1 and json.loads(receipts[0].read_text())["response_text"] == json.dumps(raw)


def test_literal_clause_with_source_page_is_verified_but_still_partial(tmp_path):
    prepared, result = retrieved_prepared(tmp_path)
    raw = {"participation": {"consorcio": {"status": "permitted", "clause": "Consorcio permitido.", "page": 1}},
        "evidence_citations": [{"field_path": "participation.consorcio", "block_id": "block-0001",
            "quote": "Consorcio permitido.", "page": 1}]}
    value = input_policy.validate_retrieved_evidence(raw, prepared["llm1_input"])
    assert value["citations_valid"] and value["requires_human_review"] and value["projection_pending"]
    raw["participation"]["consorcio"]["page"] = 2
    assert not input_policy.validate_retrieved_evidence(raw, prepared["llm1_input"])["citations_valid"]


def test_fresh_readback_time_does_not_create_another_paid_identity(tmp_path):
    prepared, result = retrieved_prepared(tmp_path)
    first = deepcopy(prepared["llm1_input"])
    result["durable_proof"]["verified_at"] = "2026-10-09T22:00:00Z"
    assert input_policy.input_snapshot(result, prepared) == first
    assert "verified_at" not in first["durable_proof"]


@pytest.mark.parametrize("mutation", [None, "corpus_id", "retrieval_payload_sha256", "chunk_count"])
def test_completion_compares_actual_durable_rows_and_literal_input(monkeypatch, tmp_path, mutation):
    from buenapro_worker.prod2 import embedding_store, llm1_completion
    prepared, _ = retrieved_prepared(tmp_path)
    proof = prepared["llm1_input"]
    actual = deepcopy(proof["durable_proof"])
    actual["verified_at"] = "2026-10-09T22:00:00Z"
    if mutation == "retrieval_payload_sha256": actual[mutation] = "f" * 64
    elif mutation: actual[mutation] += 1
    verify = MagicMock(return_value=actual)
    monkeypatch.setattr(embedding_store, "verified_corpus_retrieval", verify)
    conn = object()
    if mutation:
        with pytest.raises(llm1_completion.Llm1CompletionError):
            llm1_completion._verify_retrieved_database(conn, proof)
    else:
        llm1_completion._verify_retrieved_database(conn, proof)
    verify.assert_called_once_with(conn, proof["corpus_hash"], proof["retrieval_hash"],
        allow_transaction=True, expected_input=proof)


def test_luna_retrieved_transport_has_no_pdf_ceiling(monkeypatch, tmp_path):
    prepared, result = retrieved_prepared(tmp_path)
    client = MagicMock()
    monkeypatch.setattr(selected_luna, "LunaResponsesClient", lambda _: client)
    prepared["chunks"][0]["bytes"] = 51_000_000
    # The original PDF integrity guard still applies. Simulate a verified large
    # source preparation and prove only the transport ceiling is bypassed.
    monkeypatch.setattr(selected_luna.SelectedLunaExtractor, "_validate_prepared", lambda _: None)
    s = settings()
    s.openai_api_key = "synthetic-key"
    with selected_luna.SelectedLunaExtractor(s, prepared, tmp_path, local_text_result=result) as reader:
        parts = reader._contents(prepared["pdf_path"].read_bytes(), mime="application/pdf")
    assert parts == [{"type": "input_text", "text": result["context_text"]}]
    client.generate.assert_not_called()


def retrieved_receipt(monkeypatch, tmp_path):
    conn, extraction = focused_receipt(monkeypatch)
    selection = extraction["selection_context"]
    selection["llm1_input_policy"] = input_policy.EMBEDDING_RETRIEVED_TEXT_POLICY
    manifest = extraction["raw_extraction_json"]["source_document_manifest"]
    prepared = {"analysis_sha256": extraction["analysis_sha256"], "page_count": manifest[-1]["end_page"],
        "source_document_manifest": manifest, "selection_context": selection}
    local = result_for(prepared, tmp_path / "context.private.txt")
    extraction["llm1_input"] = input_policy.input_snapshot(local, prepared)
    extraction.update(prompt_version=input_policy.prompt_version_for_input("bases_service", selection),
        schema_version=input_policy.schema_version_for_input("bases_service", selection))
    raw = extraction["raw_extraction_json"]
    raw.update(llm1_input=deepcopy(extraction["llm1_input"]), retrieval_context_text=local["context_text"])
    raw["retrieval_evidence_audit"] = input_policy.validate_retrieved_evidence(raw, extraction["llm1_input"])
    raw["retrieval_evidence_audit"]["paid_participation"] = deepcopy(raw.get("participation", {}))
    for error in raw["retrieval_evidence_audit"]["errors"]:
        if error["kind"] == "unverified_participation_clause":
            raw["participation"][error["field"]] = {"status": "not_identified", "clause": None, "page": None, "conditions": []}
    sync(extraction, conn)
    from buenapro_worker.prod2 import llm1_completion
    monkeypatch.setattr(llm1_completion, "_verify_retrieved_database", MagicMock())
    return conn, extraction


@pytest.mark.parametrize("previous_source", ["seace_prod2", "seace_prod4", None])
def test_retrieved_persistence_preserves_all_canonical_fields(monkeypatch, tmp_path, previous_source):
    conn, extraction = retrieved_receipt(monkeypatch, tmp_path)
    canonical = conn.canonical
    canonical.update(consortium_status="conditional", subcontracting_status="prohibited", updated_at=NOW)
    canonical["participation_terms_json"] = ({"source": previous_source,
        "document_sha256": "e" * 64, "extraction_id": 41} if previous_source else {})
    before = deepcopy(canonical)
    saved = finish(conn, extraction)
    assert saved["projection_pending"] and saved["canonical_participation_preserved"]
    assert saved["preservation_policy_version"] == "prod2_retrieved_participation_preserved_v1"
    assert conn.canonical == before and not conn.updates and len(conn.persist_calls) == 1


def test_visual_review_v2_persistence_keeps_foreign_canonical_and_timestamp(monkeypatch, tmp_path):
    conn, extraction = retrieved_receipt(monkeypatch, tmp_path)
    selection = extraction["selection_context"]
    selection[input_policy.VISUAL_REVIEW_POLICY_KEY] = input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY
    extraction["prompt_version"] = input_policy.prompt_version_for_input("bases_service", selection)
    sync(extraction, conn)
    conn.canonical.update(consortium_status="conditional", subcontracting_status="prohibited", updated_at=NOW,
        participation_terms_json={"source": "seace_prod4", "document_sha256": "e" * 64, "extraction_id": 41})
    before = deepcopy(conn.canonical)
    saved = finish(conn, extraction)
    assert saved["canonical_participation_preserved"] and saved["projection_pending"]
    assert conn.canonical == before and not conn.updates and len(conn.persist_calls) == 1


@pytest.mark.parametrize("mutation", ["text", "audit", "needs_review"])
def test_bad_retrieval_receipt_cannot_persist(monkeypatch, tmp_path, mutation):
    conn, extraction = retrieved_receipt(monkeypatch, tmp_path)
    if mutation == "text": extraction["raw_extraction_json"]["retrieval_context_text"] += "changed"
    if mutation == "audit": extraction["raw_extraction_json"]["retrieval_evidence_audit"]["citations_valid"] = True
    if mutation == "needs_review": extraction["requires_human_review"] = False
    from buenapro_worker.prod2.llm1_completion import Llm1CompletionError
    with pytest.raises(Llm1CompletionError): finish(conn, extraction)
    assert not conn.updates and not conn.persist_calls
