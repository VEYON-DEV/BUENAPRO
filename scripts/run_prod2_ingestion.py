"""Unattended, resumable PROD2 keyword metadata ingestion; PDFs/Gemini are phase 2."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))
RETIRED_EXECUTION = True  # Public execution disabled; historical pure tests can inspect it.

from dotenv import dotenv_values
import httpx
import psycopg
from psycopg.rows import dict_row

from buenapro_worker.prod2 import batch_repository as repo
from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION, JevDescriptionAnalyzer, jev_request_hash
from buenapro_worker.prod2.batch_plan import MEMBER_IDS, configured_keywords, digest, freeze_plan, native_arguments, publication_window
from buenapro_worker.prod2.batch_runner import discover_task, enrich_task, paid_jev_stage, private_write, read_private
from buenapro_worker.prod2.client import Prod2HttpClient
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.settings import Settings


class Pacer:
    def __init__(self, seconds: float):
        self.seconds, self.last = seconds, 0.0

    def __call__(self, request):
        wait = self.seconds - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()


def source_factory(pacer):
    @contextmanager
    def create():
        with httpx.Client(timeout=60, verify=True, event_hooks={"request": [pacer]}) as http:
            yield Prod2HttpClient(http)
    return create


def commercial_snapshot(conn):
    members = []
    with conn.cursor(row_factory=dict_row) as cursor:
        for profile_id in MEMBER_IDS:
            cursor.execute("SELECT identity_json FROM company_profiles WHERE id=%s AND is_active", (profile_id,))
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("Authorized active commercial profile is unavailable")
            description = row["identity_json"].get("descripcion")
            cursor.execute("SELECT nombre FROM business_lines WHERE profile_id=%s AND is_active ORDER BY nombre", (profile_id,))
            lines = [line["nombre"] for line in cursor.fetchall()]
            if not isinstance(description, str) or not description.strip() or not lines:
                raise RuntimeError("Commercial description/lines must exist before freezing this run")
            members.append({"descripcion": description, "lineas": lines})
    return members


def preflight(source_context, years, objects, start, end, folder):
    """Only a verified unfiltered zero can skip a whole exercise/object scope."""
    proofs = []
    for year in years:
        for obj in objects:
            query = {"keyword": "", "year": year, "object_type": obj,
                     "published_from": start, "published_to": end, "seace_version": "3"}
            path = folder / "preflight" / f"{year}-{obj}.private.json"
            if path.exists():
                receipt = read_private(path)
                if receipt["query"] != query:
                    raise RuntimeError("Preflight cache belongs to another frozen interval")
            else:
                with source_context() as source:
                    result = source.search("", **native_arguments(query), allow_empty_keyword=True)
                zero = result["counter"] == [0, 0, 0, 1, 1] and result["rows"] == []
                if zero:
                    with source_context() as verifier:
                        checked = verifier.search("", **native_arguments(query), allow_empty_keyword=True)
                    zero = checked["counter"] == [0, 0, 0, 1, 1] and checked["rows"] == []
                receipt = {"query": query, "observed_at": datetime.now(timezone.utc).isoformat(),
                           "zero_verified": zero, "counter": result["counter"], "rows_count": len(result["rows"])}
                private_write(path, receipt)
            if receipt["zero_verified"]:
                proofs.append({**query, "observed_at": receipt["observed_at"], "total_results": 0,
                               "rows_count": 0, "counter": [0, 0, 0, 1, 1]})
            print(json.dumps({"stage": "preflight", "year": year, "object": obj,
                              "zero_scope": receipt["zero_verified"]}), flush=True)
    return proofs


def recover_paid_receipts(conn, run_id, manifest, folder, jev):
    """Recover completed payments only. Missing responses never authorize paying again."""
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""SELECT id,id_convocatoria,status,error_code FROM prod2_batch_tasks
          WHERE run_id=%s AND kind='enrich' AND
            ((status='running' AND lease_until<=now()) OR (status='blocked' AND
              (error_code='expired_enrichment_lease' OR error_code LIKE 'provider_%%'
               OR error_code IN ('source_403','source_429','metadata_task_failed'))))""", (run_id,))
        tasks = cursor.fetchall()
    for task in tasks:
        locator = repo.discovery_locator(conn, run_id, task["id_convocatoria"])
        description = locator["row_manifest"]["description"]
        request = jev_request_hash(description, commercial_profiles=manifest["commercial_profiles"], model=manifest["jev_model"])
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""SELECT id,status FROM prod2_evaluations WHERE id_convocatoria=%s AND kind='jev_affinity'
              AND request_hash=%s AND model=%s AND policy_version=%s AND profile_id=%s""",
              (task["id_convocatoria"],request,manifest["jev_model"],manifest["jev_policy_version"],manifest["profile_id"]))
            evaluation = cursor.fetchone()
        record = folder / "records" / str(task["id_convocatoria"])
        stem = record / f"jev-{request}"
        if evaluation and evaluation["status"] != "done" and stem.with_suffix(".claim.private.json").exists() and stem.with_suffix(".response.private.json").exists():
            # This path already holds a receipt: paid_jev_stage will complete its
            # original reservation and cannot call the provider here.
            paid_jev_stage(conn, record, task["id_convocatoria"], description, manifest, jev)
            evaluation["status"] = "done"
        if evaluation and evaluation["status"] == "done":
            repo.recover_enrichment_task(conn, task["id"], jev_evaluation_id=evaluation["id"])
        elif evaluation is None or evaluation["status"] == "pending":
            repo.recover_unpaid_enrichment_task(conn, task["id"])


def public_report(conn, run_id):
    report = repo.report_batch(conn, run_id)
    config = report["frozen_manifest"]
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""SELECT a.state,count(*) AS records FROM prod2_batch_memberships m
          JOIN prod2_participation_assessments a USING(id_convocatoria)
          WHERE m.run_id=%s AND a.is_current GROUP BY a.state ORDER BY a.state""", (run_id,))
        states = cursor.fetchall()
        # Read the payment ledger independently of task completion: a paid
        # response remains accountable even if a later metadata step failed.
        cursor.execute("""SELECT e.id,e.status,e.request_hash,e.input_tokens,e.cost_usd,e.claimed_at,
          e.completed_at,m.row_manifest->>'description' AS description
          FROM prod2_batch_memberships m JOIN prod2_evaluations e USING(id_convocatoria)
          WHERE m.run_id=%s AND e.kind='jev_affinity' AND e.profile_id=%s AND e.model=%s AND e.policy_version=%s""",
          (run_id,config["profile_id"],config["jev_model"],config["jev_policy_version"]))
        ledger = cursor.fetchall()
    requests, usage = {}, {"by_status":{},"input_tokens_known":0,"known_attributed_estimate_usd":0.0,
                          "reservations_started_during_run":0,"known_started_during_run_estimate_usd":0.0,
                          "unknown_billing_reservations":0,"invoice_total_usd":None}
    for evaluation in ledger:
        description = evaluation["description"]
        if description not in requests:
            requests[description] = jev_request_hash(description,commercial_profiles=config["commercial_profiles"],model=config["jev_model"])
        if evaluation["request_hash"] != requests[description]:
            continue
        status = evaluation["status"]
        usage["by_status"][status] = usage["by_status"].get(status,0)+1
        started = evaluation["claimed_at"] is not None and evaluation["claimed_at"] >= report["created_at"]
        usage["reservations_started_during_run"] += int(started)
        if status == "done":
            usage["input_tokens_known"] += evaluation["input_tokens"] or 0
            if evaluation["cost_usd"] is None:
                usage["unknown_billing_reservations"] += 1
            else:
                cost = float(evaluation["cost_usd"])
                usage["known_attributed_estimate_usd"] += cost
                if started:
                    usage["known_started_during_run_estimate_usd"] += cost
        elif status != "pending":
            usage["unknown_billing_reservations"] += 1
    return {"run_id": run_id,"status":report["status"],"phase":report["phase"],
            "coverage_status":report["coverage_status"],"blocked_reason":report["blocked_reason"],
            "published_from":config["published_from"],"published_to":config["published_to"],
            "procedure_years":config["procedure_years"],"keywords":len(config["keywords"]),
            "objects":config["object_types"],"planned_queries":config["planned_query_count"],
            "zero_preflight_skipped_queries":config["preflight_skipped_query_count"],
            "tasks":report["tasks"],"unique_records":report["unique_candidates"],
            "temporal_states":states,"jev_usage_including_reused_evaluations":usage,
            "pdf_downloads_this_pipeline":0,"gemini_calls_this_pipeline":0,
            "coverage_scope":config["coverage_scope"],"source_atomic_snapshot":False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--folder", type=Path, required=True)
    result.add_argument("--production", action="store_true", help="Use the existing production PG through a private SSH tunnel")
    result.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    result.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    result.add_argument("--config", type=Path, default=ROOT / "docs/sources/prod2-convenio-keywords.v4.json")
    result.add_argument("--months", type=int, default=6)
    result.add_argument("--from-date")
    result.add_argument("--to-date")
    result.add_argument("--years", default="all")
    result.add_argument("--objects", default="good,service")
    result.add_argument("--keyword", action="append", help="Explicit subset for a separate bounded validation run")
    result.add_argument("--skip-preflight", action="store_true")
    result.add_argument("--request-interval", type=float, default=2.0)
    result.add_argument("--max-work-items", type=int, help="Stop after this many tasks, keeping partial coverage")
    result.add_argument("--dry-run", action="store_true", help="Pure local planning; no HTTP/DB/provider calls")
    result.add_argument("--report-only", action="store_true")
    return result


@contextmanager
def settings_context(args):
    if args.production:
        with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            yield settings
    else:
        yield Settings()


def main():
    args = parser().parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    inventory = json.loads((args.config.parent / config["keyword_inventory_file"]).read_text())
    all_keywords = configured_keywords(inventory)
    canonical_keywords = {word.casefold():word for word in all_keywords}
    requested_words = args.keyword or all_keywords
    if any(word.strip().casefold() not in canonical_keywords for word in requested_words):
        raise ValueError("Requested keyword is not configured")
    keywords = list(dict.fromkeys(canonical_keywords[word.strip().casefold()] for word in requested_words))
    if len(all_keywords) != config["keyword_count"] or any(word not in all_keywords for word in keywords):
        raise ValueError("Keyword inventory changed or subset is not configured")
    objects = args.objects.split(",")
    if len(set(objects)) != len(objects) or not objects or set(objects) - {"good","service"}:
        raise ValueError("Use unique good/service objects")
    if not 1 <= args.request_interval <= 60 or (args.max_work_items is not None and args.max_work_items < 1):
        raise ValueError("Invalid pacing/work bound")
    configured_years = None
    if args.years != "all":
        if not re.fullmatch(r"[0-9]{4}(,[0-9]{4})*",args.years):
            raise ValueError("Use all or comma-separated four-digit exercise years")
        configured_years = [int(year) for year in args.years.split(",")]
        if len(configured_years) != len(set(configured_years)) or any(not 2000<=year<=2100 for year in configured_years):
            raise ValueError("Configured years must be unique and bounded")
    as_of = datetime.now(timezone.utc)
    start, end = publication_window(as_of, months=args.months,published_from=args.from_date,published_to=args.to_date)
    requested = {"months":args.months,"from_date":args.from_date,"to_date":args.to_date,"years":args.years,
                 "objects":objects,"keywords":keywords,"config_hash":digest(config),"inventory_hash":digest(inventory),
                 "skip_preflight":args.skip_preflight}
    if args.dry_run:
        print(json.dumps({"dry_run":True,"published_from":start,"published_to":end,"keywords":len(keywords),
          "objects":objects,"years":args.years,"native_years_require_http_discovery":args.years=="all",
          "pipeline":"metadata_jev_schedule_only","pdf_downloads":False,"gemini":False},ensure_ascii=False))
        return 0
    if RETIRED_EXECUTION and not args.report_only:
        print(json.dumps({"status":"retired", "reason":"Superseded by accepted-only Jev>=60 pipeline ending at LLM1",
                          "replacement":"scripts/run_prod2_accepted.py"}), file=sys.stderr)
        return 3
    folder = args.folder.resolve()
    folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    if args.folder.is_symlink():
        raise ValueError("Run directory must not be a symlink")
    os.chmod(folder,0o700)
    run_id = folder.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}",run_id):
        raise ValueError("Run folder needs a safe, unique name")
    create_source = source_factory(Pacer(args.request_interval))
    with settings_context(args) as settings, psycopg.connect(settings.database_url,autocommit=True,row_factory=dict_row) as conn:
        if args.report_only:
            print(json.dumps(public_report(conn,run_id),default=str,ensure_ascii=False))
            return 0
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_metadata_executor'),0) AS locked").fetchone()["locked"]:
            raise RuntimeError("Another metadata collector owns the global executor lock")
        try:
            manifest_path = folder / "manifest.private.json"
            if manifest_path.exists():
                envelope = read_private(manifest_path)
                if envelope["requested"] != requested:
                    raise ValueError("Resume requires the same scope flags/config; use a new folder for a changed run")
                manifest = envelope["manifest"]
            else:
                init_path = folder / "initialization.private.json"
                if init_path.exists():
                    init = read_private(init_path)
                    if init["requested"] != requested:
                        raise ValueError("Initialization belongs to another requested scope")
                else:
                    with create_source() as source:
                        source.bootstrap()
                        available = source.available_convocation_years()
                    years = available if args.years == "all" else configured_years
                    if len(years) != len(set(years)) or not years or set(years)-set(available):
                        raise ValueError("Configured years must exist in the current native selector")
                    init = {"requested":requested,"as_of":as_of.isoformat(),"start":start,"end":end,
                            "years":years,"commercial_profiles":commercial_snapshot(conn)}
                    private_write(init_path,init)
                proofs = [] if args.skip_preflight else preflight(create_source,init["years"],objects,init["start"],init["end"],folder)
                manifest = freeze_plan(as_of=datetime.fromisoformat(init["as_of"]),keywords=keywords,years=init["years"],
                  published_from=init["start"],published_to=init["end"],commercial_profiles=init["commercial_profiles"],
                  profile_id=MEMBER_IDS[0],jev_model=JEV_MODEL,jev_policy=JEV_POLICY_VERSION,
                  keyword_config_hash=digest(inventory),zero_result_scopes=proofs)
                manifest["object_types"] = objects
                manifest, _ = repo.plan_queries(manifest)
                private_write(manifest_path,{"requested":requested,"manifest":manifest})
            repo.create_batch(conn,run_id,manifest)
            for row in conn.execute("""SELECT id FROM prod2_batch_tasks WHERE run_id=%s AND kind='discovery'
              AND status='blocked' AND error_code IN ('source_403','source_429','metadata_task_failed')""",(run_id,)).fetchall():
                repo.recover_discovery_task(conn,row["id"])
            jev = JevDescriptionAnalyzer(dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_API_KEY"),
                                         model=manifest["jev_model"])
            recover_paid_receipts(conn,run_id,manifest,folder,jev)
            owner = "metadata-" + uuid4().hex
            completed = 0
            while args.max_work_items is None or completed < args.max_work_items:
                phase = repo.advance_phase(conn,run_id)
                if phase["status"] in {"blocked","complete"}:
                    break
                task = repo.claim_task(conn,run_id,owner_id=owner,lease_seconds=3600)
                if task["status"] != "claimed":
                    break
                try:
                    if task["kind"] == "discovery":
                        outcome = discover_task(conn,task,repo,create_source)
                        while outcome == "restarted":
                            current = conn.execute("SELECT * FROM prod2_batch_tasks WHERE id=%s",(task["task_id"],)).fetchone()
                            if current["status"] != "running":
                                break
                            task.update(discovery_run_id=current["discovery_run_id"],checkpoint=current["checkpoint_json"])
                            outcome = discover_task(conn,task,repo,create_source)
                    else:
                        outcome = enrich_task(conn,task,repo,manifest,folder,create_source,jev)
                    completed += 1
                    print(json.dumps({"stage":task["kind"],"task_id":task["task_id"],"outcome":outcome},default=str),flush=True)
                except Exception as error:
                    message = str(error)
                    code = "source_403" if "PROD2 HTTP status 403" in message else "source_429" if "PROD2 HTTP status 429" in message else "provider_reconciliation" if task["kind"]=="enrich" else "metadata_task_failed"
                    try:
                        repo.block_task(conn,task["task_id"],task["owner_token"],error_code=code)
                    except Exception:
                        pass
                    print(json.dumps({"stage":task["kind"],"error_code":code,"error_type":type(error).__name__}),flush=True)
                    break
            repo.advance_phase(conn,run_id)
            report = public_report(conn,run_id)
            report_path = folder / f"report-{time.time_ns()}.private.json"
            private_write(report_path,report)
            print(json.dumps(report,default=str,ensure_ascii=False),flush=True)
            return 2 if report["status"] == "blocked" else 0
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_metadata_executor'),0)")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Do not emit exception text, connection URLs, provider keys or raw HTML.
        print(json.dumps({"status":"failed","error_type":type(error).__name__}),file=sys.stderr)
        raise SystemExit(1)
