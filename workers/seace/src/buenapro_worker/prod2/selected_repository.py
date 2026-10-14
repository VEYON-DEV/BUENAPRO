"""Reserve selector payments durably; no providers or source HTTP here."""
from __future__ import annotations

import math
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row

from buenapro_worker.prod2.repository import _id, _sha, _uuid, _text
from buenapro_worker.prod2.discovery import _json
from buenapro_worker.prod2.document_selection import CACHE_PREFIX


class SelectionPersistenceError(ValueError):
    pass


def prepare_schema(conn, migration: Path):
    """Apply ONLY migration0039, transactionally; never replay unrelated legacy DDL."""
    if migration.name != "0039_prod2_document_selection_cache.sql":
        raise SelectionPersistenceError("Expected the dedicated selector migration")
    expected = {"request_hash", "convocation_id", "model", "policy_version", "status", "claim_token",
                "claimed_at", "lease_until", "completed_at", "result_json", "blocked_reason"}
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_document_selection_schema'),0)")
        conn.execute(migration.read_text(encoding="utf-8"))
        columns = conn.execute("""SELECT attname FROM pg_attribute WHERE attrelid=
            'prod2_document_selection_cache'::regclass AND attnum>0 AND NOT attisdropped""").fetchall()
        if {row["attname"] for row in columns} != expected:
            raise SelectionPersistenceError("Existing selector table has incompatible columns")
        constraints = conn.execute("""SELECT contype,pg_get_constraintdef(oid) AS definition
            FROM pg_constraint WHERE conrelid='prod2_document_selection_cache'::regclass""").fetchall()
        if (not any(row["contype"] == "p" and row["definition"] == "PRIMARY KEY (request_hash)" for row in constraints)
                or not any(row["contype"] == "f" and "REFERENCES prod2_discoveries(id_convocatoria)" in row["definition"] for row in constraints)
                or not any(row["contype"] == "c" and all(value in row["definition"] for value in ("running", "done", "uncertain")) for row in constraints)):
            raise SelectionPersistenceError("Existing selector ownership constraints are incompatible")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT(version) DO NOTHING", (migration.name,))
    return {"status": "selector_schema_ready", "migration": migration.name}


def _result(value, row):
    if not isinstance(value, dict) or any(value.get(key) != row[key] for key in
            ("request_hash", "model", "policy_version")):
        raise SelectionPersistenceError("Selector receipt differs from its reservation")
    if value.get("cache_key") != CACHE_PREFIX + row["request_hash"] or not isinstance(value.get("raw_response"), dict):
        raise SelectionPersistenceError("Selector requires its exact original response")
    for key in ("input_tokens", "output_tokens"):
        if type(value.get(key)) is not int or value[key] < 0:
            raise SelectionPersistenceError("Invalid selector usage")
    cost = value.get("cost_usd")
    if type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost < 10000:
        raise SelectionPersistenceError("Invalid selector cost")
    return value


def claim_selection(conn, *, convocation_id, request_hash, model, policy_version):
    conv, request_hash = _id(convocation_id), _sha(request_hash)
    model, policy_version = _text(model, limit=200), _text(policy_version, limit=200)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_document_selection_cache'),hashtext(%s))", (request_hash,))
        cur.execute("SELECT *,lease_until<=clock_timestamp() AS expired FROM prod2_document_selection_cache WHERE request_hash=%s FOR UPDATE", (request_hash,))
        row = cur.fetchone()
        if row:
            if (row["model"], row["policy_version"]) != (model, policy_version):
                raise SelectionPersistenceError("Selector reservation model/policy changed")
            if row["status"] == "done":
                return {"state": "cached", "result": _result(row["result_json"], row)}
            if row["status"] == "running" and row["expired"]:
                cur.execute("UPDATE prod2_document_selection_cache SET status='uncertain',blocked_reason='expired_provider_claim' WHERE request_hash=%s", (request_hash,))
            return {"state": "blocked", "reason": "provider_running" if row["status"] == "running" and not row["expired"] else "provider_uncertain"}
        token = str(uuid4())
        cur.execute("""INSERT INTO prod2_document_selection_cache(request_hash,convocation_id,model,policy_version,
            status,claim_token,lease_until) VALUES (%s,%s,%s,%s,'running',%s,clock_timestamp()+interval '10 minutes')""",
            (request_hash, conv, model, policy_version, token))
    return {"state": "claimed", "claim_token": token}


def complete_selection(conn, *, request_hash, claim_token, result):
    request_hash, token = _sha(request_hash), _uuid(claim_token)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM prod2_document_selection_cache WHERE request_hash=%s FOR UPDATE", (request_hash,))
        row = cur.fetchone()
        if not row or str(row["claim_token"]) != token:
            raise SelectionPersistenceError("Selector receipt owner mismatch")
        normalized = _result(result, row)
        if row["status"] == "done":
            if row["result_json"] != normalized:
                raise SelectionPersistenceError("Completed selector receipt is immutable")
            return normalized
        if row["status"] not in {"running", "uncertain"}:
            raise SelectionPersistenceError("Unexpected selector reservation status")
        cur.execute("""UPDATE prod2_document_selection_cache SET status='done',result_json=%s::jsonb,
            completed_at=clock_timestamp(),blocked_reason=NULL WHERE request_hash=%s""", (_json(normalized), request_hash))
    return normalized


def uncertain_selection(conn, *, request_hash, claim_token):
    conn.execute("""UPDATE prod2_document_selection_cache SET status='uncertain',blocked_reason='provider_outcome_unknown'
        WHERE request_hash=%s AND claim_token=%s AND status='running'""", (_sha(request_hash), _uuid(claim_token)))
