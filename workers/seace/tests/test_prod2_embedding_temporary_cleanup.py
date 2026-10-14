"""Embedding derivatives only disappear after matching fresh durable readback."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_prod2_storage_policy import completed
from test_prod2_temporary_storage import case, clean, write_json
from test_prod2_embedding_corpus_retrieval import native_row, query, good_ocr, scan_row, V2

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import embedding_corpus, embedding_retrieval, embedding_store
from buenapro_worker.prod2 import storage_policy, temporary_storage


def proof(corpus_hash="e" * 64, retrieval_hash="f" * 64, context_sha="9" * 64,
          corpus_payload="7" * 64):
    return {"version": "prod2_embedding_durable_proof_v1", "corpus_id": 1, "retrieval_id": 2,
        "corpus_hash": corpus_hash, "retrieval_hash": retrieval_hash, "context_sha256": context_sha,
        "corpus_payload_sha256": corpus_payload, "retrieval_payload_sha256": "8" * 64,
        "chunk_count": 1, "blocks_persisted": True, "vectors_persisted": True, "pg_verified": True}


def retrieved_completed(completed):
    folder, outcome, conn = completed
    value = {"policy_version": "prod2_embedding_retrieved_text_v1", "corpus_hash": "e" * 64,
        "retrieval_hash": "f" * 64, "context_sha256": "9" * 64, "analysis_sha256": "6" * 64,
        "source_manifest_hash": canonical_hash(outcome["source_document_manifest"]), "durable_proof": proof()}
    outcome["llm1_input"] = deepcopy(value)
    bundle = conn.row["result_json"]
    bundle["analysis_sha256"] = value["analysis_sha256"]
    bundle["raw_extraction_json"]["llm1_input"] = deepcopy(value)
    outcome["llm1_receipt_hash"] = canonical_hash(bundle)
    write_json(folder / "outcome.private.json", outcome)
    return folder, outcome, conn


def test_storage_readback_invokes_real_adapter_contract_with_exact_sent_input(completed, monkeypatch):
    folder, outcome, conn = retrieved_completed(completed)
    calls = []
    def verified(actual_conn, corpus_hash, retrieval_hash, *, allow_transaction, expected_input):
        calls.append((actual_conn, corpus_hash, retrieval_hash, allow_transaction, expected_input))
        return {**proof(), "verified_at": "2026-10-08T23:00:00+00:00"}
    monkeypatch.setattr(embedding_store, "verified_corpus_retrieval", verified)
    result = storage_policy.verify_completed_storage(conn, folder, outcome)
    assert calls == [(conn, "e" * 64, "f" * 64, True, outcome["llm1_input"])]
    assert result["embedding_durable_proof"]["pg_verified"] is True
    assert all(statement.lstrip().startswith("SELECT") for statement in conn.statements)


@pytest.mark.parametrize("failure", ["input_changed", "source_changed", "fresh_proof_changed", "durable_missing"])
def test_inconsistent_or_missing_embedding_persistence_blocks_cleanup(completed, monkeypatch, failure):
    folder, outcome, conn = retrieved_completed(completed)
    def verified(*args, **kwargs):
        if failure == "durable_missing":
            raise embedding_store.EmbeddingPersistenceError("synthetic missing PG chunk")
        return {**proof(), "retrieval_hash": "1" * 64, "verified_at": "now"}
    monkeypatch.setattr(embedding_store, "verified_corpus_retrieval", verified)
    if failure == "input_changed":
        outcome["llm1_input"]["corpus_hash"] = "1" * 64
    elif failure == "source_changed":
        outcome["llm1_input"]["source_manifest_hash"] = "1" * 64
        conn.row["result_json"]["raw_extraction_json"]["llm1_input"] = deepcopy(outcome["llm1_input"])
        outcome["llm1_receipt_hash"] = canonical_hash(conn.row["result_json"])
    write_json(folder / "outcome.private.json", outcome)
    with pytest.raises((ValueError, embedding_store.EmbeddingPersistenceError)):
        storage_policy.verify_completed_storage(conn, folder, outcome)
    assert (folder / "outcome.private.json").exists()


def test_legacy_storage_does_not_require_embeddings_schema(completed, monkeypatch):
    def unwanted(*args, **kwargs):
        pytest.fail("Legacy cleanup accessed embeddings")
    monkeypatch.setattr(embedding_store, "verified_corpus_retrieval", unwanted)
    folder, outcome, conn = completed
    result = storage_policy.verify_completed_storage(conn, folder, outcome)
    assert "embedding_durable_proof" not in result


def embedding_case(case, monkeypatch, *, ocr=False):
    folder, workspace, verification, private, work = case
    pin = json.loads((private / "manifest.private.json").read_text())
    manifest = [{"candidate_id": "loose:00000000-0000-4000-8000-000000000001",
        "document_uuid": "00000000-0000-4000-8000-000000000001", "original_sha256": "a" * 64,
        "analysis_sha256": "b" * 64, "start_page": 1, "end_page": 1, "page_offset": 0}]
    prepared = {"pdf_path": private / "analysis.pdf", "analysis_sha256": pin["analysis_sha256"],
        "analysis_bytes": pin["analysis_bytes"], "page_count": 1, "source_document_manifest": manifest}
    monkeypatch.setattr(embedding_corpus, "_extract_native", lambda *_: [scan_row(image_ratio=.4) if ocr else
        native_row("Objeto: contratación. Subcontratación permitida hasta 30%.")])
    if ocr:
        monkeypatch.setattr(embedding_corpus, "_ocr_page", lambda *_: good_ocr())
    monkeypatch.setattr(embedding_corpus, "_versions", lambda *_: {"offline": True})
    corpus = embedding_corpus.build_corpus(prepared, folder, config=V2 if ocr else None)
    corpus.pop("cache_hit")
    retrieval = embedding_retrieval.retrieve_context(corpus, [[1., 0.] for _ in corpus["chunks"]],
        {"subcontracting": [1., 0.]}, config={"queries": [query()]})
    durable = proof(corpus["corpus_hash"], retrieval["retrieval_hash"], retrieval["context_sha256"],
                    canonical_hash(corpus))
    durable["chunk_count"] = len(corpus["chunks"])
    value = {"policy_version": "prod2_embedding_retrieved_text_v1", "analysis_sha256": corpus["analysis_sha256"],
        "source_manifest_hash": corpus["source_manifest_hash"], "corpus_hash": corpus["corpus_hash"],
        "retrieval_hash": retrieval["retrieval_hash"], "context_sha256": retrieval["context_sha256"],
        "context_bytes": retrieval["context_bytes"], "durable_proof": durable}
    outcome = json.loads((folder / "outcome.private.json").read_text())
    outcome["llm1_input"] = value
    write_json(folder / "outcome.private.json", outcome)
    verification["embedding_durable_proof"] = {**durable, "verified_at": "2026-10-08T23:00:00+00:00"}
    context = folder / ("embedding-context-" + retrieval["retrieval_hash"])
    context.mkdir()
    (context / "context.private.txt").write_text(retrieval["context_text"])
    write_json(context / "manifest.private.json", {"analysis_sha256": corpus["analysis_sha256"],
        "corpus_hash": corpus["corpus_hash"], "retrieval_hash": retrieval["retrieval_hash"],
        "context_sha256": retrieval["context_sha256"], "context_bytes": retrieval["context_bytes"],
        "durable_proof": verification["embedding_durable_proof"]})
    return corpus, retrieval, next(folder.rglob("corpus.private.json")), context / "context.private.txt"


def test_verified_embedding_cleanup_deletes_fulltext_only_and_replays_without_pdf(case, monkeypatch):
    folder, _, verification, *_ = case
    corpus, retrieval, corpus_path, context_path = embedding_case(case, monkeypatch)
    corpus_bytes, context_bytes = corpus_path.stat().st_size, context_path.stat().st_size
    compact = (context_path.parent / "manifest.private.json").read_bytes()
    before_responses = {path: path.read_bytes() for path in folder.glob("*.response.private.json")}
    result = clean(case)
    assert result["deleted_files"] == 6 and result["deleted_bytes"] >= corpus_bytes + context_bytes
    assert not corpus_path.exists() and not context_path.exists()
    assert (context_path.parent / "manifest.private.json").read_bytes() == compact
    assert all(path.read_bytes() == value for path, value in before_responses.items())
    replay = clean(case)
    assert replay["deleted_bytes"] == 0 and replay["already_absent_files"] == 6


@pytest.mark.parametrize("mutation", ["proof_absent", "proof_changed", "corpus_mutated", "context_mutated", "context_manifest_changed"])
def test_unverified_embedding_derivatives_never_trigger_partial_deletion(case, monkeypatch, mutation):
    folder, _, verification, *_ = case
    corpus, retrieval, corpus_path, context_path = embedding_case(case, monkeypatch)
    if mutation == "proof_absent":
        verification.pop("embedding_durable_proof")
    elif mutation == "proof_changed":
        verification["embedding_durable_proof"]["vectors_persisted"] = False
    elif mutation == "corpus_mutated":
        corpus_path.write_text(corpus_path.read_text().replace("hasta 30%", "hasta 90%"))
    elif mutation == "context_mutated":
        context_path.write_text("Requisito cambiado")
    else:
        pin_path = context_path.parent / "manifest.private.json"
        pin = json.loads(pin_path.read_text())
        pin["corpus_hash"] = "1" * 64
        write_json(pin_path, pin)
    with pytest.raises(temporary_storage.TemporaryCleanupError):
        clean(case)
    assert len(list(folder.rglob("*.pdf"))) == 4
    assert context_path.exists() and corpus_path.exists()


@pytest.mark.parametrize("status", ["running", "uncertain", "partial_known"])
def test_pending_or_partially_paid_embedding_attempt_retains_all_inputs(case, monkeypatch, status):
    folder, *_ = case
    _, _, corpus_path, context_path = embedding_case(case, monkeypatch)
    attempt = "00000000-0000-4000-8000-000000000001"
    write_json(folder / ("embedding-" + attempt + ".started.private.json"), {"attempt_id": attempt})
    write_json(folder / ("embedding-" + attempt + ".outcome.private.json"), {
        "attempt_id": attempt, "status": status, "provider_usage_known": status == "partial_known"})
    result = clean(case)
    assert result["status"] == "retained_unreconciled_payment"
    assert corpus_path.exists() and context_path.exists() and len(list(folder.rglob("*.pdf"))) == 4


def test_forged_json_plan_cannot_delete_legacy_case_receipt(case):
    folder, workspace, verification, *_ = case
    target = folder / ("embedding-corpus-" + "a" * 64) / "corpus.private.json"
    write_json(target, {"keep": "no retrieved contract"})
    outcome = json.loads((folder / "outcome.private.json").read_text())
    write_json(folder / "temporary-cleanup-plan.private.json", {"policy_version": temporary_storage.POLICY,
        "convocation_id": verification["convocation_id"], "outcome_hash": canonical_hash(outcome),
        "document_set_hash": outcome["document_set_hash"], "files": [{"path": target.relative_to(folder).as_posix(),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "bytes": target.stat().st_size,
            "artifact_kind": "embedding_corpus_derivative"}]})
    with pytest.raises(temporary_storage.TemporaryCleanupError, match="PostgreSQL proof"):
        clean(case)
    assert target.exists() and len(list(folder.rglob("*.pdf"))) == 4


def test_v2_page_ocr_cache_cleans_only_with_its_complete_durable_corpus(case, monkeypatch):
    folder, *_ = case
    _, _, corpus_path, context_path = embedding_case(case, monkeypatch, ocr=True)
    page_cache, = folder.rglob("ocr-page-0001.private.json")
    result = clean(case)
    assert result["deleted_files"] == 7
    assert not page_cache.exists() and not corpus_path.exists() and not context_path.exists()
    assert list(folder.glob("gemini_extraction-*.response.private.json"))
    again = clean(case)
    assert again["already_absent_files"] == 7 and again["deleted_files"] == 0


def test_v2_mutated_ocr_cache_retains_all_case_inputs(case, monkeypatch):
    folder, *_ = case
    _, _, corpus_path, context_path = embedding_case(case, monkeypatch, ocr=True)
    page_cache, = folder.rglob("ocr-page-0001.private.json")
    cache = json.loads(page_cache.read_text())
    cache["result"]["text"] = "Alteración del contenido después del guardado"
    write_json(page_cache, cache)
    with pytest.raises(temporary_storage.TemporaryCleanupError, match="OCR text derivative"):
        clean(case)
    assert len(list(folder.rglob("*.pdf"))) == 4
    assert page_cache.exists() and corpus_path.exists() and context_path.exists()
