"""PostgreSQL checkpoints for frozen, bounded PROD2 metadata loads.

No HTTP/model calls or migration application. Commit task claims before I/O.
Each mutation is transactional; an existing outer transaction remains owned by
the caller. Discovery I/O leases may be reclaimed; expired enrichment leases
block the run because remote billing must be reconciled separately. Jev uses
the permanent prod2_evaluations reservation/cache, never the task lease alone.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from buenapro_worker.prod2.analysis import JEV_POLICY_VERSION, jev_request_hash
from buenapro_worker.prod2.discovery import _hash, _json, import_capture, validate_capture
from buenapro_worker.prod2.participation import POLICY_VERSION as PARTICIPATION_POLICY_VERSION

PARTICIPATION_STATES = {"closed", "adjudicado", "cancelado", "desierto", "suspendido", "unknown",
                        "pending_registration", "registration_open", "registered_only", "submission_open"}
SECRET_KEYS = {"api_key", "apikey", "password", "secret", "token", "cookies", "cookie",
               "authorization", "alf_ticket", "javax.faces.viewstate", "database_url"}
RECOVERABLE_SOURCE_ERRORS = {"source_403", "source_429", "metadata_task_failed"}


class BatchError(ValueError):
    """Safe orchestration error without credentials, opaque state or response bodies."""


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise BatchError("Expected a structured object")
    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.casefold() in SECRET_KEYS:
                    raise BatchError("Secrets/session state do not belong in durable manifests or checkpoints")
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    walk(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError()
        return json.loads(encoded)
    except (TypeError, ValueError):
        raise BatchError("Expected bounded, finite JSON data") from None


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", value):
        raise BatchError("Invalid durable identifier")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise BatchError("Expected lowercase SHA256")
    return value


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        raise BatchError("Invalid ownership/profile UUID") from None


def _positive(value: Any) -> int:
    if type(value) is not int or not 1 <= value < 2**63:
        raise BatchError("Invalid task/evaluation/convocation identity")
    return value


def _date(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BatchError("Expected ISO publication date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BatchError("Invalid publication date") from None


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError:
        raise BatchError("Invalid observation time") from None
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BatchError("Observation time requires timezone")
    return parsed.astimezone(timezone.utc)


def _strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list) or not value:
        raise BatchError("A nonempty configured list is required")
    result = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or len(entry) > limit:
            raise BatchError("Invalid configured text")
        result.append(" ".join(entry.split()))
    if len(set(result)) != len(result):
        raise BatchError("Configured list contains duplicate values")
    return result


def plan_queries(manifest: dict) -> tuple[dict, list[dict]]:
    """Cross native procedure years with independent publication-calendar slices."""
    config = _object(manifest)
    required = {"published_from", "published_to", "procedure_years", "keywords", "object_types",
                "seace_version", "profile_id", "commercial_profiles", "jev_model", "jev_policy_version"}
    if not required <= set(config):
        raise BatchError("Frozen manifest is missing scope or Jev binding fields")
    start, end = _date(config["published_from"]), _date(config["published_to"])
    if start > end:
        raise BatchError("Reversed publication range")
    years = config["procedure_years"]
    if (not isinstance(years, list) or not years
            or any(type(year) is not int or not 2000 <= year <= 2100 for year in years)
            or len(set(years)) != len(years)):
        raise BatchError("Explicit native procedure years are required")
    keywords, objects = _strings(config["keywords"], 120), _strings(config["object_types"], 20)
    if set(objects) - {"good", "service"} or config["seace_version"] != "3":
        raise BatchError("Only configured PROD2 goods/services SEACE3 are supported")
    if config.get("phase_policy", "discovery_then_enrich") != "discovery_then_enrich":
        raise BatchError("Discovery must finish before enrichment")
    config.update(phase_policy="discovery_then_enrich", keywords=keywords, object_types=objects,
                  profile_id=_uuid(config["profile_id"]))
    if config.get("pipeline", "metadata_jev_schedule_only") != "metadata_jev_schedule_only":
        raise BatchError("This batch implements only metadata, Jev and official schedules")
    config.update(pipeline="metadata_jev_schedule_only")
    if config.get("participation_policy_version", PARTICIPATION_POLICY_VERSION) != PARTICIPATION_POLICY_VERSION:
        raise BatchError("Unsupported temporal participation policy")
    config["participation_policy_version"] = PARTICIPATION_POLICY_VERSION
    if config["jev_policy_version"] != JEV_POLICY_VERSION:
        raise BatchError("Configured Jev policy is not supported by the current handler")
    # Validate the exact input shape without contacting the provider.
    jev_request_hash("configuration validation", commercial_profiles=config["commercial_profiles"], model=config["jev_model"])
    for field in ("max_io_attempts", "max_snapshot_restarts"):
        bound = config.get(field, 3)
        if type(bound) is not int or not 1 <= bound <= 10:
            raise BatchError("Retry/restart bounds must be between one and ten")
        config[field] = bound
    windows = []
    cursor = start
    while cursor <= end:
        last = min(end, date(cursor.year, 12, 31))
        windows.append((cursor.isoformat(), last.isoformat()))
        cursor = last + timedelta(days=1)
    queries = [{"keyword": keyword, "object_type": object_type, "year": year,
                "published_from": first, "published_to": last, "seace_version": "3"}
               for keyword in keywords for object_type in objects for year in years for first, last in windows]
    proofs = config.get("zero_result_scopes", [])
    if not isinstance(proofs, list):
        raise BatchError("Expected a list of verified broad zero-result scopes")
    for proof in proofs:
        if not isinstance(proof, dict):
            raise BatchError("Invalid zero-result scope proof")
        counter = proof.get("counter")
        if (proof.get("keyword") != "" or proof.get("seace_version") != "3"
                or type(proof.get("total_results")) is not int or proof["total_results"] != 0
                or not isinstance(counter, list) or len(counter)!=5 or any(type(n) is not int for n in counter)
                or counter != [0,0,0,1,1]
                or not ((type(proof.get("rows_count")) is int and proof["rows_count"]==0) or proof.get("rows")==[])
                or ("rows" in proof and proof["rows"]!=[])
                or proof.get("year") not in years or proof.get("object_type") not in objects):
            raise BatchError("Only verified broad unfiltered zero results can omit keyword queries")
        first, last = _date(proof.get("published_from")), _date(proof.get("published_to"))
        observed = _time(proof.get("observed_at"))
        if first>last or observed>datetime.now(timezone.utc)+timedelta(minutes=5):
            raise BatchError("Invalid zero-result proof dates")
    remaining = [query for query in queries if not any(
        proof["year"]==query["year"] and proof["object_type"]==query["object_type"]
        and proof["published_from"]<=query["published_from"] and proof["published_to"]>=query["published_to"]
        for proof in proofs)]
    config.update(zero_result_scopes=proofs, planned_query_count=len(queries),
                  preflight_skipped_query_count=len(queries)-len(remaining))
    return config, remaining


def split_query(query: dict) -> tuple[dict, dict] | None:
    """Inclusive halves with no holes/overlap; a one-day capped query is blocked."""
    start, end = _date(query["published_from"]), _date(query["published_to"])
    if start > end:
        raise BatchError("Reversed partition")
    if start == end:
        return None
    middle = start + timedelta(days=(end - start).days // 2)
    return ({**query, "published_to": middle.isoformat()},
            {**query, "published_from": (middle + timedelta(days=1)).isoformat()})


def _one(cursor: Any, statement: str, params: tuple = ()) -> dict | None:
    cursor.execute(statement, params)
    return cursor.fetchone()


def _discovery_pass(run_id: str, task_key: str, restart: int = 0) -> str:
    return f"batch:{_hash(run_id)[:20]}:{task_key[:40]}:r{restart}"


def create_batch(conn: Any, run_id: str, frozen_manifest: dict) -> dict:
    run_id = _identifier(run_id)
    config, queries = plan_queries(frozen_manifest)
    digest = _hash(config)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""INSERT INTO prod2_batch_runs(run_id,manifest_hash,frozen_manifest)
            VALUES (%s,%s,%s::jsonb) ON CONFLICT(run_id) DO NOTHING""", (run_id, digest, _json(config)))
        run = _one(cursor, "SELECT * FROM prod2_batch_runs WHERE run_id=%s FOR UPDATE", (run_id,))
        if run["manifest_hash"] != digest or run["frozen_manifest"] != config:
            raise BatchError("Run configuration changed; use a new run_id")
        cursor.executemany("""INSERT INTO prod2_batch_tasks(run_id,task_key,kind,query_manifest,
            discovery_run_id,checkpoint_json) VALUES (%s,%s,'discovery',%s::jsonb,%s,'{"next_page":1}'::jsonb)
            ON CONFLICT(run_id,task_key) DO NOTHING""", [(run_id, _hash(query), _json(query),
                _discovery_pass(run_id, _hash(query))) for query in queries])
        cursor.execute("""INSERT INTO prod2_batch_task_passes(task_id,discovery_run_id,restart_number)
            SELECT id,discovery_run_id,snapshot_restarts FROM prod2_batch_tasks WHERE run_id=%s AND kind='discovery'
            ON CONFLICT(discovery_run_id) DO NOTHING""", (run_id,))
    return {"run_id": run_id, "manifest_hash": digest, "seeded_queries": len(queries),
            "phase": run["phase"], "status": run["status"], "coverage_status": run["coverage_status"]}


def _block_run(cursor: Any, run_id: str, reason: str) -> None:
    cursor.execute("""UPDATE prod2_batch_runs SET status='blocked',blocked_reason=%s,
        coverage_status=CASE WHEN phase='discovery' THEN 'blocked' ELSE coverage_status END,
        updated_at=now() WHERE run_id=%s AND status<>'complete'""", (reason, run_id))


def _lease_seconds(value: int) -> int:
    if type(value) is not int or not 30 <= value <= 3600:
        raise BatchError("Lease must be bounded between 30 and 3600 seconds")
    return value


def claim_task(conn: Any, run_id: str, *, owner_id: str, lease_seconds: int = 600) -> dict:
    run_id, owner = _identifier(run_id), _identifier(owner_id)
    seconds, token = _lease_seconds(lease_seconds), str(uuid4())
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        run = _one(cursor, "SELECT * FROM prod2_batch_runs WHERE run_id=%s FOR UPDATE", (run_id,))
        if not run:
            raise BatchError("Unknown run")
        if run["status"] != "active":
            return {"status": run["status"], "run_id": run_id, "phase": run["phase"]}
        kind = "discovery" if run["phase"] == "discovery" else "enrich"
        if kind == "enrich":
            expired = _one(cursor, """UPDATE prod2_batch_tasks SET status='blocked',error_code='expired_enrichment_lease',
                updated_at=now() WHERE run_id=%s AND kind='enrich' AND status='running'
                AND lease_until<=clock_timestamp() RETURNING id""", (run_id,))
            if expired:
                _block_run(cursor, run_id, "expired_enrichment_lease")
                return {"status": "blocked", "run_id": run_id, "phase": run["phase"]}
        task = _one(cursor, """SELECT * FROM prod2_batch_tasks WHERE run_id=%s AND kind=%s
            AND (status='pending' OR (kind='discovery' AND status='running' AND lease_until<=clock_timestamp()))
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""", (run_id, kind))
        if not task:
            return {"status": "idle", "run_id": run_id, "phase": run["phase"]}
        if task["attempts"] >= run["frozen_manifest"]["max_io_attempts"]:
            cursor.execute("UPDATE prod2_batch_tasks SET status='blocked',error_code='io_claims_exhausted',updated_at=now() WHERE id=%s", (task["id"],))
            _block_run(cursor, run_id, "io_claims_exhausted")
            return {"status": "blocked", "run_id": run_id, "phase": run["phase"]}
        row = _one(cursor, """UPDATE prod2_batch_tasks SET status='running',owner_id=%s,owner_token=%s,
            claimed_at=clock_timestamp(),lease_until=clock_timestamp()+(%s*interval '1 second'),attempts=attempts+1,
            updated_at=now() WHERE id=%s RETURNING *""", (owner, token, seconds, task["id"]))
    return {**row, "task_id": int(row["id"]), "status": "claimed", "query": row["query_manifest"],
            "checkpoint": row["checkpoint_json"], "owner_token": str(row["owner_token"])}


def _owned(cursor: Any, task_id: int, owner_token: str) -> tuple[dict, dict]:
    identity, token = _positive(task_id), _uuid(owner_token)
    link = _one(cursor, "SELECT run_id FROM prod2_batch_tasks WHERE id=%s", (identity,))
    if not link:
        raise BatchError("Unknown task")
    # Every mutation locks run before task, matching the claim/phase lock order.
    run = _one(cursor, "SELECT * FROM prod2_batch_runs WHERE run_id=%s FOR UPDATE", (link["run_id"],))
    task = _one(cursor, "SELECT *, (lease_until>clock_timestamp()) AS owner_live FROM prod2_batch_tasks WHERE id=%s FOR UPDATE", (identity,))
    if run["status"] != "active" or task["status"] != "running" or str(task["owner_token"]) != token or not task["owner_live"]:
        raise BatchError("Task is not owned by this live lease")
    expected = "discovery" if task["kind"] == "discovery" else "enrichment"
    if run["phase"] != expected:
        raise BatchError("Task does not belong to the active phase")
    return run, task


def renew_lease(conn: Any, task_id: int, owner_token: str, *, lease_seconds: int = 600) -> dict:
    seconds = _lease_seconds(lease_seconds)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        _, task = _owned(cursor, task_id, owner_token)
        return _one(cursor, "UPDATE prod2_batch_tasks SET lease_until=clock_timestamp()+(%s*interval '1 second'),updated_at=now() WHERE id=%s RETURNING lease_until", (seconds, task["id"]))


def update_checkpoint(conn: Any, task_id: int, owner_token: str, checkpoint: dict) -> None:
    progress = _object(checkpoint)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        _, task = _owned(cursor, task_id, owner_token)
        if task["kind"] == "discovery":
            raise BatchError("Discovery page checkpoints are advanced only by persist_page")
        cursor.execute("UPDATE prod2_batch_tasks SET checkpoint_json=%s::jsonb,updated_at=now() WHERE id=%s",
            (_json({**task["checkpoint_json"], **progress}), task["id"]))


def discovery_checkpoint(conn: Any, task_id: int, owner_token: str) -> dict:
    """Read the current pass's immutable totals/first page for native revalidation."""
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        _, task = _owned(cursor, task_id, owner_token)
        if task["kind"] != "discovery":
            raise BatchError("A discovery lease is required")
        saved = _one(cursor, """SELECT r.total_results,r.total_pages,r.page_size,r.status,
            p.page_hash AS first_page_hash,p.rows_manifest,p.observed_at AS first_observed_at
            FROM prod2_discovery_runs r LEFT JOIN prod2_discovery_pages p
              ON p.run_id=r.run_id AND p.page_number=1 WHERE r.run_id=%s""", (task["discovery_run_id"],))
    return {"discovery_run_id":task["discovery_run_id"],"checkpoint":task["checkpoint_json"],"saved_pass":saved}


def persist_page(conn: Any, task_id: int, owner_token: str, value: dict) -> dict:
    """The producer must recheck the native total/first-page identity BEFORE its last page.

    Coverage means a verified bounded pass; it does not prove an instantaneous
    portal snapshot. A changed pass must call restart_discovery_task instead.
    """
    capture = validate_capture(value)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        run, task = _owned(cursor, task_id, owner_token)
        if task["kind"] != "discovery" or task["query_manifest"] != capture.query:
            raise BatchError("Captured query does not match the owned frozen partition")
        expected = task["checkpoint_json"].get("next_page", 1)
        if capture.page["number"] > expected:
            raise BatchError("Cannot skip an unpersisted page")
        result = import_capture(conn, value, run_id=task["discovery_run_id"])
        for row in capture.rows:
            cursor.execute("""INSERT INTO prod2_batch_memberships(run_id,id_convocatoria,row_hash,row_manifest,
                first_observed_at,last_observed_at) VALUES (%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT(run_id,id_convocatoria) DO UPDATE SET row_hash=EXCLUDED.row_hash,
                  row_manifest=EXCLUDED.row_manifest,last_observed_at=EXCLUDED.last_observed_at
                WHERE EXCLUDED.last_observed_at>=prod2_batch_memberships.last_observed_at""",
                (run["run_id"], row["convocation_id"], _hash(row), _json(row), capture.observed_at, capture.observed_at))
        checkpoint = {"next_page": max(expected, capture.page["number"] + 1),
                      "total_pages": capture.page["total_pages"], "total_results": capture.page["total_results"]}
        if capture.page["number"] < expected and not result["replayed"]:
            raise BatchError("An earlier checkpoint page was not a durable replay")
        complete = result["status"] == "complete"
        cursor.execute("""UPDATE prod2_batch_tasks SET status=%s,checkpoint_json=%s::jsonb,result_json=%s::jsonb,
            completed_at=CASE WHEN %s THEN now() ELSE NULL END,updated_at=now() WHERE id=%s""",
            ("complete" if complete else "running", _json(checkpoint), _json(result), complete, task["id"]))
    return {**result, "task_id": task_id, "next_page": checkpoint["next_page"]}


def split_capped_task(conn: Any, task_id: int, owner_token: str, *, total_results: int) -> dict:
    if type(total_results) is not int or total_results < 499:
        raise BatchError("Only capped query totals are adaptively partitioned")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        run, task = _owned(cursor, task_id, owner_token)
        if task["kind"] != "discovery":
            raise BatchError("Only discovery queries can be partitioned")
        children = split_query(task["query_manifest"])
        if children is None:
            cursor.execute("UPDATE prod2_batch_tasks SET status='blocked',error_code='single_day_result_cap',updated_at=now() WHERE id=%s", (task["id"],))
            _block_run(cursor, run["run_id"], "single_day_result_cap")
            return {"status": "blocked", "reason": "single_day_result_cap", "task_id": task_id}
        for query in children:
            key = _hash(query)
            cursor.execute("""INSERT INTO prod2_batch_tasks(run_id,task_key,kind,query_manifest,parent_task_id,
                discovery_run_id,checkpoint_json) VALUES (%s,%s,'discovery',%s::jsonb,%s,%s,'{"next_page":1}'::jsonb)
                ON CONFLICT(run_id,task_key) DO NOTHING""", (run["run_id"], key, _json(query), task_id, _discovery_pass(run["run_id"], key)))
        cursor.execute("""INSERT INTO prod2_batch_task_passes(task_id,discovery_run_id,restart_number)
            SELECT id,discovery_run_id,snapshot_restarts FROM prod2_batch_tasks WHERE parent_task_id=%s
            ON CONFLICT(discovery_run_id) DO NOTHING""", (task_id,))
        cursor.execute("""UPDATE prod2_batch_tasks SET status='split',result_json=%s::jsonb,
            completed_at=now(),updated_at=now() WHERE id=%s""", (_json({"reported_total": total_results, "children": children}), task_id))
    return {"status": "split", "task_id": task_id, "children": children}


def restart_discovery_task(conn: Any, task_id: int, owner_token: str) -> dict:
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        run, task = _owned(cursor, task_id, owner_token)
        if task["kind"] != "discovery":
            raise BatchError("Only discovery passes can restart without API billing reconciliation")
        restart = task["snapshot_restarts"] + 1
        if restart > run["frozen_manifest"]["max_snapshot_restarts"]:
            cursor.execute("UPDATE prod2_batch_tasks SET status='blocked',error_code='snapshot_restarts_exhausted',updated_at=now() WHERE id=%s", (task_id,))
            _block_run(cursor, run["run_id"], "snapshot_restarts_exhausted")
            return {"status": "blocked", "task_id": task_id}
        pass_id = _discovery_pass(run["run_id"], task["task_key"], restart)
        cursor.execute("""UPDATE prod2_batch_tasks SET discovery_run_id=%s,snapshot_restarts=%s,
            checkpoint_json='{"next_page":1}'::jsonb,result_json=NULL,error_code='snapshot_changed',
            completed_at=NULL,updated_at=now() WHERE id=%s""", (pass_id, restart, task_id))
        cursor.execute("INSERT INTO prod2_batch_task_passes(task_id,discovery_run_id,restart_number) VALUES (%s,%s,%s)",
            (task_id,pass_id,restart))
    return {"status": "running", "task_id": task_id, "discovery_run_id": pass_id, "next_page": 1}


def block_task(conn: Any, task_id: int, owner_token: str, *, error_code: str) -> None:
    if not isinstance(error_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error_code):
        raise BatchError("Expected a sanitized failure code")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        run, task = _owned(cursor, task_id, owner_token)
        cursor.execute("UPDATE prod2_batch_tasks SET status='blocked',error_code=%s,updated_at=now() WHERE id=%s", (error_code, task["id"]))
        _block_run(cursor, run["run_id"], error_code)


def _expected_jev(cursor: Any, run: dict, convocation: int) -> str:
    config=run["frozen_manifest"]
    member=_one(cursor,"""SELECT m.row_manifest,p.description FROM prod2_batch_memberships m
        JOIN prod2_discoveries p USING(id_convocatoria) WHERE m.run_id=%s AND m.id_convocatoria=%s FOR UPDATE OF p""",
        (run["run_id"],convocation))
    if not member or member["row_manifest"]["description"]!=member["description"]:
        raise BatchError("Frozen batch description changed; discovery needs reconciliation")
    return jev_request_hash(member["description"],commercial_profiles=config["commercial_profiles"],model=config["jev_model"])


def _bound_jev(cursor: Any, run: dict, convocation: int, evaluation: int) -> dict:
    config=run["frozen_manifest"]
    request=_expected_jev(cursor,run,convocation)
    ev=_one(cursor,"SELECT * FROM prod2_evaluations WHERE id=%s",(evaluation,))
    if not ev or (ev["id_convocatoria"],ev["kind"],ev["status"],str(ev["profile_id"]),ev["model"],ev["policy_version"],ev["request_hash"]) != (
            convocation,"jev_affinity","done",config["profile_id"],config["jev_model"],config["jev_policy_version"],request):
        raise BatchError("Jev evaluation does not bind the frozen description/profile/model/policy")
    return ev


def _recovery_task(cursor: Any, task_id: int) -> tuple[dict,dict]:
    link=_one(cursor,"SELECT run_id FROM prod2_batch_tasks WHERE id=%s",(_positive(task_id),))
    if not link:
        raise BatchError("Unknown task")
    run=_one(cursor,"SELECT * FROM prod2_batch_runs WHERE run_id=%s FOR UPDATE",(link["run_id"],))
    task=_one(cursor,"SELECT *, (lease_until<=clock_timestamp()) AS expired FROM prod2_batch_tasks WHERE id=%s FOR UPDATE",(task_id,))
    return run,task


def _recoverable_enrichment(run: dict, task: dict) -> bool:
    code=task["error_code"] or ""
    eligible=(task["status"]=="running" and task["expired"]) or (task["status"]=="blocked" and
        (code=="expired_enrichment_lease" or code.startswith("provider_") or code in RECOVERABLE_SOURCE_ERRORS))
    return task["kind"]=="enrich" and run["phase"]=="enrichment" and run["status"]!="complete" and eligible


def _reset_recovered_task(cursor: Any, run: dict, task: dict) -> None:
    cursor.execute("""UPDATE prod2_batch_tasks SET status='pending',error_code=NULL,
        owner_id=NULL,owner_token=NULL,lease_until=NULL,updated_at=now() WHERE id=%s""",(task["id"],))
    others=_one(cursor,"SELECT count(*) AS blocked FROM prod2_batch_tasks WHERE run_id=%s AND status='blocked'",(run["run_id"],))
    if not others["blocked"]:
        cursor.execute("""UPDATE prod2_batch_runs SET status='active',blocked_reason=NULL,
            coverage_status=CASE WHEN phase='discovery' THEN 'pending' ELSE coverage_status END,
            updated_at=now() WHERE run_id=%s""",(run["run_id"],))


def recover_discovery_task(conn: Any, task_id: int) -> dict:
    """Explicit rerun of a source I/O failure; retain pages and bounded attempts."""
    identity=_positive(task_id)
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cursor:
        run,task=_recovery_task(cursor,identity)
        if (task["kind"]!="discovery" or run["phase"]!="discovery" or run["status"]=="complete"
                or task["status"]!="blocked" or task["error_code"] not in RECOVERABLE_SOURCE_ERRORS
                or task["attempts"]>=run["frozen_manifest"]["max_io_attempts"]):
            raise BatchError("Only bounded source-failed discovery can be resumed")
        _reset_recovered_task(cursor,run,task)
    return {"task_id":identity,"status":"pending"}


def recover_unpaid_enrichment_task(conn: Any, task_id: int) -> dict:
    """Recover source I/O only while the exact Jev request remains unclaimed.

    Insert a pending reservation if absent, then lock its unique key. This
    serializes recovery with the provider claim; no provider call or billing
    attempt is made, and running/uncertain/failed reservations stay blocked.
    """
    identity=_positive(task_id)
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cursor:
        run,task=_recovery_task(cursor,identity)
        if not _recoverable_enrichment(run,task):
            raise BatchError("Only expired/source-blocked enrichment can be reconciled")
        config,convocation=run["frozen_manifest"],task["id_convocatoria"]
        request=_expected_jev(cursor,run,convocation)
        key=_hash({"convocation":convocation,"kind":"jev_affinity","request_hash":request,
            "model":config["jev_model"],"policy_version":config["jev_policy_version"],"profile_id":config["profile_id"]})
        cursor.execute("""INSERT INTO prod2_evaluations(evaluation_key,id_convocatoria,kind,
            request_hash,model,policy_version,profile_id) VALUES (%s,%s,'jev_affinity',%s,%s,%s,%s)
            ON CONFLICT(evaluation_key) DO NOTHING""",(key,convocation,request,config["jev_model"],
                config["jev_policy_version"],config["profile_id"]))
        ev=_one(cursor,"SELECT id,status FROM prod2_evaluations WHERE evaluation_key=%s FOR UPDATE",(key,))
        if not ev or ev["status"]!="pending":
            raise BatchError("Jev may already have been billed; exact completion must be reconciled")
        _reset_recovered_task(cursor,run,task)
    return {"task_id":identity,"status":"pending","jev_evaluation_id":int(ev["id"])}


def recover_enrichment_task(conn: Any, task_id: int, jev_evaluation_id: int) -> dict:
    """Recover metadata I/O only after exact paid Jev completion has been proved.

    Never retries an API, changes its reservation, or erases its checkpoint.
    The caller must reconcile a private response with its original claim first.
    """
    identity,evaluation=_positive(task_id),_positive(jev_evaluation_id)
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cursor:
        run,task=_recovery_task(cursor,identity)
        if not _recoverable_enrichment(run,task):
            raise BatchError("Only expired/provider-blocked enrichment can be reconciled")
        _bound_jev(cursor,run,task["id_convocatoria"],evaluation)
        _reset_recovered_task(cursor,run,task)
    return {"task_id":identity,"status":"pending","jev_evaluation_id":evaluation}


def complete_task(conn: Any, task_id: int, owner_token: str, result_json: dict) -> dict:
    """Metadata completion requires durable Jev, the current ficha and own assessment."""
    result = _object(result_json)
    evaluation, assessment = _positive(result.get("jev_evaluation_id")), _positive(result.get("participation_assessment_id"))
    detail_hash = _sha(result.get("detail_hash"))
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        run, task = _owned(cursor, task_id, owner_token)
        if task["kind"] != "enrich":
            raise BatchError("Discovery completion is established only by persisted pages")
        config, convocation = run["frozen_manifest"], task["id_convocatoria"]
        _bound_jev(cursor,run,convocation,evaluation)
        detail = _one(cursor, "SELECT detail_hash,document_inventory_complete FROM prod2_details WHERE id_convocatoria=%s FOR UPDATE", (convocation,))
        if not detail or detail["detail_hash"] != detail_hash:
            raise BatchError("Metadata completion requires the current official ficha")
        observed = _one(cursor, "SELECT * FROM prod2_participation_assessments WHERE id=%s FOR UPDATE", (assessment,))
        if not observed or (observed["id_convocatoria"],observed["detail_hash"],observed["is_current"],observed["policy_version"]) != (convocation,detail_hash,True,config["participation_policy_version"]):
            raise BatchError("Participation assessment does not bind the current ficha")
        cursor.execute("""UPDATE prod2_batch_tasks SET status='complete',result_json=%s::jsonb,
            completed_at=now(),updated_at=now() WHERE id=%s""", (_json(result), task_id))
    return {"task_id": task_id, "status": "complete"}


def advance_phase(conn: Any, run_id: str) -> dict:
    run_id = _identifier(run_id)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        run = _one(cursor, "SELECT * FROM prod2_batch_runs WHERE run_id=%s FOR UPDATE", (run_id,))
        if not run:
            raise BatchError("Unknown run")
        if run["status"] != "active":
            return {"run_id": run_id, "phase": run["phase"], "status": run["status"], "coverage_status": run["coverage_status"]}
        kind = "discovery" if run["phase"] == "discovery" else "enrich"
        counts = _one(cursor, """SELECT count(*) AS total,
            count(*) FILTER(WHERE status='blocked') AS blocked,
            count(*) FILTER(WHERE status NOT IN ('complete','split')) AS remaining
            FROM prod2_batch_tasks WHERE run_id=%s AND kind=%s""", (run_id, kind))
        if counts["blocked"]:
            _block_run(cursor, run_id, "unfinished_partition")
            return {"run_id": run_id, "phase": run["phase"], "status": "blocked"}
        if counts["remaining"]:
            return {"run_id": run_id, "phase": run["phase"], "status": "active", "remaining_tasks": counts["remaining"]}
        if kind == "discovery":
            if counts["total"] == 0 and run["frozen_manifest"].get("preflight_skipped_query_count",0) != run["frozen_manifest"].get("planned_query_count"):
                raise BatchError("Discovery cannot be complete without configured partitions")
            cursor.execute("SELECT id_convocatoria FROM prod2_batch_memberships WHERE run_id=%s ORDER BY id_convocatoria", (run_id,))
            members = cursor.fetchall()
            cursor.executemany("""INSERT INTO prod2_batch_tasks(run_id,task_key,kind,id_convocatoria)
                VALUES (%s,%s,'enrich',%s) ON CONFLICT(run_id,task_key) DO NOTHING""",
                [(run_id, _hash({"kind": "enrich", "convocation": member["id_convocatoria"]}), member["id_convocatoria"]) for member in members])
            cursor.execute("""UPDATE prod2_batch_runs SET phase='enrichment',coverage_status='complete',
                discovery_completed_at=now(),updated_at=now() WHERE run_id=%s""", (run_id,))
            if members:
                return {"run_id": run_id, "phase": "enrichment", "status": "active", "coverage_status": "complete", "unique_candidates": len(members)}
        cursor.execute("UPDATE prod2_batch_runs SET phase='complete',status='complete',coverage_status='complete',completed_at=now(),updated_at=now() WHERE run_id=%s", (run_id,))
    return {"run_id": run_id, "phase": "complete", "status": "complete", "coverage_status": "complete"}


def report_batch(conn: Any, run_id: str) -> dict:
    """Read durable progress; complete describes only the frozen configured scope."""
    with conn.cursor(row_factory=dict_row) as cursor:
        run=_one(cursor,"SELECT * FROM prod2_batch_runs WHERE run_id=%s",(_identifier(run_id),))
        if not run:
            raise BatchError("Unknown run")
        cursor.execute("SELECT kind,status,count(*) AS tasks FROM prod2_batch_tasks WHERE run_id=%s GROUP BY kind,status ORDER BY kind,status",(run_id,))
        tasks=cursor.fetchall()
        members=_one(cursor,"SELECT count(*) AS unique_candidates FROM prod2_batch_memberships WHERE run_id=%s",(run_id,))
    return {**run,"tasks":tasks,"unique_candidates":members["unique_candidates"],"coverage_basis":"verified_bounded_passes_and_frozen_zero_result_proofs"}


def discovery_locator(conn: Any, run_id: str, convocation_id: int) -> dict:
    """Locate observed query/page provenance; native identity must be reverified.

    Includes observations from old partial passes retained after snapshot restart.
    A row position is a hint, never permission to open an unverified current row.
    """
    with conn.cursor(row_factory=dict_row) as cursor:
        member=_one(cursor,"SELECT row_manifest FROM prod2_batch_memberships WHERE run_id=%s AND id_convocatoria=%s",
            (_identifier(run_id),_positive(convocation_id)))
        if not member:
            raise BatchError("Candidate is not part of this configured batch")
        locator=_one(cursor,"""SELECT q.query_manifest,p.page_number,pr.row_number,p.observed_at
            FROM prod2_discovery_page_rows pr JOIN prod2_discovery_pages p USING(run_id,page_number)
            JOIN prod2_discovery_runs r ON r.run_id=pr.run_id JOIN prod2_discovery_queries q ON q.query_hash=r.query_hash
            JOIN prod2_batch_task_passes tp ON tp.discovery_run_id=pr.run_id
            JOIN prod2_batch_tasks t ON t.id=tp.task_id WHERE t.run_id=%s AND pr.id_convocatoria=%s
            ORDER BY p.observed_at DESC,pr.run_id LIMIT 1""",(run_id,convocation_id))
        if not locator:
            raise BatchError("Candidate has no durable native listing provenance")
    return {**locator,"row_manifest":member["row_manifest"]}


def save_participation_assessment(conn: Any, convocation_id: int, *, detail_hash: str,
                                   assessment: dict, valid_until: datetime | None = None) -> dict:
    convocation, detail_hash, data = _positive(convocation_id), _sha(detail_hash), _object(assessment)
    as_of = _time(data.get("as_of"))
    if as_of > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise BatchError("Future participation assessment")
    until = _time(valid_until) if valid_until is not None else None
    if until is not None and until <= as_of:
        raise BatchError("Assessment validity must end after its observation")
    state, policy = data.get("state"), data.get("policy_version")
    if state not in PARTICIPATION_STATES or not isinstance(policy, str) or not policy.strip() or len(policy)>200 or data.get("eligibility_verified") is not False:
        raise BatchError("Invalid temporal assessment or provider eligibility assertion")
    snapshot = data.get("snapshot")
    if (not isinstance(snapshot,dict) or _sha(data.get("snapshot_sha256"))!=_hash(snapshot)
            or _sha(data.get("assessment_sha256"))!=_hash({k:v for k,v in data.items() if k!='assessment_sha256'})):
        raise BatchError("Temporal assessment source/result fingerprint mismatch")
    key = _hash({"convocation":convocation,"detail_hash":detail_hash,"as_of":as_of.isoformat(),
                 "valid_until":until.isoformat() if until else None,"assessment":data})
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        detail = _one(cursor, "SELECT * FROM prod2_details WHERE id_convocatoria=%s FOR UPDATE", (convocation,))
        if not detail or detail["detail_hash"] != detail_hash or detail["detail_fetched_at"] > as_of:
            raise BatchError("Assessment must follow and bind the current official ficha")
        raw=detail["raw_detail"]
        if snapshot.get("convocatoria_id")!=convocation or snapshot.get("procedure_id")!=raw["procedure_id"]:
            raise BatchError("Temporal assessment belongs to another official identity")
        if set(snapshot)!={"procedure_id","convocatoria_id","selection_type","item_states","schedule"}:
            raise BatchError("Invalid official temporal source snapshot")
        expected={key:raw.get(key) for key in snapshot}
        given_rows=snapshot.get("schedule")
        raw_rows=raw.get("schedule")
        if not isinstance(given_rows,list) or not isinstance(raw_rows,list) or len(given_rows)!=len(raw_rows):
            raise BatchError("Temporal schedule does not match the current official ficha")
        expected["schedule"]=[{key:source.get(key) for key in given} if isinstance(given,dict) else None
            for given,source in zip(given_rows,raw_rows)]
        if _hash(expected)!=_hash(snapshot):
            raise BatchError("Temporal assessment schedule/states differ from the current official ficha")
        prior = _one(cursor, "SELECT id,is_current FROM prod2_participation_assessments WHERE assessment_key=%s", (key,))
        if prior:
            return {"id":int(prior["id"]),"cached":True,"is_current":prior["is_current"]}
        current = _one(cursor, "SELECT as_of FROM prod2_participation_assessments WHERE id_convocatoria=%s AND is_current", (convocation,))
        if current and current["as_of"] >= as_of:
            raise BatchError("Stale or contradictory participation assessment")
        cursor.execute("UPDATE prod2_participation_assessments SET is_current=false WHERE id_convocatoria=%s AND is_current", (convocation,))
        saved = _one(cursor, """INSERT INTO prod2_participation_assessments(assessment_key,id_convocatoria,
            detail_hash,as_of,valid_until,policy_version,state,evidence_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id""",
            (key,convocation,detail_hash,as_of,until,policy,state,_json(data)))
    return {"id":int(saved["id"]),"cached":False,"is_current":True}
