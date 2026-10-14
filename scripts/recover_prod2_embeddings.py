"""Recover only the accepted members of a stopped, ten-case embeddings pilot.

Dry-run is entirely offline. Local preparation adopts SHA-verified originals,
previews and exact preparation derivatives, preserving the original captures.
Execution is a separate opt-in: no discovery, Redis, new Jev payment or download.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row

import recover_prod2_selected as recovery
import run_prod2_selected as selected
from recover_prod2_details import member_proof
from buenapro_worker.prod2.accepted_repository import _run_id
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.documents import METADATA_URL
from buenapro_worker.prod2.embedding_runtime import config_hash, load_config, validate_frozen_config
from buenapro_worker.prod2.pdf_preparation import _result as verified_preparation
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2.selected_control import RunLimits
from buenapro_worker.prod2.selected_payments import audit_summary

POLICY = "prod2_ten_member_embedding_recovery_v1"
STOP = selected.STOP
FORMATS = {".pdf": "pdf", ".zip": "zip", ".rar": "rar", ".doc": "ole", ".docx": "zip"}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--original-folder", type=Path, required=True)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--embedding-config", type=Path, required=True)
    p.add_argument("--convocation-id", type=int, action="append", default=[])
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--prepare-local", action="store_true")
    modes.add_argument("--execute", action="store_true")
    p.add_argument("--max-embedding-calls", type=int, default=200)
    p.add_argument("--max-llm1-calls", type=int, default=10)
    p.add_argument("--processing-workers", type=int, default=2)
    p.add_argument("--model-workers", type=int, default=2)
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    return p


def sha(path, maximum=300 * 1024 * 1024):
    path = recovery._path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("Recovery evidence must be a bounded nonempty regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def evidence(path, maximum=300 * 1024 * 1024):
    return {"path": str(recovery._path(path)), "sha256": sha(path, maximum), "bytes": path.stat().st_size}


def source_scope(original):
    """Only terminal local review outcomes, with no paid reader attempt, qualify."""
    old = read_private(original / "manifest.private.json")
    if (old.get("pipeline") != "prod2_selected_llm1_v1" or old.get("terminal_stage") != "llm1"
            or old.get("max_accepted") != 10 or old.get("jev_threshold") != 60
            or old.get("llm1_input_policy") != "prod2_embedding_retrieved_text_v1"
            or old.get("document_storage_policy") != "prod2_official_links_temporary_v1"):
        raise ValueError("Recovery requires the frozen ten-case embeddings pilot")
    _run_id(old["run_id"])
    validate_frozen_config(old)
    reports = sorted(original.glob("report-*.private.json"))
    if not reports:
        raise ValueError("Stopped pilot has no terminal report")
    report_path = reports[-1]
    report = read_private(report_path)
    if (report.get("run_id") != old["run_id"] or report.get("accepted") != 10
            or report.get("llm1_complete") != 0 or not isinstance(report.get("records"), list)):
        raise ValueError("Recovery requires exactly ten previously accepted unprocessed readers")
    accepted = [r for r in report["records"] if r.get("accepted") is True]
    ids = [r.get("convocation_id") for r in accepted]
    if (len(ids) != 10 or len(set(ids)) != 10
            or any(type(n) is not int or n <= 0 for n in ids)):
        raise ValueError("Terminal report does not bind ten distinct accepted members")
    costs = audit_summary(original)
    if costs["usage_unknown_attempts"] or costs["llm1_calls"]:
        raise ValueError("Unknown payments or prior reader attempts require explicit reconciliation")
    if any(report.get("costs", {}).get(k) != costs[k] for k in ("usage_unknown_attempts", "llm1_calls")):
        raise ValueError("Pilot payment report differs from actual attempt receipts")
    outcomes = {}
    for record in accepted:
        conv = record["convocation_id"]
        path = original / str(conv) / "outcome.private.json"
        value = read_private(path)
        score = value.get("affinity_index")
        if (value != record or value.get("status") != "needs_review"
                or value.get("error_type") != "EmbeddingPreparationReview"
                or type(score) not in (int, float) or not math.isfinite(score) or not 60 <= score <= 100):
            raise ValueError("Accepted recovery scope includes a nonlocal or changed terminal outcome")
        outcomes[str(conv)] = {"sha256": sha(path), "affinity_index": score,
                               "description_hash": hashlib.sha256(value["description"].encode()).hexdigest()}
    return old, sorted(ids), outcomes, evidence(report_path), costs


def plan(args):
    original, folder = recovery._path(args.original_folder), recovery._path(args.folder)
    _run_id(folder.name)
    if (folder == original or folder.is_relative_to(original) or original.is_relative_to(folder)
            or not original.is_dir()):
        raise ValueError("Use a separate private recovery folder outside the source pilot")
    marker = folder / "manifest.private.json"
    if folder.exists() and (not folder.is_dir() or (any(folder.iterdir()) and not marker.is_file())):
        raise ValueError("Recovery folder must be new or own a frozen manifest")
    if not (1 <= args.max_embedding_calls <= 200 and 1 <= args.max_llm1_calls <= 10
            and 1 <= args.processing_workers <= 2 and 1 <= args.model_workers <= 2):
        raise ValueError("Recovery provider and worker bounds are invalid")
    old, scope_ids, outcomes, report, source_costs = source_scope(original)
    ids = args.convocation_id or scope_ids
    if (not ids or len(ids) != len(set(ids)) or any(n not in scope_ids for n in ids)
            or any(type(n) is not int for n in ids)):
        raise ValueError("Execution IDs must be a distinct subset of the ten accepted pilot members")
    config = load_config(args.embedding_config)
    if config_hash(config) == old["embedding_reading"]["config_sha256"]:
        raise ValueError("Use the new explicit corrected embeddings configuration")
    ocr_sources = {}
    for conv in ids:
        paths = sorted((original / str(conv)).glob("embedding-corpus-*"))
        ocr_sources[str(conv)] = [str(recovery._path(path)) for path in paths if path.is_dir()]
    manifest = {k: v for k, v in old.items() if k not in ("requested", "embedding_resume_from", "frozen_at")}
    manifest.update(recovery_policy=POLICY, recovery_run_id=folder.name,
        original_folder=str(original), recovery_folder=str(folder),
        original_manifest_hash=hash_request(old), original_manifest_file_sha256=sha(original / "manifest.private.json"),
        original_terminal_report=report, accepted_scope_ids=scope_ids, recovery_ids=sorted(ids),
        original_outcomes=outcomes, original_costs=source_costs, no_discovery=True, no_redis=True,
        no_downloads=True, max_source_requests=0, max_jev_calls=0, max_accepted=len(ids), max_candidates=len(ids),
        max_embedding_calls=args.max_embedding_calls, max_llm1_calls=args.max_llm1_calls,
        processing_workers=args.processing_workers, model_workers=args.model_workers,
        embedding_reading={"config": config, "config_sha256": config_hash(config)},
        embedding_ocr_cache_sources=ocr_sources, detail_recovery_folders=[], download_cache_folders=[str(original)])
    # New coverage modes have a distinct prompt identity; old manifests stay frozen.
    if config.get("reading", {}).get("transport_policy"):
        manifest["llm1_retrieved_transport_policy"] = config["reading"]["transport_policy"]
    if config.get("reading", {}).get("source_coverage_policy"):
        manifest["llm1_source_coverage_policy"] = config["reading"]["source_coverage_policy"]
    else:
        manifest.pop("llm1_source_coverage_policy", None)
    validate_frozen_config(manifest)
    if marker.exists() and read_private(marker) != manifest:
        raise ValueError("Frozen recovery inputs changed; use identical flags or a new folder")
    return manifest


def copy_evidence(source, target, expected=None):
    fact = evidence(source)
    if expected is not None and fact["sha256"] != expected:
        raise ValueError("Adopted file SHA differs from its immutable source receipt")
    target = recovery._path(target)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    recovery._copy_exact(source, target, fact["sha256"])
    if sha(target) != fact["sha256"]:
        raise ValueError("Adopted file readback differs from its source")
    return {**fact, "target": str(target)}


def prepare_local(manifest):
    """Adopt inputs only; every receipt is rechecked before execution."""
    folder, original = Path(manifest["recovery_folder"]), Path(manifest["original_folder"])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    recovery.frozen(folder / "manifest.private.json", manifest)
    adopted, origins = [], []
    for conv in manifest["recovery_ids"]:
        source, private = original / str(conv), folder / str(conv)
        capture = read_private(source / "detail.private.json")
        detail = capture.get("detail", {})
        outcome = read_private(source / "outcome.private.json")
        if (detail.get("convocatoria_id") != conv or detail.get("description") != outcome["description"]
                or detail.get("document_inventory_complete") is not True):
            raise ValueError("Frozen ficha differs from the accepted pilot outcome")
        adopted.append(copy_evidence(source / "detail.private.json", private / "detail.private.json"))
        docs = {doc["uuid"]: doc for doc in detail["documents"]}
        if len(docs) != len(detail["documents"]):
            raise ValueError("Official document UUIDs must not repeat")
        for code, doc in docs.items():
            suffix = Path(doc["filename"]).suffix.lower()
            if suffix not in FORMATS:
                continue  # Unsupported formats cannot cause an automatic download.
            receipt_path = source / (code + ".download.private.json")
            facts = read_private(receipt_path)
            original_file = source / (code + suffix)
            if (facts.get("document_uuid") != code or facts.get("source_url") != METADATA_URL + "?id=" + code
                    or facts.get("format") != FORMATS[suffix] or type(facts.get("bytes")) is not int
                    or not 0 < facts["bytes"] <= manifest["max_source_mib"] * 1024**2
                    or original_file.stat().st_size != facts["bytes"]):
                raise ValueError("Official original does not match its UUID, format or byte receipt")
            adopted.append(copy_evidence(original_file, private / original_file.name, facts["sha256"]))
            adopted.append(copy_evidence(receipt_path, private / receipt_path.name))
        recovery.cached_previews([source], private)
        for preview in private.glob("text-previews/*/document-preview-cache/*.private.json"):
            relative = preview.relative_to(private)
            # Existing preview validation publishes canonical-equivalent JSON;
            # preserve both byte hashes instead of claiming identical formatting.
            source_preview = source / relative
            if read_private(source_preview) != read_private(preview):
                raise ValueError("Validated adopted preview differs from its source content")
            adopted.append({**evidence(source_preview), "target": str(preview),
                "target_sha256": sha(preview), "canonical_hash": hash_request(read_private(preview)),
                "adoption_mode": "validated_preview_json"})
        selection_path = source / "selection.private.json"
        selection = read_private(selection_path)
        if (selection.get("policy_version") != manifest["selector_policy_version"]
                or not selection.get("selected_ids")):
            raise ValueError("Accepted member has no reusable immutable document selection")
        origins.append({"convocation_id": conv, "selection": evidence(selection_path)})
        # Exact preparation caches preserve Word pagination and the OCR source PDF SHA.
        for marker in sorted(source.glob("preparation-cache/selected-analysis-*/manifest.private.json")):
            pinned = read_private(marker)
            if marker.parent.name != "selected-analysis-" + pinned.get("selection_hash", ""):
                raise ValueError("Selected preparation cache identity is invalid")
            verified_preparation(marker.parent, pinned, pinned["max_analysis_bytes"])
            files = [marker, marker.parent / "analysis.pdf", *[marker.parent / p["file_name"] for p in pinned["chunks"]]]
            for path in files:
                adopted.append(copy_evidence(path, private / path.relative_to(source)))
            target = private / marker.relative_to(source)
            verified_preparation(target.parent, read_private(target), pinned["max_analysis_bytes"])
        for corpus_root in manifest["embedding_ocr_cache_sources"][str(conv)]:
            for path in sorted(Path(corpus_root).glob("ocr-page-*.private.json")):
                value = read_private(path)
                if (value.get("identity_hash") != hash_request(value.get("identity"))
                        or value.get("cache_hash") != hash_request({k: v for k, v in value.items() if k != "cache_hash"})):
                    raise ValueError("Prior OCR page receipt differs from its immutable content hash")
                origins.append({"convocation_id": conv, "ocr_page": evidence(path, 16 * 1024 * 1024)})
    receipt = {"policy": POLICY, "manifest_hash": hash_request(manifest), "files": adopted,
        "read_only_origins": origins, "files_count": len(adopted), "adopted_bytes": sum(f["bytes"] for f in adopted),
        "source_calls": 0, "provider_calls": 0, "postgres_calls": 0, "redis_calls": 0}
    recovery.frozen(folder / "local-adoption.private.json", receipt)
    return receipt


def verify_adoption(manifest):
    folder = Path(manifest["recovery_folder"])
    receipt = read_private(folder / "local-adoption.private.json")
    if receipt.get("manifest_hash") != hash_request(manifest):
        raise ValueError("Adoption is bound to another recovery manifest")
    original = Path(manifest["original_folder"])
    for fact in receipt["files"]:
        if (not recovery._path(fact["path"]).is_relative_to(original)
                or not recovery._path(fact["target"]).is_relative_to(folder)
                or sha(Path(fact["path"])) != fact["sha256"]
                or sha(Path(fact["target"])) != fact.get("target_sha256", fact["sha256"])):
            raise ValueError("Adopted source or keeper changed before execution")
        if fact.get("adoption_mode") == "validated_preview_json" and (
                hash_request(read_private(Path(fact["path"]))) != fact["canonical_hash"]
                or hash_request(read_private(Path(fact["target"]))) != fact["canonical_hash"]):
            raise ValueError("Adopted preview content differs from its validated identity")
    for item in receipt["read_only_origins"]:
        fact = item.get("selection", item.get("ocr_page"))
        if (not recovery._path(fact["path"]).is_relative_to(original)
                or sha(Path(fact["path"])) != fact["sha256"]):
            raise ValueError("Read-only paid selection/OCR origin changed")
    return receipt


class NoSourcePacer:
    requests = 0

    def __call__(self, request=None):
        raise RuntimeError("Embedding recovery forbids discovery, ficha HTTP and downloads")


def recover_member(settings, proof, manifest, limits, jev_key):
    private = Path(manifest["recovery_folder"]) / str(proof["convocation_id"])
    path = private / "outcome.private.json"
    if path.exists():
        value = read_private(path)
        if value.get("recovery_manifest_hash") != hash_request(manifest):
            raise ValueError("Existing outcome is bound to different recovery inputs")
        return value  # Terminal attempts are immutable; no automatic paid retry.
    record = {"convocation_id": proof["convocation_id"], "accepted": True,
        "affinity_index": proof["affinity_index"], "status": "needs_review",
        "recovery_manifest_hash": hash_request(manifest), "accepted_proof_hash": hash_request(proof)}
    try:
        item = recovery.prepare_member(proof, manifest, private)
        if STOP.is_set():
            return {**record, "status": "stopped_before_enrichment"}
        with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                application_name="buenapro-prod2-embedding-recovery-member", connect_timeout=15) as conn:
            record.update(selected.enrich(conn, settings, private, item, manifest, NoSourcePacer(), limits, jev_key))
    except Exception as error:
        from buenapro_worker.prod2.embedding_corpus import CorpusStopped
        if isinstance(error, CorpusStopped) and STOP.is_set():
            return {**record, "status": "stopped_local_ocr"}
        record.update(requires_human_review=True, error_type=type(error).__name__)
        reason = recovery.safe_error_reason(error)
        if reason:
            record["error_reason"] = reason
    private_write(path, record)
    if record["status"] == "llm1_complete":
        # Storage verification consumes the immutable published outcome. A local
        # cleanup failure must never replace the paid result or trigger a retry.
        try:
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                    application_name="buenapro-prod2-embedding-recovery-cleanup", connect_timeout=15) as conn:
                selected.cleanup_finished_case(conn, private, record, manifest)
        except Exception as error:
            print(json.dumps({"convocation_id": proof["convocation_id"],
                "temporary_cleanup": {"status": "retained_verification_failed", "error_type": type(error).__name__}}), flush=True)
    print(json.dumps({k: record.get(k) for k in ("convocation_id", "status", "error_type", "llm1_cost_usd")}), flush=True)
    return record


def execute(args, manifest):
    """Future explicit execution; lock and accepted PG proof precede provider work."""
    folder = Path(manifest["recovery_folder"])
    verify_adoption(manifest)
    STOP.clear()
    costs = audit_summary(folder)
    if costs["usage_unknown_attempts"]:
        raise RuntimeError("Recovery payment needs reconciliation; no automatic retry")
    limits = RunLimits(max_accepted=len(manifest["recovery_ids"]), max_candidates=len(manifest["recovery_ids"]),
        max_jev_calls=0, max_llm1_calls=manifest["max_llm1_calls"], max_embedding_calls=manifest["max_embedding_calls"],
        model_workers=manifest["model_workers"], accepted_ids=manifest["recovery_ids"],
        llm1_calls=costs["llm1_calls"], embedding_calls=costs["embedding_calls"])
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in handlers:
        signal.signal(sig, lambda *_: STOP.set())
    started = time.monotonic()
    try:
        with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            settings = settings.model_copy(update={"gemini_model": manifest["gemini_model"]})
            jev_key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY")
            if not jev_key:
                raise ValueError("Existing Jev configuration is missing")
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                    application_name="buenapro-prod2-embedding-recovery", connect_timeout=15) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                    raise RuntimeError("Another PROD2 coordinator owns the executor")
                try:
                    with conn.transaction():
                        conn.execute("SET TRANSACTION READ ONLY")
                        proofs = [member_proof(conn, manifest["run_id"], conv) for conv in manifest["recovery_ids"]]
                        for proof in proofs:
                            prior = manifest["original_outcomes"][str(proof["convocation_id"])]
                            if (proof["affinity_index"] != prior["affinity_index"]
                                    or hashlib.sha256(proof["row"]["description"].encode()).hexdigest() != prior["description_hash"]):
                                raise ValueError("Durable accepted member differs from the frozen pilot")
                        # Cache-only selector: every existing payment must already be DONE.
                        for conv in manifest["recovery_ids"]:
                            receipt = read_private(Path(manifest["original_folder"]) / str(conv) / "selection.private.json")
                            cached = conn.execute("SELECT status FROM prod2_document_selection_cache WHERE request_hash=%s",
                                                  (receipt["request_hash"],)).fetchone()
                            if not cached or cached["status"] != "done":
                                raise RuntimeError("Document selection needs reconciliation; new Jev calls are forbidden")
                    recovery.frozen(folder / "members.private.json", proofs)
                    from buenapro_worker.prod2.embedding_queries import prepare_query_vectors
                    prepare_query_vectors(validate_frozen_config(manifest), settings, conn, folder, limits, stop_requested=STOP.is_set)
                    with ThreadPoolExecutor(max_workers=manifest["processing_workers"]) as pool:
                        futures = [pool.submit(recover_member, settings, proof, manifest, limits, jev_key) for proof in proofs]
                        records = [future.result() for future in futures]
                    costs = audit_summary(folder)
                    report = {"policy": POLICY, "run_id": manifest["run_id"], "recovery_run_id": manifest["recovery_run_id"],
                        "scope_ids": manifest["accepted_scope_ids"], "processed_ids": manifest["recovery_ids"], "records": records,
                        "llm1_complete": sum(r["status"] == "llm1_complete" for r in records), "costs": costs,
                        "source_calls": 0, "redis_calls": 0, "new_candidates": 0, "stopped": STOP.is_set(),
                        "elapsed_seconds": round(time.monotonic() - started, 3), "completed_at": datetime.now(timezone.utc).isoformat()}
                    private_write(folder / ("report-" + str(time.time_ns()) + ".private.json"), report)
                    print(json.dumps({k: v for k, v in report.items() if k != "records"}), flush=True)
                    return 0 if report["llm1_complete"] == len(proofs) and costs["usage_unknown_attempts"] == 0 else 2
                finally:
                    if not conn.closed and not conn.broken:
                        conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main():
    args = parser().parse_args()
    manifest = plan(args)
    if args.dry_run:
        print(json.dumps({"policy": POLICY, "scope_ids": manifest["accepted_scope_ids"], "execution_ids": manifest["recovery_ids"],
            "source_calls": 0, "provider_calls": 0, "postgres_calls": 0, "redis_calls": 0,
            "new_candidates": 0, "embedding_config_sha256": manifest["embedding_reading"]["config_sha256"]}))
        return 0
    os.umask(0o077)
    if args.prepare_local:
        receipt = prepare_local(manifest)
        print(json.dumps({k: receipt[k] for k in ("files_count", "adopted_bytes", "source_calls", "provider_calls", "postgres_calls", "redis_calls")}))
        return 0
    return execute(args, manifest)


if __name__ == "__main__":
    raise SystemExit(main())
