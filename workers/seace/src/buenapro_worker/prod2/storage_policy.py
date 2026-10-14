"""Official references with disposable inputs; historical R2 manifests keep their policy.

This module performs read-only persistence checks. It never calls a provider,
downloads a document, signs a ticket, or deletes any cloud object.
"""
from __future__ import annotations

from pathlib import Path
import re

from buenapro_worker.normalization.facets import canonical_hash
from .batch_runner import read_private
from .selected_payments import audit_summary

OFFICIAL_LINKS_POLICY = "prod2_official_links_temporary_v1"
_SHA = re.compile(r"[0-9a-f]{64}")


def official_links_only(manifest: dict) -> bool:
    if "document_storage_policy" not in manifest:
        return False
    if manifest["document_storage_policy"] != OFFICIAL_LINKS_POLICY:
        raise ValueError("Unsupported document storage policy")
    return True


def verify_completed_storage(conn, folder: Path, outcome: dict) -> dict:
    """Bind the durable local outcome to the paid receipt and stored official evidence.

    A changed/current ficha or extraction requires investigation and retains the
    local inputs. Semantic review is independent: a stored read can still need it.
    """
    if (not official_links_only(outcome) or outcome.get("status") != "llm1_complete"
            or type(outcome.get("convocation_id")) is not int
            or type(outcome.get("extraction_id")) is not int
            or type(outcome.get("llm1_evaluation_id")) is not int):
        raise ValueError("Expected a completed official-reference read")
    for key in ("document_set_hash", "detail_hash", "llm1_request_hash", "llm1_receipt_hash"):
        if not isinstance(outcome.get(key), str) or not _SHA.fullmatch(outcome[key]):
            raise ValueError("Completed storage identity is incomplete")
    if folder.is_symlink() or read_private(folder / "outcome.private.json") != outcome:
        raise ValueError("Durable outcome does not match the completed case")
    conv = outcome["convocation_id"]
    row = conn.execute("""SELECT ex.id_convocatoria,ex.evaluation_id,ex.model,
        ex.raw_extraction_json,ex.facet_count,ex.is_current,ex.quality,
        ev.status AS evaluation_status,ev.kind,ev.request_hash,ev.result_json,
        d.detail_hash,d.document_inventory_complete
        FROM prod2_document_extractions ex JOIN prod2_evaluations ev ON ev.id=ex.evaluation_id
        JOIN prod2_details d ON d.id_convocatoria=ex.id_convocatoria
        WHERE ex.id=%s AND ex.id_convocatoria=%s""", (outcome["extraction_id"], conv)).fetchone()
    if (not row or row["id_convocatoria"] != conv
            or row["evaluation_id"] != outcome["llm1_evaluation_id"]
            or row["evaluation_status"] != "done" or row["kind"] != "gemini_extraction"
            or row["request_hash"] != outcome["llm1_request_hash"]
            or row["is_current"] is not True or row["quality"] == "failed"
            or row["detail_hash"] != outcome["detail_hash"]
            or row["document_inventory_complete"] is not True
            or not isinstance(row["result_json"], dict)
            or canonical_hash(row["result_json"]) != outcome["llm1_receipt_hash"]):
        raise ValueError("PostgreSQL does not confirm the completed paid read")
    bundle = row["result_json"]
    if bundle.get("model") == "gpt-6-luna":
        from .luna_policy import validate_luna_bundle
        from .luna_fallback import validated_failure_receipt
        validate_luna_bundle(bundle)
        parent = bundle.get("llm1_fallback_from")
        if not isinstance(parent, dict):
            raise ValueError("Luna backup lacks its reconciled parent failure")
        failed = conn.execute("SELECT id,status,result_json,claim_token,evaluation_key FROM prod2_evaluations WHERE id=%s AND id_convocatoria=%s",
            (parent.get("evaluation_id"), conv)).fetchone()
        stem = folder / ("gemini_extraction-" + str(parent.get("request_hash")))
        failure_path, claim_path = stem.with_suffix(".failure.private.json"), stem.with_suffix(".claim.private.json")
        if not failed or failed["status"] != "failed":
            raise ValueError("Luna parent payment is not durably reconciled")
        failure = failed["result_json"]
        claim = {"status": "claimed", "id": failed["id"], "claim_token": str(failed["claim_token"]),
                 "evaluation_key": failed["evaluation_key"]}
        if failure_path.exists() or claim_path.exists():
            if (not failure_path.is_file() or failure_path.is_symlink()
                    or not claim_path.is_file() or claim_path.is_symlink()
                    or read_private(failure_path) != failure or read_private(claim_path) != claim):
                raise ValueError("Local Luna parent ownership differs from PostgreSQL")
        if (not validated_failure_receipt(claim, failure)
                or any(failure.get(key) != value for key, value in parent.items())
                or failure.get("analysis_sha256") != bundle.get("analysis_sha256")
                or failure.get("document_set_hash") != bundle.get("document_set_hash")):
            raise ValueError("Luna parent failure differs from the stored receipt")
    if (bundle.get("cache_key") != "prod2:llm1:" + outcome["llm1_request_hash"]
            or bundle.get("model") != row["model"]
            or bundle.get("document_set_hash") != outcome["document_set_hash"]
            or bundle.get("raw_extraction_json", {}).get("source_document_manifest") != outcome.get("source_document_manifest")
            or bundle.get("raw_extraction_json") != row["raw_extraction_json"]
            or row["facet_count"] != outcome.get("facet_count")):
        raise ValueError("Stored source hashes or structured result differ from the receipt")
    embedding_proof = None
    from .input_policy import EMBEDDING_RETRIEVED_TEXT_POLICY
    sent_input = outcome.get("llm1_input")
    stored_input = bundle.get("raw_extraction_json", {}).get("llm1_input")
    if any(isinstance(value, dict) and value.get("policy_version") == EMBEDDING_RETRIEVED_TEXT_POLICY
           for value in (sent_input, stored_input)):
        if (not isinstance(sent_input, dict) or sent_input != stored_input
                or sent_input.get("analysis_sha256") != bundle.get("analysis_sha256")
                or sent_input.get("source_manifest_hash") != canonical_hash(outcome["source_document_manifest"])):
            raise ValueError("Completed retrieved input differs from the stored paid source")
        from .embedding_store import verified_corpus_retrieval
        # The caller may already own its normal coordinator transaction. This
        # checks real corpus, exact chunks, vectors and immutable provider
        # responses, never merely the earlier pg_verified boolean in a file.
        embedding_proof = verified_corpus_retrieval(conn, sent_input.get("corpus_hash"),
            sent_input.get("retrieval_hash"), allow_transaction=True, expected_input=sent_input)
        stable = {key: value for key, value in embedding_proof.items() if key != "verified_at"}
        if sent_input.get("durable_proof") != stable:
            raise ValueError("Fresh embedding persistence readback differs from the paid input")
    documents = conn.execute("""SELECT codigo_alfresco::text,source_url FROM prod2_documents
        WHERE id_convocatoria=%s AND is_listed ORDER BY codigo_alfresco""", (conv,)).fetchall()
    expected = sorted((doc["document_uuid"], doc["source_url"]) for doc in outcome["documents"])
    if sorted((doc["codigo_alfresco"], doc["source_url"]) for doc in documents) != expected:
        raise ValueError("Official download references are not durably stored")
    # Every parent original, including ZIP/RAR, must have its immutable download fact.
    for path in folder.glob("*.download.private.json"):
        receipt = read_private(path)
        stored = conn.execute("""SELECT bytes,format FROM prod2_document_downloads
            WHERE id_convocatoria=%s AND codigo_alfresco=%s AND sha256_original=%s""",
            (conv, receipt["document_uuid"], receipt["sha256"])).fetchone()
        if not stored or (stored["bytes"], stored["format"]) != (receipt["bytes"], receipt["format"]):
            raise ValueError("An original download fact is missing or differs")
    active = conn.execute("""SELECT count(*) AS count FROM prod2_evaluations
        WHERE id_convocatoria=%s AND status='running'""", (conv,)).fetchone()["count"]
    audit = audit_summary(folder)
    remote_pending, remote_unknown = 0, False
    # The extractor's final cleanup receipt supersedes its generation outcome.
    paths = list(folder.glob("gemini-selected-*.outcome.private.json")) + list(folder.glob("openai-selected-*.outcome.private.json"))
    for path in paths:
        receipt = read_private(path)
        attempt_id = path.name.removesuffix(".outcome.private.json")
        if receipt.get("attempt_id") != attempt_id:
            raise ValueError("Temporary upload outcome belongs to another attempt")
        cleanup = path.with_name(path.name.replace(".outcome.private.json", ".cleanup.private.json"))
        if cleanup.exists():
            receipt = read_private(cleanup)
            if receipt.get("attempt_id") != attempt_id:
                raise ValueError("Temporary upload cleanup belongs to another attempt")
        pending = receipt.get("remote_cleanup_pending")
        unknown = receipt.get("remote_cleanup_unknown")
        if type(pending) is not int or pending < 0 or type(unknown) is not bool:
            raise ValueError("Remote temporary upload cleanup cannot be confirmed")
        remote_pending += pending
        remote_unknown |= unknown
    proof = {"pg_verified": True, "convocation_id": conv, "status": "llm1_complete",
        "document_set_hash": outcome["document_set_hash"], "extraction_id": outcome["extraction_id"],
        "evaluation_id": row["evaluation_id"], "request_hash": row["request_hash"],
        "active_payments": active, "usage_unknown_attempts": audit["usage_unknown_attempts"],
        "remote_cleanup_pending": remote_pending, "remote_cleanup_unknown": remote_unknown}
    if embedding_proof is not None:
        proof["embedding_durable_proof"] = embedding_proof
    return proof
