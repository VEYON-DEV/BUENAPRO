"""V5 skips unusable pages without disguising gaps or buying a second read."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import (
    analysis, embedding_client, embedding_corpus, embedding_retrieval,
    embedding_runtime, embedding_store, input_policy, pdf_preparation,
    selected_extractor, selected_luna,
)
from buenapro_worker.prod2.extraction_policy import QUALIFICATION_FOCUS_POLICY
from buenapro_worker.prod2.luna_policy import luna_settings
from buenapro_worker.prod2.partial_reading import (
    PAGE_FAILURE_POLICY, SOURCE_COVERAGE_POLICY, SOURCE_COVERAGE_POLICY_KEY,
    compact_source_capture,
)
from buenapro_worker.prod2.selected_control import RunLimits
from test_prod2_embedding_runtime import options, runner
from test_prod2_input_integration import context
from test_prod2_llm1_completion import finish, NOW
from test_prod2_local_text import pdf
from test_prod2_retrieved_input_transport import result_for, retrieved_receipt
from test_prod2_selected_completion import sync
from test_prod2_selected_extractor import sdk, settings
from test_prod2_selected_luna import body as luna_body


ROOT = Path(__file__).resolve().parents[3]


def partial_config():
    config = embedding_runtime.load_config(
        ROOT / "docs/sources/prod2-embedding-reading.runtime.v4.json")
    config["corpus"]["page_failure_policy"] = PAGE_FAILURE_POLICY
    config["reading"]["source_coverage_policy"] = SOURCE_COVERAGE_POLICY
    return config


def partial_prepared(tmp_path):
    """Three real source pages, only page one appears in the retrieved context."""
    data = pdf([
        ("Experiencia del postor: 10000 soles. Personal tecnico. Consorcio permitido.", 0),
        ("Pagina omitida por una lectura local no confiable.", 0),
        ("Entregable: aplicacion en 90 dias.", 0),
    ])
    original = tmp_path / "original.pdf"
    original.write_bytes(data)
    code = "00000000-0000-4000-8000-000000000001"
    source = {"candidate_id": "loose:" + code, "document_uuid": code,
        "source_kind": "loose", "filename": "bases.pdf", "type": "Bases",
        "stage": "Convocatoria", "published_at": "06/10/2026 12:00",
        "source_url": "https://example.invalid/" + code,
        "original_sha256": hashlib.sha256(data).hexdigest()}
    selection = {"official_inventory": [source], "llm1_scope_policy": QUALIFICATION_FOCUS_POLICY,
        input_policy.INPUT_POLICY_KEY: input_policy.EMBEDDING_RETRIEVED_TEXT_POLICY,
        input_policy.VISUAL_REVIEW_POLICY_KEY: input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY,
        input_policy.RETRIEVED_TRANSPORT_POLICY_KEY: input_policy.COMPACT_RETRIEVED_TRANSPORT_POLICY,
        SOURCE_COVERAGE_POLICY_KEY: SOURCE_COVERAGE_POLICY}
    prepared = pdf_preparation.prepare_selected_document_set(tmp_path, [source],
        {source["candidate_id"]: original}, selection, optimize_lossless=False)
    result = result_for(prepared, tmp_path / "context.private.txt")
    result.update(indexed_complete=False, needs_review=True,
        reasons=["corpus_page_coverage_unresolved"], source_capture={
            "policy_version": SOURCE_COVERAGE_POLICY, "total_pages": 3,
            "captured_pages": [1, 3], "omitted_pages": [{"global_page": 2,
                "reasons": ["ocr_low_confidence"], "source_pages": [{
                    "candidate_id": source["candidate_id"], "document_uuid": code,
                    "source_page": 2}]}], "text_coverage_complete": False})
    result["citations"] = [{"block_id": block["block_id"],
        "global_page": block["global_page"], **location}
        for block in result["selected_blocks"] for location in block["locations"]]
    marker = "[COBERTURA PARCIAL " + json.dumps(
        compact_source_capture(result["source_capture"]), sort_keys=True) + "]\n"
    result["context_text"] = marker + result["context_text"]
    result["context_text_path"].write_text(result["context_text"])
    result["context_sha256"] = hashlib.sha256(result["context_text"].encode()).hexdigest()
    result["context_bytes"] = len(result["context_text"].encode())
    result["durable_proof"]["context_sha256"] = result["context_sha256"]
    prepared["retrieval_input_result"] = result
    return input_policy.prepare_input(prepared, tmp_path)


def validate(prepared, proof):
    return input_policy.validate_input_snapshot(prepared["selection_context"], proof,
        prepared["analysis_sha256"], prepared["source_document_manifest"], prepared["page_count"])


def test_partial_proof_keeps_every_source_page_and_omits_no_provenance(tmp_path):
    prepared, result = partial_prepared(tmp_path)
    proof = prepared["llm1_input"]
    assert proof["source_capture"] == result["source_capture"]
    assert proof["source_capture"]["captured_pages"] == [1, 3]
    assert proof["source_capture"]["omitted_pages"][0]["source_pages"][0]["source_page"] == 2
    assert proof["indexed_complete"] is False and proof["needs_review"] is True
    assert input_policy.read_verified_retrieved_text(result, prepared) == result["context_text"]


@pytest.mark.parametrize("mutation", [
    "total", "captured_omitted_overlap", "missing_page", "duplicate_page", "missing_reason",
    "source_page", "source_uuid", "false_complete", "missing_review", "missing_gap_reason",
    "missing_capture", "block_on_omitted_page", "citation_on_omitted_page",
])
def test_partial_proof_rejects_coverage_and_citation_tamper(tmp_path, mutation):
    prepared, _ = partial_prepared(tmp_path)
    proof = deepcopy(prepared["llm1_input"])
    capture = proof["source_capture"]
    if mutation == "total": capture["total_pages"] = 4
    elif mutation == "captured_omitted_overlap": capture["captured_pages"] = [1, 2, 3]
    elif mutation == "missing_page": capture["captured_pages"] = [1]
    elif mutation == "duplicate_page": capture["captured_pages"] = [1, 1, 3]
    elif mutation == "missing_reason": capture["omitted_pages"][0]["reasons"] = []
    elif mutation == "source_page": capture["omitted_pages"][0]["source_pages"][0]["source_page"] = 3
    elif mutation == "source_uuid": capture["omitted_pages"][0]["source_pages"][0]["document_uuid"] = "other"
    elif mutation == "false_complete": proof["indexed_complete"] = capture["text_coverage_complete"] = True
    elif mutation == "missing_review": proof["needs_review"] = False
    elif mutation == "missing_gap_reason": proof["reasons"] = []
    elif mutation == "missing_capture": proof.pop("source_capture")
    elif mutation == "block_on_omitted_page":
        proof["selected_blocks"][0].update(global_page=2, page_start=2, page_end=2)
        proof["selected_blocks"][0]["locations"][0]["source_page"] = 2
    elif mutation == "citation_on_omitted_page":
        proof["citations"][0].update(global_page=2, source_page=2)
    with pytest.raises(ValueError):
        validate(prepared, proof)


def test_partial_proof_cannot_be_relabelled_as_existing_v4(tmp_path):
    prepared, _ = partial_prepared(tmp_path)
    prepared["selection_context"].pop(SOURCE_COVERAGE_POLICY_KEY)
    with pytest.raises(ValueError):
        validate(prepared, prepared["llm1_input"])


def test_partial_metadata_reports_compact_gaps_without_mutating_full_proof(tmp_path):
    prepared, _ = partial_prepared(tmp_path)
    proof = deepcopy(prepared["llm1_input"])
    before = deepcopy(proof)
    metadata = input_policy.compact_retrieved_metadata(
        prepared["selection_context"], proof, prepared["source_document_manifest"])
    summary = metadata["source_capture"]
    assert summary["captured_page_count"] == 2 and summary["omitted_page_count"] == 1
    assert summary["omitted_global_page_ranges"] == [[2, 2]]
    assert summary["omission_reasons"] == ["ocr_low_confidence"]
    assert summary["absence_in_partial_text_means_unknown"] is True
    assert "source_pages" not in summary and "captured_pages" not in summary
    assert proof == before and "source_pages" in proof["source_capture"]["omitted_pages"][0]


@pytest.mark.parametrize("provider", ["gemini", "luna"])
def test_v5_both_providers_send_only_partial_text_and_preserve_review(sdk, monkeypatch, tmp_path, provider):
    prepared, result = partial_prepared(tmp_path)
    data = prepared["pdf_path"].read_bytes()
    args = {"object_type": "service", "manifest": prepared["source_document_manifest"],
        "document_context": context(prepared)}
    if provider == "gemini":
        s = settings()
        with selected_extractor.SelectedGeminiExtractor(s, prepared, tmp_path,
                local_text_result=result) as reader:
            bundle = analysis.extract_document(s, data, **args, extractor=reader)
        call = sdk.client.models.generate_content.call_args.kwargs
        assert len(call["contents"]) == 1 and call["contents"][0].text == result["context_text"]
        assert call["contents"][0].inline_data is None and call["contents"][0].file_data is None
        sdk.client.files.upload.assert_not_called()
        assert sdk.client.models.generate_content.call_count == 1
        instructions = call["config"].system_instruction
    else:
        client = MagicMock()
        def generate(**kwargs):
            import httpx
            response = luna_body()
            kwargs["on_response"](httpx.Response(200, json=response))
            return response
        client.generate.side_effect = generate
        monkeypatch.setattr(selected_luna, "LunaResponsesClient", lambda _: client)
        s = luna_settings(settings())
        s.openai_api_key = "synthetic-key"
        with selected_luna.SelectedLunaExtractor(s, prepared, tmp_path,
                local_text_result=result) as reader:
            bundle = analysis.extract_document(s, data, **args, extractor=reader)
        call = client.generate.call_args.kwargs
        assert call["contents"] == [{"type": "input_text", "text": result["context_text"]}]
        assert client.generate.call_count == 1
        instructions = call["instructions"]
    assert "COBERTURA_FUENTE=" + SOURCE_COVERAGE_POLICY in instructions
    assert "RECUPERACION_DE_PROCEDENCIA_COMPACTA=" in instructions
    assert '"omitted_global_page_ranges":[[2,2]]' in instructions
    assert bundle["prompt_version"].endswith("_prompt_v5")
    assert bundle["llm1_input"] == prepared["llm1_input"]
    assert bundle["requires_human_review"] is True
    assert bundle["raw_extraction_json"]["retrieval_context_text"] == result["context_text"]
    assert canonical_hash(instructions) == bundle["prompt_hash"]


def test_new_default_v5_and_frozen_v4_keep_separate_identities(tmp_path):
    new_folder = tmp_path / "new"
    new_folder.mkdir()
    new = runner.plan(options(new_folder))
    assert new[SOURCE_COVERAGE_POLICY_KEY] == SOURCE_COVERAGE_POLICY
    assert new["embedding_reading"]["config"]["corpus"]["page_failure_policy"] == PAGE_FAILURE_POLICY
    embedding_runtime.validate_frozen_config(new)
    old_folder = tmp_path / "old"
    old_folder.mkdir()
    old = runner.plan(options(old_folder, "--embedding-config",
        str(ROOT / "docs/sources/prod2-embedding-reading.runtime.v4.json")))
    assert SOURCE_COVERAGE_POLICY_KEY not in old
    manifest = {**old, "requested": old, "run_id": "synthetic", "commercial_profiles": [],
        "gemini_model": "gemini-3.1-flash-lite", "keyword_tracking": {"sweep_id": "synthetic"}}
    runner.private_write(old_folder / "manifest.private.json", manifest)
    assert runner.plan(options(old_folder)) == old
    with pytest.raises(ValueError, match="new folder"):
        runner.plan(options(old_folder, "--embedding-config",
            str(ROOT / "docs/sources/prod2-embedding-reading.runtime.v5.json")))


def runtime_mocks(monkeypatch, prepared, result, *, empty=False):
    corpus = {"corpus_hash": result["corpus_hash"], "coverage_complete": False,
        "unresolved_pages": [2], "chunks": [] if empty else [{"text": result["selected_blocks"][0]["text"]}],
        "config": {"page_failure_policy": PAGE_FAILURE_POLICY}}
    build = MagicMock(return_value=corpus)
    monkeypatch.setattr(embedding_corpus, "build_corpus", build)
    save_corpus = MagicMock(return_value={"pg_verified": True})
    monkeypatch.setattr(embedding_store, "save_corpus", save_corpus)
    store = MagicMock()
    store.ensure_vectors.return_value = {"vectors": [[1., 0.]] * 11,
        "known_cost_usd": 0, "cached_count": 11, "new_token_count": 0, "attempt_ids": []}
    factory = MagicMock(return_value=store)
    monkeypatch.setattr(embedding_store, "EmbeddingStore", factory)
    client = MagicMock()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(embedding_client, "EmbeddingClient", client_factory)
    retrieve = MagicMock(return_value=result)
    monkeypatch.setattr(embedding_retrieval, "retrieve_context", retrieve)
    save_retrieval = MagicMock(return_value=result["durable_proof"])
    monkeypatch.setattr(embedding_store, "save_retrieval", save_retrieval)
    return SimpleNamespace(build=build, save_corpus=save_corpus, store=store,
        factory=factory, client=client, client_factory=client_factory,
        retrieve=retrieve, save_retrieval=save_retrieval)


def run_runtime(prepared, folder, config):
    folder.mkdir()
    limits = RunLimits(max_accepted=10, max_candidates=10, max_jev_calls=10,
        max_llm1_calls=10, max_embedding_calls=10)
    return embedding_runtime.prepare_retrieved_input(object(),
        SimpleNamespace(openai_api_key="synthetic-key"), prepared, folder, limits,
        config, convocation_id=1)


def test_v5_runtime_allows_incomplete_source_using_only_usable_chunks(monkeypatch, tmp_path):
    prepared, result = partial_prepared(tmp_path)
    mocks = runtime_mocks(monkeypatch, prepared, result)
    output = run_runtime(prepared, tmp_path / "runtime", partial_config())
    assert output["retrieval_input_result"]["source_capture"] == result["source_capture"]
    sent = mocks.store.ensure_vectors.call_args.args[0]
    assert sent[0] == result["selected_blocks"][0]["text"] and len(sent) == 11
    assert "Pagina omitida" not in " ".join(sent)
    mocks.client.close.assert_called_once()
    mocks.save_retrieval.assert_called_once()


def test_existing_v4_runtime_still_blocks_incomplete_before_providers(monkeypatch, tmp_path):
    prepared, result = partial_prepared(tmp_path)
    prepared["selection_context"].pop(SOURCE_COVERAGE_POLICY_KEY)
    mocks = runtime_mocks(monkeypatch, prepared, result)
    config = embedding_runtime.load_config(ROOT / "docs/sources/prod2-embedding-reading.runtime.v4.json")
    with pytest.raises(embedding_runtime.EmbeddingPreparationReview, match="Complete usable"):
        run_runtime(prepared, tmp_path / "runtime", config)
    mocks.client_factory.assert_not_called()
    mocks.factory.assert_not_called()
    mocks.retrieve.assert_not_called()


@pytest.mark.parametrize("mutation", ["source_policy", "failure_policy", "selection_policy"])
def test_v5_runtime_rejects_unpaired_policy_before_corpus_or_providers(monkeypatch, tmp_path, mutation):
    prepared, result = partial_prepared(tmp_path)
    mocks = runtime_mocks(monkeypatch, prepared, result)
    config = partial_config()
    if mutation == "source_policy": config["reading"].pop("source_coverage_policy")
    elif mutation == "failure_policy": config["corpus"].pop("page_failure_policy")
    else: prepared["selection_context"].pop(SOURCE_COVERAGE_POLICY_KEY)
    with pytest.raises(ValueError, match="Partial source"):
        run_runtime(prepared, tmp_path / "runtime", config)
    mocks.build.assert_not_called()
    mocks.client_factory.assert_not_called()
    mocks.factory.assert_not_called()


def test_v5_runtime_no_usable_text_still_stops_without_paid_fallback(monkeypatch, tmp_path):
    prepared, result = partial_prepared(tmp_path)
    mocks = runtime_mocks(monkeypatch, prepared, result, empty=True)
    folder = tmp_path / "runtime"
    with pytest.raises(embedding_runtime.EmbeddingPreparationReview, match="no usable text"):
        run_runtime(prepared, folder, partial_config())
    mocks.save_corpus.assert_not_called()
    mocks.client_factory.assert_not_called()
    mocks.factory.assert_not_called()
    mocks.retrieve.assert_not_called()
    review = json.loads((folder / "embedding-preparation-review.private.json").read_text())
    assert review["provider_calls"] == 0 and review["full_pdf_fallback"] is False


def test_v5_completion_still_preserves_all_canonical_participation(monkeypatch, tmp_path):
    conn, extraction = retrieved_receipt(monkeypatch, tmp_path)
    selection = extraction["selection_context"]
    selection.update({input_policy.VISUAL_REVIEW_POLICY_KEY: input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY,
        input_policy.RETRIEVED_TRANSPORT_POLICY_KEY: input_policy.COMPACT_RETRIEVED_TRANSPORT_POLICY,
        SOURCE_COVERAGE_POLICY_KEY: SOURCE_COVERAGE_POLICY})
    proof = extraction["llm1_input"]
    proof["source_capture"] = {"policy_version": SOURCE_COVERAGE_POLICY,
        "total_pages": proof["page_count"], "captured_pages": list(range(1, proof["page_count"] + 1)),
        "omitted_pages": [], "text_coverage_complete": True}
    proof["citations"] = [{"block_id": block["block_id"], "global_page": block["global_page"], **loc}
        for block in proof["selected_blocks"] for loc in block["locations"]]
    extraction["raw_extraction_json"]["llm1_input"] = deepcopy(proof)
    extraction["prompt_version"] = input_policy.prompt_version_for_input("bases_service", selection)
    sync(extraction, conn)
    conn.canonical.update(consortium_status="conditional", subcontracting_status="prohibited",
        updated_at=NOW, participation_terms_json={"source": "seace_prod4", "extraction_id": 41,
            "document_sha256": "e" * 64})
    before = deepcopy(conn.canonical)
    saved = finish(conn, extraction)
    assert saved["canonical_participation_preserved"] and saved["projection_pending"]
    assert conn.canonical == before and not conn.updates and len(conn.persist_calls) == 1
