"""Durable source-query progress, independently of accepted candidates/models."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from .batch_plan import digest
from .discovery import validate_capture
from .keyword_sweep import SWEEP_VERSION, inspect_complete_cache
from .empty_scope_probe import inspect_zero_scope_cache
from .national_listing import POLICY as NATIONAL_LISTING_POLICY

POLICY = "prod2_keyword_tracking_v1"
CACHE_ERROR = "completed_query_cache_missing_or_invalid_restore_or_new_epoch"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", value):
        raise ValueError("Expected a bounded keyword sweep/run identifier")
    return value


def validated_query(query, *, allow_empty_keyword=False):
    now = datetime.now(timezone.utc)
    value = validate_capture({"query": query, "observed_at": now.isoformat(),
        "page": {"number": 1, "total_pages": 1, "total_results": 0, "page_size": 1}, "rows": []}, now=now,
        allow_empty_keyword=allow_empty_keyword).query
    if value["keyword"] != " ".join(value["keyword"].split()).casefold():
        raise ValueError("Tracking needs canonical configured keyword spelling")
    return value


def scope_for(manifest):
    # Exercise years/keywords may be extended while resuming the same epoch. The national listing is
    # part of the scope only when used, so a keyword epoch keeps its frozen scope and hash.
    return {"policy": POLICY, "sweep_policy": SWEEP_VERSION, "source": "seace_prod2", "seace_version": "3",
        "published_from": manifest["published_from"], "published_to": manifest["published_to"],
        "native_lifecycle_filter": None, "source_atomic_snapshot": False,
        **({"discovery_policy": manifest["discovery_policy"]} if "discovery_policy" in manifest else {})}


def prepare_schema(conn, migration: Path):
    if migration.name != "0040_prod2_keyword_tracking.sql":
        raise ValueError("Expected the dedicated keyword tracking migration")
    expected = {"prod2_keyword_sweeps": {"sweep_id", "scope_hash", "frozen_scope", "created_at"},
        "prod2_keyword_tasks": {"sweep_id", "query_hash", "query_manifest", "status", "owner_run_id",
            "claim_token", "attempts", "started_at", "updated_at", "last_completed_at", "result_json", "last_complete_json"}}
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_keyword_schema'),0)")
        conn.execute(migration.read_text(encoding="utf-8"))
        for table, names in expected.items():
            rows = conn.execute("SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(%s) AND attnum>0 AND NOT attisdropped", (table,)).fetchall()
            if {row["attname"] for row in rows} != names:
                raise ValueError("Existing keyword tracking table has incompatible columns")
        constraints = conn.execute("SELECT contype,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid='prod2_keyword_tasks'::regclass").fetchall()
        if (not any(row["contype"] == "p" and row["definition"] == "PRIMARY KEY (sweep_id, query_hash)" for row in constraints)
                or not any(row["contype"] == "f" and "REFERENCES prod2_keyword_sweeps(sweep_id)" in row["definition"] for row in constraints)
                or not any(row["contype"] == "c" and all(state in row["definition"] for state in ("pending", "running", "partial", "blocked", "complete")) for row in constraints)):
            raise ValueError("Existing keyword tracking ownership constraints are incompatible")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT(version) DO NOTHING", (migration.name,))
    return {"status": "keyword_tracking_schema_ready", "migration": migration.name}


def seed_queries(conn, sweep_id, manifest, queries):
    sweep_id = identifier(sweep_id)
    scope = scope_for(manifest)
    national = manifest.get("discovery_policy") == NATIONAL_LISTING_POLICY
    normalized = [validated_query(query, allow_empty_keyword=national) for query in queries]
    if national:
        # Each listing query is one publication day; it must lie inside the epoch's window.
        outside = any(query["seace_version"] != scope["seace_version"] or not (
            scope["published_from"] <= query["published_from"] <= query["published_to"] <= scope["published_to"])
            for query in normalized)
    else:
        outside = any(any(query[key] != scope[key] for key in ("published_from", "published_to", "seace_version"))
                      for query in normalized)
    if outside:
        raise ValueError("Tracked query differs from its frozen epoch")
    with conn.transaction():
        conn.execute("INSERT INTO prod2_keyword_sweeps(sweep_id,scope_hash,frozen_scope) VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
            (sweep_id, digest(scope), json.dumps(scope)))
        row = conn.execute("SELECT * FROM prod2_keyword_sweeps WHERE sweep_id=%s FOR UPDATE", (sweep_id,)).fetchone()
        if row["scope_hash"] != digest(scope) or row["frozen_scope"] != scope:
            raise ValueError("Keyword sweep dates/policy changed; use a new sweep ID")
        # psycopg batches these statements through its pipeline. One round trip
        # per query made even an unchanged 1,380-query resume unnecessarily slow.
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO prod2_keyword_tasks(sweep_id,query_hash,query_manifest) VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                [(sweep_id, digest(query), json.dumps(query, ensure_ascii=False)) for query in normalized])


def summarize_result(query, result):
    if (result.get("query") != query or type(result.get("coverage_complete")) is not bool
            or result.get("source_atomic_snapshot") is not False or not isinstance(result.get("errors"), list)
            or result.get("status") not in {"complete", "partial", "stopped", "blocked"}
            or result["coverage_complete"] != (result["status"] == "complete")
            or (result["coverage_complete"] and result["errors"])):
        raise ValueError("Query progress requires a bound native coverage result")
    times = [row["observed_at"] for row in result.get("leaf_windows", []) if row.get("observed_at")]
    observed = result.get("source_observed_at") or (max(times) if times else None)
    counts = {key: result.get(key, 0) for key in ("pages_processed", "cached_pages_reemitted", "rows_offered",
        "emit_returned_true", "emit_returned_false", "http_searches", "http_pages", "windows_visited")}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("Invalid query progress counters")
    bindings = {key: result[key] for key in ("scope_proof_hash", "cache_evidence_hash") if key in result}
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in bindings.values()):
        raise ValueError("Invalid completed cache evidence hash")
    return {"query": query, "coverage_complete": result["coverage_complete"], "coverage_kind": result.get("coverage_kind"),
        "source_atomic_snapshot": False, "source_observed_at": observed, "report_hash": digest(result),
        "status": "partial" if result["status"] == "stopped" else result["status"], **counts,
        **bindings,
        "replayed_observation": counts["cached_pages_reemitted"] > 0 and counts["http_searches"] == 0,
        "error_codes": [row["code"] for row in result["errors"]]}


def _completed_evidence(query, summary, epoch_folder, *, allow_empty_keyword=False):
    """Fail closed when a completed observation cannot be replayed exactly."""
    if (summary.get("query") != query or summary.get("coverage_complete") is not True
            or summary.get("status") != "complete" or summary.get("source_atomic_snapshot") is not False):
        raise ValueError("Completed query summary is not bound")
    if summary.get("coverage_kind") == "observed_pass":
        evidence = inspect_complete_cache(query, Path(epoch_folder) / digest(query),
                                          allow_empty_keyword=allow_empty_keyword)
        if summary.get("cache_evidence_hash") and summary["cache_evidence_hash"] != evidence["cache_evidence_hash"]:
            raise ValueError("Completed query cache evidence changed")
    elif summary.get("coverage_kind") == "verified_empty_scope_observed":
        evidence = inspect_zero_scope_cache(query, Path(epoch_folder) / "scope-probes", summary.get("scope_proof_hash"))
    else:
        raise ValueError("Unsupported completed query evidence")
    if summary.get("source_observed_at") != evidence["source_observed_at"]:
        raise ValueError("Completed query source observation changed")
    return evidence


def _cache_error(query, summary, epoch_folder, *, allow_empty_keyword=False):
    if not summary:
        return None
    try:
        _completed_evidence(query, summary, epoch_folder, allow_empty_keyword=allow_empty_keyword)
    except Exception:
        return {"query": query, "status": "blocked", "coverage_complete": False,
            "source_atomic_snapshot": False, "errors": [{"code": CACHE_ERROR}]}
    return None


def completed_cache_errors(conn, sweep_id, cache_folder, queries=None, *, allow_empty_keyword=False):
    """Read-only guard for callers before bootstrap/probes or any source I/O.

    ``queries`` optionally restricts the guard to planned query identities.
    Completed evidence is required even after a later partial/blocked replay.
    Returning errors never recreates evidence and never changes PostgreSQL rows.
    """
    sweep_id = identifier(sweep_id)
    keys = None if queries is None else {
        digest(validated_query(query, allow_empty_keyword=allow_empty_keyword)) for query in queries}
    folder = Path(cache_folder).resolve() / sweep_id
    rows = conn.execute("SELECT query_hash,query_manifest,last_complete_json FROM prod2_keyword_tasks WHERE sweep_id=%s AND last_complete_json IS NOT NULL ORDER BY query_hash",
        (sweep_id,)).fetchall()
    errors = []
    for row in rows:
        if keys is not None and row["query_hash"] not in keys:
            continue
        error = _cache_error(row["query_manifest"], row["last_complete_json"], folder,
                             allow_empty_keyword=allow_empty_keyword)
        if error:
            errors.append(error)
    return errors


def progress_summary(rows, keywords):
    results = []
    for keyword in keywords:
        tasks = [row for row in rows if row["query_manifest"]["keyword"] == keyword]
        states = [row["status"] for row in tasks]
        state = ("complete" if states and all(value == "complete" for value in states) else
            "running" if "running" in states else "blocked" if "blocked" in states else
            "partial" if any(value != "pending" for value in states) else "pending")
        results.append({"keyword": keyword, "status": state, "queries_planned": len(tasks),
            "tracking_registered": bool(tasks),
            "queries_complete": states.count("complete"), "used": any(row["attempts"] > 0 for row in tasks),
            "latest_source_observed_at": max((row["result_json"].get("source_observed_at") for row in tasks
                if row.get("result_json") and row["result_json"].get("source_observed_at")), default=None),
            "queries": [{"year": row["query_manifest"]["year"], "object_type": row["query_manifest"]["object_type"],
                "published_from": row["query_manifest"]["published_from"], "published_to": row["query_manifest"]["published_to"],
                "status": row["status"], "attempts": row["attempts"], "progress": row["result_json"]} for row in tasks]})
    return results


def read_progress(conn, sweep_id, keywords):
    rows = conn.execute("SELECT query_manifest,status,attempts,result_json FROM prod2_keyword_tasks WHERE sweep_id=%s ORDER BY query_hash", (identifier(sweep_id),)).fetchall()
    return progress_summary(rows, keywords)


class KeywordTracker:
    """Reuse one connection per search thread; session lock survives source I/O.

    A crashed source worker releases its DB lock. A new owner can then resume the
    native cache; this mechanism never authorizes retrying a paid provider claim.
    """
    def __init__(self, connect, sweep_id, run_id, cache_folder, *, allow_empty_keyword=False):
        self.connect, self.sweep_id, self.run_id = connect, identifier(sweep_id), identifier(run_id)
        self.allow_empty_keyword = allow_empty_keyword
        self.folder = Path(cache_folder).resolve() / self.sweep_id
        self.local, self.connections, self.guard = threading.local(), [], threading.Lock()

    def close(self):
        for conn in self.connections:
            conn.close()

    def completed_cache_errors(self, queries=None):
        """Read-only pre-source guard for direct coordinator/sweep callers."""
        conn = self.connect()
        try:
            return completed_cache_errors(conn, self.sweep_id, self.folder.parent, queries,
                                          allow_empty_keyword=self.allow_empty_keyword)
        finally:
            conn.close()

    def run(self, query, execute):
        query = validated_query(query, allow_empty_keyword=self.allow_empty_keyword)
        key = digest(query)
        if not hasattr(self.local, "conn"):
            self.local.conn = self.connect()
            with self.guard:
                self.connections.append(self.local.conn)
        conn = self.local.conn
        identity = self.sweep_id + ":" + key
        acquired = conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_keyword_query'),hashtext(%s)) AS acquired", (identity,)).fetchone()["acquired"]
        if not acquired:
            return {"query": query, "status": "blocked", "coverage_complete": False, "source_atomic_snapshot": False,
                "errors": [{"code": "query_owned_by_another_worker"}]}
        token = str(uuid4())
        try:
            with conn.transaction():
                row = conn.execute("SELECT * FROM prod2_keyword_tasks WHERE sweep_id=%s AND query_hash=%s FOR UPDATE", (self.sweep_id, key)).fetchone()
                if not row or row["query_manifest"] != query:
                    raise ValueError("Keyword tracking query was not seeded or changed")
                cache_error = _cache_error(query, row.get("last_complete_json"), self.folder,
                                           allow_empty_keyword=self.allow_empty_keyword)
                conn.execute("UPDATE prod2_keyword_tasks SET status='running',owner_run_id=%s,claim_token=%s,attempts=attempts+1,started_at=clock_timestamp(),updated_at=clock_timestamp() WHERE sweep_id=%s AND query_hash=%s",
                    (self.run_id, token, self.sweep_id, key))
            if cache_error:
                result = cache_error
            else:
                try:
                    result = execute(self.folder / key)
                except Exception:
                    result = {"query": query, "status": "partial", "coverage_complete": False, "source_atomic_snapshot": False,
                        "errors": [{"code": "query_execution_failed"}]}
            if result.get("coverage_complete") is True:
                try:
                    if result.get("coverage_kind") == "observed_pass":
                        result = {**result, **inspect_complete_cache(query, self.folder / key,
                                                                     allow_empty_keyword=self.allow_empty_keyword)}
                    elif result.get("coverage_kind") == "verified_empty_scope_observed":
                        result = {**result, **inspect_zero_scope_cache(query, self.folder / "scope-probes", result.get("scope_proof_hash"))}
                    _completed_evidence(query, summarize_result(query, result), self.folder,
                                        allow_empty_keyword=self.allow_empty_keyword)
                except Exception:
                    result = {"query": query, "status": "blocked", "coverage_complete": False,
                        "source_atomic_snapshot": False, "errors": [{"code": CACHE_ERROR}]}
            summary = summarize_result(query, result)
            encoded = json.dumps(summary, ensure_ascii=False)
            with conn.transaction():
                changed = conn.execute("""UPDATE prod2_keyword_tasks SET status=%s,result_json=%s::jsonb,
                    updated_at=clock_timestamp(),last_completed_at=CASE WHEN %s THEN clock_timestamp() ELSE last_completed_at END,
                    last_complete_json=CASE WHEN %s THEN %s::jsonb ELSE last_complete_json END
                    WHERE sweep_id=%s AND query_hash=%s AND claim_token=%s""",
                    (summary["status"], encoded, summary["coverage_complete"], summary["coverage_complete"], encoded, self.sweep_id, key, token))
                if changed.rowcount != 1:
                    raise ValueError("Keyword progress owner changed")
            return result
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_keyword_query'),hashtext(%s))", (identity,))
