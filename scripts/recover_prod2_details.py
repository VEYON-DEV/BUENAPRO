"""Recover accepted PROD2 fichas only; no downloads, models, Redis or new candidates.

Uses the existing durable Jev decision and preserves the original run's receipts
and outcomes. A separate private folder pins the recovery's source observations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

import httpx
import psycopg
from psycopg.rows import dict_row

from buenapro_worker.prod2.accepted_repository import _run_id, _same_result
from buenapro_worker.prod2.batch_plan import native_arguments
from buenapro_worker.prod2.batch_repository import save_participation_assessment
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.client import Prod2HttpClient, SEARCH_URL
from buenapro_worker.prod2.documents import METADATA_URL
from buenapro_worker.prod2.participation import decide
from buenapro_worker.prod2.repository import persist_detail, hash_request
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2.selected_control import SharedPacer

POLICY = "prod2_accepted_detail_recovery_v1"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--convocation-id", type=int, action="append", required=True)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--max-source-requests", type=int, default=40)
    p.add_argument("--max-search-pages", type=int, default=5)
    p.add_argument("--request-interval", type=float, default=2)
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    p.add_argument("--dry-run", action="store_true")
    return p


def member_proof(conn, run_id, convocation):
    member = conn.execute("""SELECT m.*,c.status AS cache_status,c.result_json AS cache_result
        FROM prod2_accepted_run_members m JOIN prod2_jev_decision_cache c ON c.cache_key=m.request_hash
        WHERE m.run_id=%s AND m.convocation_id=%s""", (run_id, convocation)).fetchone()
    if not member or member["accepted"] is not True or not member["opportunity_id"]:
        raise ValueError("Recovery requires an already accepted canonical member")
    score = member["result_json"].get("affinity_index")
    if (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
            or not 60 <= score <= 100 or member["cache_status"] != "done"
            or not _same_result(member["result_json"], member["cache_result"])):
        raise ValueError("Recovery requires the completed matching Jev decision")
    row, query = member["row_manifest"], member["query_manifest"]
    if (not isinstance(row, dict) or row.get("convocation_id") != convocation
            or not isinstance(query, dict)):
        raise ValueError("Accepted member lacks a bound source observation")
    return {"policy": POLICY, "run_id": run_id, "convocation_id": convocation,
            "opportunity_id": str(member["opportunity_id"]), "request_hash": member["request_hash"],
            "affinity_index": score, "row": row, "query": query}


class SourceBudget:
    def __init__(self, maximum, interval):
        self.maximum, self.requests, self.stopped = maximum, 0, False
        self.pacer = SharedPacer(interval)

    def request(self, request):
        if self.stopped or self.requests >= self.maximum:
            raise RuntimeError("Recovery source budget exhausted or source rejected requests")
        self.pacer(request)
        self.requests += 1

    def response(self, response):
        if response.status_code in {403, 429}:
            self.stopped = True
            raise RuntimeError("Source rejected recovery; stopping before further requests")


def fetch_capture(proof, budget, max_pages):
    query, expected = proof["query"], proof["row"]
    with httpx.Client(timeout=60, verify=True,
            event_hooks={"request": [budget.request], "response": [budget.response]}) as http:
        source = Prod2HttpClient(http)
        source.search(query["keyword"], **native_arguments(query))
        for page in range(1, min(source.counter[4], max_pages) + 1):
            if page > 1:
                source.search_page(page)
            index = next((i for i, row in enumerate(source.rows)
                          if row["convocatoria_id"] == expected["convocation_id"]), None)
            if index is None:
                continue
            row = source.rows[index]
            if (row["procedure_id"] != expected["procedure_id"]
                    or row["description"] != expected["description"]
                    or row["entity"] != expected["buyer_name"]
                    or row["nomenclature"] != expected["nomenclature"]
                    or row["object"] != expected["object_label"]):
                raise ValueError("Accepted source identity changed; recovery cannot reuse its Jev decision")
            parsed = source.detail(index, all_documents=True)
            return {"proof_hash": hash_request(proof), "observed_at": datetime.now(timezone.utc).isoformat(),
                    "detail": {k: v for k, v in parsed.items() if k not in {"http_status", "redirect_statuses"}}}
    raise ValueError("Accepted source row not found within the bounded query pages")


def recover_one(conn, proof, folder, budget, *, max_pages=5):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = folder / "accepted-proof.private.json"
    if marker.exists():
        if read_private(marker) != proof:
            raise ValueError("Pinned recovery membership changed")
    else:
        private_write(marker, proof)
    path = folder / "detail.private.json"
    if path.exists():
        capture = read_private(path)
    else:
        capture = fetch_capture(proof, budget, max_pages)
        private_write(path, capture)
    if capture.get("proof_hash") != hash_request(proof):
        raise ValueError("Recovered ficha is not bound to its accepted member")
    observed = datetime.fromisoformat(capture["observed_at"])
    detail = capture["detail"]
    if detail.get("convocatoria_id") != proof["convocation_id"]:
        raise ValueError("Recovered ficha has a different official identity")
    with conn.transaction():
        saved = persist_detail(conn, detail, observed_at=observed, source_url=SEARCH_URL)
        temporal = decide(detail, observed)
        save_participation_assessment(conn, proof["convocation_id"], detail_hash=saved["detail_hash"], assessment=temporal)
    result = {"convocation_id": proof["convocation_id"], "status": "detail_recovered",
              "affinity_index": proof["affinity_index"], "detail_hash": saved["detail_hash"],
              "document_inventory_complete": detail["document_inventory_complete"],
              "publication_events_count": len(detail["document_events"]),
              "unique_documents_count": len(detail["documents"]), "schedule_count": len(detail["schedule"]),
              "temporal_state": temporal["state"], "document_downloads": 0, "provider_calls": 0,
              "next_stage": "document_selection_pending", "documents": [
                  {"uuid": doc["uuid"], "filename": doc["filename"],
                   "source_url": METADATA_URL + "?id=" + doc["uuid"]} for doc in detail["documents"]]}
    outcome = folder / "recovery.private.json"
    if outcome.exists():
        if read_private(outcome) != result:
            raise ValueError("Recovered outcome changed")
    else:
        private_write(outcome, result)
    return result


def main():
    args = parser().parse_args()
    run_id = _run_id(args.run_id)
    ids = args.convocation_id
    if not 1 <= len(ids) <= 10 or len(set(ids)) != len(ids) or any(n <= 0 for n in ids):
        raise ValueError("Choose one to ten distinct accepted convocatorias")
    if not (1 <= args.max_source_requests <= 100 and 1 <= args.max_search_pages <= 20
            and 1 <= args.request_interval <= 60):
        raise ValueError("Recovery limits must be bounded")
    if args.folder.is_symlink() or args.folder.name == run_id:
        raise ValueError("Use a separate private recovery folder, not the original run")
    if args.dry_run:
        print(json.dumps({"policy": POLICY, "run_id": run_id, "ids": ids,
                          "source_calls": 0, "provider_calls": 0, "document_downloads": 0}))
        return 0
    os.umask(0o077)
    folder = args.folder.resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    budget = SourceBudget(args.max_source_requests, args.request_interval)
    with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
        with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                application_name="buenapro-prod2-detail-recovery", connect_timeout=15) as conn:
            if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                raise RuntimeError("Another PROD2 coordinator owns the executor")
            try:
                if not conn.execute("SELECT to_regclass('prod2_document_events') AS relation").fetchone()["relation"]:
                    raise RuntimeError("Apply migration0041 before recovery")
                proofs = [member_proof(conn, run_id, conv) for conv in ids]
                results = [recover_one(conn, proof, folder / str(proof["convocation_id"]), budget,
                                       max_pages=args.max_search_pages) for proof in proofs]
                report = {"policy": POLICY, "run_id": run_id, "results": results,
                          "source_requests": budget.requests, "provider_calls": 0, "document_downloads": 0}
                private_write(folder / ("report-" + str(datetime.now(timezone.utc).timestamp()) + ".private.json"), report)
                print(json.dumps({**report, "results": [{k: v for k, v in row.items() if k != "documents"}
                                                        for row in results]}, ensure_ascii=False))
            finally:
                if not conn.closed and not conn.broken:
                    conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
