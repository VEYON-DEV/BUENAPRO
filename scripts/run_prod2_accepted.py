"""Bounded HTTP-only PROD2 pilot: Redis -> Jev>=60 -> ficha -> documents -> LLM1.

No scheduler, national sweep, LLM2, matching or notifications. At most twenty
accepted convocatorias per frozen run; replay never automatically repays an
uncertain provider reservation. Credentials remain in ignored env/in memory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row
import redis

from buenapro_worker.prod2 import accepted_repository as repo
from buenapro_worker.prod2.accepted_runner import accepted, paid_jev, paid_llm1
from buenapro_worker.prod2.analysis import (JEV_MODEL, JEV_POLICY_VERSION, JevDescriptionAnalyzer,
    extract_document, extraction_request_hash)
from buenapro_worker.prod2.batch_plan import MEMBER_IDS, configured_keywords, digest, native_arguments, page_capture, publication_window
from buenapro_worker.prod2.batch_repository import save_participation_assessment
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.client import Prod2HttpClient, SEARCH_URL
from buenapro_worker.prod2.discovery import validate_capture
from buenapro_worker.prod2.documents import METADATA_URL, download_document
from buenapro_worker.prod2.document_set import DIRECT_DOCUMENTS_POLICY, prepare_direct_document_set
from buenapro_worker.prod2.llm1_completion import persist_completed_llm1
from buenapro_worker.prod2.participation import decide
from buenapro_worker.prod2.previews import upload_preview
from buenapro_worker.prod2.repository import persist_detail, record_document_download
from buenapro_worker.prod2.runtime import production_staging_settings
from buenapro_worker.prod2.staging import RedisStage
from buenapro_worker.seace.egress import seace_http_client


class Pacer:
    def __init__(self, seconds):
        self.seconds, self.last = seconds, 0.0

    def __call__(self, request):
        delay = self.seconds - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()


def commercial_snapshot(conn):
    members = []
    for profile_id in MEMBER_IDS:
        row = conn.execute("SELECT identity_json FROM company_profiles WHERE id=%s AND is_active", (profile_id,)).fetchone()
        if not row:
            raise RuntimeError("Authorized commercial profile is unavailable")
        description = row["identity_json"].get("descripcion")
        lines = [line["nombre"] for line in conn.execute(
            "SELECT nombre FROM business_lines WHERE profile_id=%s AND is_active ORDER BY nombre", (profile_id,)).fetchall()]
        if not isinstance(description, str) or not description.strip() or not lines:
            raise RuntimeError("Commercial description/lines are required")
        members.append({"descripcion": description, "lineas": lines})
    return members


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--candidate-limit", type=int, default=25)
    p.add_argument("--keyword", action="append")
    p.add_argument("--years", default="2026,2025")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--from-date")
    p.add_argument("--to-date")
    p.add_argument("--request-interval", type=float, default=2.0)
    p.add_argument("--dry-run", action="store_true")
    return p


def plan(args):
    if not 1 <= args.limit <= 20 or not args.limit <= args.candidate_limit <= 30:
        raise ValueError("Pilot bound: 1-20 accepted, at most 30 candidate evaluations")
    if not 1 <= args.request_interval <= 60:
        raise ValueError("Source requests must be paced")
    if not re.fullmatch(r"[0-9]{4}(,[0-9]{4})*", args.years):
        raise ValueError("Use explicit comma-separated exercise years")
    years = [int(y) for y in args.years.split(",")]
    if len(years) != len(set(years)) or any(not 2000 <= y <= 2100 for y in years):
        raise ValueError("Invalid or duplicate exercise years")
    inventory = read_private(ROOT / "docs/sources/prod2-convenio-keywords.v3.json")
    configured = {kw.casefold(): kw for kw in configured_keywords(inventory)}
    requested = args.keyword or ["SOFTWARE"]
    if any(kw.strip().casefold() not in configured for kw in requested):
        raise ValueError("Keyword must belong to the current union inventory")
    return {"limit": args.limit, "candidate_limit": args.candidate_limit,
        "keywords": list(dict.fromkeys(configured[kw.strip().casefold()] for kw in requested)),
        "years": years, "months": args.months, "from_date": args.from_date, "to_date": args.to_date,
        "inventory_hash": digest(inventory), "configured_keywords": len(configured),
        "objects": ["good", "service"], "jev_threshold": 60,
        "pipeline": "temporary_jev60_detail_loose_documents_llm1_v1", "terminal_stage": "llm1",
        "document_scope": DIRECT_DOCUMENTS_POLICY,
        "coverage": "bounded_pilot_partial"}


def discover(folder, manifest, pacer):
    """Observe real pages; interleave goods/services instead of selecting one only.

    This bounded pilot intentionally makes no coverage claim. Broad source
    queries reaching 499 must be split in a future complete-load executor.
    """
    candidates, seen, pages = [], set(), []
    with seace_http_client(timeout=60, verify=True, event_hooks={"request": [pacer]}) as http:
        source = Prod2HttpClient(http)
        source.bootstrap()
        if set(manifest["years"]) - set(source.available_convocation_years()):
            raise ValueError("Exercise year is absent from the observed selector")
        for keyword in manifest["keywords"]:
            for year in manifest["years"]:
                by_object = []
                for object_type in manifest["objects"]:
                    query = {"keyword": keyword, "object_type": object_type, "year": year,
                        "published_from": manifest["published_from"], "published_to": manifest["published_to"], "seace_version": "3"}
                    raw = source.search(keyword, **native_arguments(query))
                    observed = datetime.now(timezone.utc)
                    capture = validate_capture(page_capture(query, raw, observed))
                    pages.append({"query": query, "page": capture.page, "page_hash": capture.page_hash,
                                  "observed_at": observed.isoformat()})
                    by_object.append([{"row": row, "query": query, "page_number": capture.page["number"],
                        "observed_at": observed.isoformat()} for row in capture.rows])
                for index in range(max(map(len, by_object), default=0)):
                    for rows in by_object:
                        if index < len(rows):
                            item = rows[index]
                            conv = item["row"]["convocation_id"]
                            if conv not in seen and len(candidates) < manifest["candidate_limit"]:
                                seen.add(conv)
                                candidates.append(item)
                if len(candidates) >= manifest["candidate_limit"]:
                    break
            if len(candidates) >= manifest["candidate_limit"]:
                break
    result = {"candidates": candidates, "pages": pages, "coverage": "bounded_first_pages_partial"}
    private_write(folder / "candidates.private.json", result)
    return result


def get_detail(item, folder, pacer, event_hooks=None):
    path = folder / "detail.private.json"
    if path.exists():
        return read_private(path)
    with seace_http_client(timeout=60, verify=True, event_hooks=event_hooks or {"request": [pacer]}) as http:
        source = Prod2HttpClient(http)
        query = item["query"]
        # A candidate of the national daily listing has no keyword: its query is searched as such.
        source.search(query["keyword"], **native_arguments(query),
                      **({"allow_empty_keyword": True} if query["keyword"] == "" else {}))
        row = item["row"]
        captured_page = item["page_number"]
        if type(captured_page) is not int or captured_page < 1:
            raise ValueError("Captured search page must be a positive integer")
        total_pages = source.search_snapshot["total_pages"]
        index = None
        # Re-enter only this identity in the captured query's local neighborhood.
        # Native pagination continues to validate counters, duplicates and rows.
        for page in (captured_page, captured_page - 1, captured_page + 1,
                     captured_page - 2, captured_page + 2):
            if not 1 <= page <= total_pages:
                continue
            if source.counter[3] != page:
                source.search_page(page)
            index = next((i for i, current in enumerate(source.rows)
                if current["convocatoria_id"] == row["convocation_id"]
                and current["procedure_id"] == row["procedure_id"]), None)
            if index is not None:
                if source.rows[index]["description"] != row["description"]:
                    raise RuntimeError("Accepted source row changed; a new Jev version is required")
                break
        if index is None:
            raise RuntimeError("Accepted source row not found within bounded captured-page neighbors")
        detail = source.detail(index, all_documents=True)
    capture = {"observed_at": datetime.now(timezone.utc).isoformat(),
        "detail": {k: v for k, v in detail.items() if k not in {"http_status", "redirect_statuses"}}}
    private_write(path, capture)
    return capture


def enrich(conn, settings, folder, item, manifest, pacer):
    conv = item["row"]["convocation_id"]
    capture = get_detail(item, folder, pacer)
    detail = capture["detail"]
    saved_detail = persist_detail(conn, detail, observed_at=datetime.fromisoformat(capture["observed_at"]), source_url=SEARCH_URL)
    temporal = decide(detail, datetime.now(timezone.utc))
    save_participation_assessment(conn, conv, detail_hash=saved_detail["detail_hash"], assessment=temporal)
    downloads, failures, deferred = {}, [], []
    for doc in detail["documents"]:
        suffix = Path(doc["filename"]).suffix.lower()
        if suffix in {".zip", ".rar"}:
            deferred.append(doc["uuid"])
            continue
        kind = {".pdf": "pdf", ".docx": "zip", ".doc": "ole"}.get(suffix)
        try:
            if kind is None:
                raise ValueError("Unsupported document format")
            original = folder / (doc["uuid"] + suffix)
            receipt = folder / (doc["uuid"] + ".download.private.json")
            if receipt.exists():
                downloaded = read_private(receipt)
                if (downloaded.get("document_uuid") != doc["uuid"]
                        or hashlib.sha256(original.read_bytes()).hexdigest() != downloaded["sha256"]):
                    raise RuntimeError("Original hash changed")
            else:
                with seace_http_client(timeout=90, verify=True, event_hooks={"request": [pacer]}) as http:
                    downloaded = download_document(http, doc["uuid"], original, expected_format=kind)
                private_write(receipt, downloaded)
            record_document_download(conn, conv, downloaded)
            downloads[doc["uuid"]] = {"original": original, "manifest": downloaded, "suffix": suffix}
        except Exception as error:
            failures.append({"uuid": doc["uuid"], "error_type": type(error).__name__})
    common = {"schedule": detail["schedule"], "item_states": detail["item_states"], "temporal_state": temporal["state"],
        "document_inventory_complete": detail["document_inventory_complete"],
        "documents": [{**doc, "source_url": METADATA_URL + "?id=" + doc["uuid"],
            "downloaded": doc["uuid"] in downloads,
            "analysis_status": "archive_deferred" if doc["uuid"] in deferred else
                "downloaded" if doc["uuid"] in downloads else "download_failed"}
            for doc in detail["documents"]],
        "downloads": len(downloads), "download_failures": failures, "detail_hash": saved_detail["detail_hash"],
        "document_scope": DIRECT_DOCUMENTS_POLICY, "deferred_archive_uuids": deferred}
    partial = folder / "enrichment-direct.private.json"
    progress = folder / ("enrichment-direct-attempt-" + str(time.time_ns()) + ".private.json")
    private_write(progress, common)
    snapshot = folder / (".enrichment-direct-snapshot-" + str(time.time_ns()))
    private_write(snapshot, common)
    os.replace(snapshot, partial)
    if failures:
        raise RuntimeError("At least one loose attachment was not downloaded; no partial LLM1 call")
    if detail["document_inventory_complete"] is not True:
        raise RuntimeError("An incomplete official inventory cannot complete enrichment")
    if not downloads and deferred and len(deferred) == len(detail["documents"]):
        return {**common, "status": "archives_deferred", "requires_human_review": True,
                "analyzed_document_uuids": [], "facet_count": 0,
                "llm1_cached": None, "llm1_cost_usd": 0}
    prepared = prepare_direct_document_set(folder, detail["documents"], downloads,
        inventory_complete=detail["document_inventory_complete"])
    pdf = prepared["pdf_path"].read_bytes()
    doc_manifest = prepared["source_document_manifest"]
    context = {key: prepared[key] for key in ("document_scope", "document_set", "excluded_documents", "document_set_hash")}
    anchor = prepared["anchor_document_uuid"]
    downloaded = downloads[anchor]["manifest"]
    preview = upload_preview(pdf, convocation_id=conv, document_uuid=anchor,
        original_sha256=prepared["anchor_original_sha256"], env_path=ROOT / ".env.local")
    record_document_download(conn, conv, downloaded, storage_key=preview["storage_key"])
    request_hash = extraction_request_hash(settings, pdf, object_type=item["query"]["object_type"],
        original_sha256=downloaded["sha256"], manifest=doc_manifest, document_context=context)
    def extract():
        return {**extract_document(settings, pdf, object_type=item["query"]["object_type"],
            original_sha256=downloaded["sha256"], manifest=doc_manifest, document_context=context),
            "document_uuid": anchor, "document_sha256": downloaded["sha256"]}
    evaluation_id, bundle, hit = paid_llm1(conn, folder, conv, request_hash, settings, manifest["profile_id"], extract)
    saved = persist_completed_llm1(conn, convocation_id=conv, document_uuid=anchor,
        extraction={**bundle, "evaluation_id": evaluation_id,
            "allowed_models": [settings.gemini_model, settings.gemini_fallback_model]},
        document_sha256=downloaded["sha256"], source_url=downloaded["source_url"])
    return {**common, **saved, "llm1_cached": hit, "llm1_cost_usd": 0 if hit else bundle.get("cost_usd"),
        "facet_count": len(bundle["facets"]), "anchor_document_uuid": anchor,
        "anchor_document_sha256": downloaded["sha256"], "llm1_model": bundle["model"],
        "analyzed_document_uuids": [doc["document_uuid"] for doc in prepared["document_set"]],
        "source_document_manifest": doc_manifest, "analysis_page_count": prepared["page_count"],
        "document_set_hash": prepared["document_set_hash"],
        "status": "llm1_complete"}


def main():
    args = parser().parse_args()
    os.umask(0o077)
    requested = plan(args)
    start, end = publication_window(datetime.now(timezone.utc), months=args.months,
        published_from=args.from_date, published_to=args.to_date)
    if args.dry_run:
        print(json.dumps({**requested, "dry_run": True, "published_from": start, "published_to": end}, ensure_ascii=False))
        return 0
    if args.folder.is_symlink() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", args.folder.name):
        raise ValueError("Use a private run directory with a safe unique name")
    folder = args.folder.resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_id, pacer = folder.name, Pacer(args.request_interval)
    with production_staging_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as runtime:
        settings, redis_url = runtime
        with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
            if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                raise RuntimeError("Another accepted runner owns the executor")
            try:
                marker = folder / "manifest.private.json"
                if marker.exists():
                    manifest = read_private(marker)
                    if manifest["requested"] != requested:
                        raise ValueError("Resume requires identical flags/inventory")
                else:
                    manifest = {**requested, "requested": requested, "run_id": run_id,
                        "published_from": start, "published_to": end, "profile_id": MEMBER_IDS[0],
                        "commercial_profiles": commercial_snapshot(conn), "frozen_at": datetime.now(timezone.utc).isoformat()}
                    private_write(marker, manifest)
                staging = RedisStage(redis.Redis.from_url(redis_url, decode_responses=True), run_id)
                staging.recover()
                captured = read_private(folder / "candidates.private.json") if (folder / "candidates.private.json").exists() else discover(folder, manifest, pacer)
                for item in captured["candidates"]:
                    key = str(item["row"]["convocation_id"]) + ":" + jev_request_hash_for(item, manifest)
                    staging.stage(key, item)
                jev = JevDescriptionAnalyzer(dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY"))
                outcomes = [read_private(path) for path in sorted(folder.glob("*/outcome.private.json"))]
                initial_processed = len(outcomes)
                accepted_ids = {row["convocation_id"] for row in conn.execute(
                    "SELECT convocation_id FROM prod2_accepted_run_members WHERE run_id=%s AND accepted AND opportunity_id IS NOT NULL",
                    (run_id,)).fetchall()}
                pending_accepted = accepted_ids - {row["convocation_id"] for row in outcomes if row["accepted"]}
                admitted = len(accepted_ids)
                processed = len(outcomes)
                while (admitted < manifest["limit"] or pending_accepted) and processed < manifest["candidate_limit"]:
                    job = staging.next()
                    if job is None:
                        break
                    candidate_id, item = job
                    conv = item["row"]["convocation_id"]
                    candidate_folder = folder / str(conv)
                    candidate_folder.mkdir(mode=0o700, exist_ok=True)
                    outcome_path = candidate_folder / "outcome.private.json"
                    if outcome_path.exists():
                        staging.ack(candidate_id)
                        continue
                    if admitted >= manifest["limit"] and conv not in pending_accepted:
                        # Hold later candidates until the already-promoted job is recovered.
                        continue
                    request_hash, result, cached = paid_jev(conn, candidate_folder, item["row"],
                        manifest["commercial_profiles"], manifest["profile_id"], jev, repo)
                    is_accepted = accepted(result)
                    if is_accepted:
                        result["request_context"] = {"description": item["row"]["description"],
                                                     "commercial_profiles": manifest["commercial_profiles"]}
                    record = {"convocation_id": conv, "procedure_id": item["row"]["procedure_id"],
                        "entity": item["row"]["buyer_name"], "description": item["row"]["description"],
                        "nomenclature": item["row"]["nomenclature"], "object_type": item["row"]["object_type"],
                        "affinity_index": result["affinity_index"], "jev_cached": cached,
                        "jev_cost_usd": 0 if cached else result.get("cost_usd"),
                        "accepted": is_accepted, "status": "rejected_below_60"}
                    repo.record_run_member(conn, run_id=run_id, convocation_id=conv,
                        request_hash=request_hash, accepted=is_accepted, result=result)
                    if is_accepted:
                        promotion = repo.promote_accepted(conn, run_id=run_id, row=item["row"],
                            query_manifest=item["query"], observed_at=datetime.fromisoformat(item["observed_at"]), jev_result=result)
                        if conv not in accepted_ids:
                            accepted_ids.add(conv)
                            admitted += 1
                        record["opportunity_created"] = promotion["created"]
                        try:
                            record.update(enrich(conn, settings, candidate_folder, item, manifest, pacer))
                        except Exception as error:
                            partial = candidate_folder / "enrichment-direct.private.json"
                            if partial.exists():
                                record.update(read_private(partial))
                            record.update(status="needs_review", error_type=type(error).__name__)
                    private_write(outcome_path, record)
                    staging.ack(candidate_id)
                    processed += 1
                    outcomes.append(record)
                    pending_accepted.discard(conv)
                    print(json.dumps({k: record[k] for k in ("convocation_id", "affinity_index", "accepted", "status")}, ensure_ascii=False), flush=True)
                staging.recover()  # Deferred bounded-pilot work remains queued, never expires.
                report = {"run_id": run_id, "pipeline": manifest["pipeline"], "terminal_stage": "llm1",
                    "limit": manifest["limit"], "coverage": captured["coverage"], "source_pages": captured["pages"],
                    "published_from": manifest["published_from"], "published_to": manifest["published_to"],
                    "processed": len(outcomes), "accepted": sum(r["accepted"] for r in outcomes),
                    "rejected": sum(not r["accepted"] for r in outcomes),
                    "llm1_complete": sum(r["status"] == "llm1_complete" for r in outcomes),
                    "archives_deferred": sum(r["status"] == "archives_deferred" for r in outcomes),
                    "new_jev_calls": sum(not r["jev_cached"] for r in outcomes),
                    "new_llm1_calls": sum(r.get("llm1_cached") is False for r in outcomes),
                    "new_cost_usd": sum((r.get("jev_cost_usd") or 0) + (r.get("llm1_cost_usd") or 0) for r in outcomes),
                    "this_execution": {"processed": len(outcomes) - initial_processed,
                        "new_jev_calls": sum(not r["jev_cached"] for r in outcomes[initial_processed:]),
                        "new_llm1_calls": sum(r.get("llm1_cached") is False for r in outcomes[initial_processed:]),
                        "new_cost_usd": sum((r.get("jev_cost_usd") or 0) + (r.get("llm1_cost_usd") or 0)
                            for r in outcomes[initial_processed:])},
                    "redis": staging.stats(), "records": outcomes}
                report_path = folder / ("report-" + str(time.time_ns()) + ".private.json")
                private_write(report_path, report)
                print(json.dumps({k: v for k, v in report.items() if k not in {"records", "source_pages"}}, ensure_ascii=False), flush=True)
                return 0 if report["accepted"] == manifest["limit"] and (
                    report["llm1_complete"] + report["archives_deferred"] == report["accepted"]) else 2
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")


def jev_request_hash_for(item, manifest):
    from buenapro_worker.prod2.analysis import jev_request_hash
    return jev_request_hash(item["row"]["description"], commercial_profiles=manifest["commercial_profiles"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
