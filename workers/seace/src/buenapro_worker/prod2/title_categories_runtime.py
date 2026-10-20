"""Bounded title backfill with PostgreSQL reservations and immutable paid receipts.

Every send has a committed reservation and committed send marker. A lost HTTP
reply, rejected request, malformed response, or failed durable write stops new
dispatch. No expired lease permits a second send. Exact saved replies may be
reconciled locally; uncertain attempts are never retried automatically.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
import time
import uuid

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .title_categories import (INPUT_PRICE, MODEL, POLICY, RESERVED_INPUT_TOKENS,
                               digest, encode, parse_response, sha, validate_manifest)

LOCK_KEY = 7820659504601
ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class StopDispatch(RuntimeError):
    """A durable reservation cannot safely authorize a new send."""


def immutable_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    data = encode(value)
    if path.exists():
        if path.read_bytes() != data:
            raise StopDispatch("Immutable private receipt conflicts with existing bytes")
        return
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path: Path):
    return json.loads(path.read_bytes())


def connect(dsn):
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def prepare_schema(conn, migration: Path):
    """Only dedicated 0046, registered in the normal migration ledger atomically."""
    if migration.name != "0046_prod2_title_categories.sql":
        raise ValueError("Only the dedicated title-category migration may be applied")
    tables = ("prod2_title_category_runs", "prod2_title_category_receipts", "prod2_title_classifications")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        applied = conn.execute("SELECT version FROM schema_migrations WHERE version=%s", (migration.name,)).fetchone()
        present = [conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"] for table in tables]
        if applied:
            if not all(present):
                raise StopDispatch("Registered title schema is incomplete; do not recreate blindly")
            # Select the complete public read contract to catch incompatible old installations.
            conn.execute("SELECT c.convocation_id,c.source_title,c.source_title_sha256,c.request_hash,c.categories,"
                         "c.decision,c.policy_version,r.model,r.status,r.request_json,r.response_json,r.result_json "
                         "FROM prod2_title_classifications c JOIN prod2_title_category_receipts r USING(request_hash) LIMIT 0")
        else:
            if any(present):
                raise StopDispatch("Unregistered existing title tables require integrity review")
            conn.execute(migration.read_text())
            conn.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (migration.name,))
    return {"schema_ready": True, "schema_applied": not bool(applied), "migration": migration.name,
            "migration_sha256": sha(migration.read_bytes())}


def register_run(conn, manifest, manifest_sha):
    validate_manifest(manifest, manifest_sha)
    with conn.transaction():
        conn.execute("INSERT INTO prod2_title_category_runs(run_id,manifest_sha256,manifest_json) "
                     "VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                     (manifest["run_id"], manifest_sha, Jsonb(manifest)))
        existing = conn.execute("SELECT manifest_sha256,manifest_json FROM prod2_title_category_runs "
                                "WHERE run_id=%s FOR UPDATE", (manifest["run_id"],)).fetchone()
        if existing["manifest_sha256"] != manifest_sha or existing["manifest_json"] != manifest:
            raise StopDispatch("Durable run belongs to a different frozen manifest")


def get_receipt(conn, key):
    return conn.execute("SELECT * FROM prod2_title_category_receipts WHERE request_hash=%s", (key,)).fetchone()


def validate_receipt(row, request):
    if (row["request_hash"] != request["request_hash"] or row["request_json"] != request["payload"]
            or row["model"] != MODEL or row["policy_version"] != POLICY
            or row["title_sha256"] != request["title_sha256"]
            or row["prompt_sha256"] != request["prompt_sha256"]
            or row["catalog_sha256"] != request["catalog_sha256"]):
        raise StopDispatch("Durable cache does not bind the exact frozen request")
    if row["status"] == "done":
        result = parse_response(request["payload"], row["response_json"])
        if result != row["result_json"]:
            raise StopDispatch("Durable parsed result differs from the paid raw response")


def reserve(conn, request, run_id):
    token = str(uuid.uuid4())
    with conn.transaction():
        claimed = conn.execute("""INSERT INTO prod2_title_category_receipts
            (request_hash,normalized_title,title_sha256,model,policy_version,catalog_sha256,
             prompt_sha256,request_json,status,claim_token,claimed_run_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s)
            ON CONFLICT DO NOTHING RETURNING request_hash""",
            (request["request_hash"], request["payload"]["state"]["titulo"], request["title_sha256"],
             MODEL, POLICY, request["catalog_sha256"], request["prompt_sha256"], Jsonb(request["payload"]),
             token, run_id)).fetchone()
    row = get_receipt(conn, request["request_hash"])
    validate_receipt(row, request)
    if not claimed:
        if row["status"] == "done":
            return None
        raise StopDispatch("An existing reserved/running/review/uncertain request cannot be resent")
    return token


def mark_sent(conn, key, token):
    with conn.transaction():
        changed = conn.execute("UPDATE prod2_title_category_receipts SET status='running',request_sent=true "
            "WHERE request_hash=%s AND claim_token=%s AND status='reserved' AND NOT request_sent RETURNING request_hash",
            (key, token)).fetchone()
        if not changed:
            raise StopDispatch("Reservation ownership or send marker changed")


def complete(conn, request, token, envelope):
    if (envelope.get("request_hash") != request["request_hash"] or envelope.get("claim_token") != token
            or envelope.get("request_sha256") != digest(request["payload"])):
        raise StopDispatch("Saved reply does not bind the claimed exact request")
    body, result = None, None
    status, cost, usage_in, usage_out = "uncertain", None, None, None
    if envelope.get("http_status") == 200:
        try:
            body = json.loads(envelope["body"])
            usage = body.get("usage", {})
            if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens")):
                usage_in, usage_out = usage["input_tokens"], usage["output_tokens"]
                cost = str(Decimal(usage_in) * INPUT_PRICE)
            result = parse_response(request["payload"], body)
            status = "done"
        except (ValueError, TypeError, KeyError, AttributeError):
            status = "needs_review" if cost is not None else "uncertain"
    elif envelope.get("body") is not None:
        try:
            body = json.loads(envelope["body"])
        except (ValueError, TypeError):
            body = {"unparsed_body_sha256": sha(str(envelope["body"]).encode())}
    with conn.transaction():
        row = conn.execute("SELECT * FROM prod2_title_category_receipts WHERE request_hash=%s FOR UPDATE",
                           (request["request_hash"],)).fetchone()
        validate_receipt(row, request)
        if str(row["claim_token"]) != token or not row["request_sent"]:
            raise StopDispatch("Paid reply cannot be completed without committed original ownership/send")
        if row["status"] == "done":
            if row["result_json"] != result or row["response_json"] != body:
                raise StopDispatch("Existing completed reply conflicts")
            return result
        if row["status"] not in {"running", "uncertain", "needs_review"}:
            raise StopDispatch("Unexpected durable receipt status")
        conn.execute("""UPDATE prod2_title_category_receipts SET status=%s,response_json=%s,result_json=%s,
            input_tokens=%s,output_tokens=%s,cost_usd=%s,completed_at=now()
            WHERE request_hash=%s AND claim_token=%s""",
            (status, Jsonb(body) if body is not None else None, Jsonb(result) if result is not None else None,
             usage_in, usage_out, cost, request["request_hash"], token))
    if status != "done":
        raise StopDispatch("Paid provider response requires review; no automatic resend")
    return result


def reconcile(conn, manifest, folder):
    reconciled, blocked = 0, []
    for request in manifest["requests"]:
        row = get_receipt(conn, request["request_hash"])
        if row is None:
            continue
        validate_receipt(row, request)
        if row["status"] == "done":
            continue
        path = folder / "responses" / f"{request['request_hash']}.private.json"
        if not path.exists():
            blocked.append({"request_hash": request["request_hash"], "status": row["status"]})
            continue
        try:
            complete(conn, request, str(row["claim_token"]), read_json(path))
            reconciled += 1
        except StopDispatch:
            blocked.append({"request_hash": request["request_hash"], "status": get_receipt(conn, request["request_hash"])["status"]})
    return {"reconciled": reconciled, "blocked": blocked}


def apply_results(conn, manifest):
    """Exact title CAS under source row lock; never replace extraction/matching."""
    counts = {"applied": 0, "unchanged": 0, "stale": 0, "pending": 0}
    requests = {request["request_hash"]: request for request in manifest["requests"]}
    for case in manifest["cases"]:
        request = requests[case["request_hash"]]
        row = get_receipt(conn, case["request_hash"])
        if row is None or row["status"] != "done":
            counts["pending"] += 1
            continue
        validate_receipt(row, request)
        with conn.transaction():
            current = conn.execute("SELECT description FROM prod2_discoveries WHERE id_convocatoria=%s FOR UPDATE",
                                   (case["convocation_id"],)).fetchone()
            if current is None or current["description"] != case["source_title"]:
                counts["stale"] += 1
                continue
            old = conn.execute("SELECT request_hash,source_title,source_title_sha256,categories,decision,policy_version "
                "FROM prod2_title_classifications WHERE convocation_id=%s", (case["convocation_id"],)).fetchone()
            values = {"request_hash": case["request_hash"], "source_title": case["source_title"],
                      "source_title_sha256": case["source_title_sha256"], "categories": row["result_json"]["categories"],
                      "decision": row["result_json"]["decision"], "policy_version": POLICY}
            if old == values:
                counts["unchanged"] += 1
                continue
            conn.execute("""INSERT INTO prod2_title_classifications
                (convocation_id,source_title,source_title_sha256,request_hash,categories,decision,policy_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(convocation_id) DO UPDATE SET
                source_title=excluded.source_title,source_title_sha256=excluded.source_title_sha256,
                request_hash=excluded.request_hash,categories=excluded.categories,decision=excluded.decision,
                policy_version=excluded.policy_version,updated_at=now()""",
                (case["convocation_id"], case["source_title"], case["source_title_sha256"], case["request_hash"],
                 values["categories"], values["decision"], POLICY))
            counts["applied"] += 1
    return counts


def execute(dsn, manifest, folder, expected_sha, api_key, *, limit=20, concurrency=2,
            requests_per_second=1, client_factory=None):
    if type(limit) is not int or not 1 <= limit <= len(manifest["requests"]):
        raise ValueError("Finite positive dispatch limit must not exceed manifest requests")
    if type(concurrency) is not int or not 1 <= concurrency <= 5 or not 0 < requests_per_second <= 2:
        raise ValueError("Concurrency must be 1..5 and global rate at most 2 requests/second")
    if not api_key:
        raise ValueError("Existing JEV credential is required")
    folder = Path(folder)
    stop = threading.Event()
    sent, cached, outcomes = 0, 0, []
    known = Decimal("0")
    budget = Decimal(manifest["budget_usd"])
    reservation = Decimal(RESERVED_INPUT_TOKENS) * INPUT_PRICE
    report = {"status": "running", "run_id": manifest["run_id"], "manifest_sha256": expected_sha,
              "limit": limit, "concurrency": concurrency, "budget_usd": str(budget),
              "budget_is_invoice_guarantee": False, "reserved_tokens_per_inflight": RESERVED_INPUT_TOKENS,
              "no_automatic_retry": True}

    def work(request, token):
        key = request["request_hash"]
        owner = {"request_hash": key, "claim_token": token, "manifest_sha256": expected_sha,
                 "request_sha256": digest(request["payload"])}
        immutable_write(folder / "claims" / f"{key}.private.json", owner)
        with connect(dsn) as conn:
            if stop.is_set():
                return {"request_hash": key, "status": "reserved_unsent_after_stop"}
            mark_sent(conn, key, token)
            envelope = {**owner, "received_at": None, "http_status": None, "body": None}
            client = client_factory() if client_factory else httpx.Client(timeout=90)
            try:
                response = client.post(ENDPOINT, headers={"Authorization": f"Bearer {api_key}"},
                                       json=request["payload"])
                envelope.update(http_status=response.status_code, body=response.text,
                                received_at=datetime.now(timezone.utc).isoformat())
                if response.status_code != 200:
                    stop.set()
            except Exception as error:
                stop.set()
                # Sanitized class only; raw exception strings can expose request headers.
                envelope["transport_error_type"] = type(error).__name__
            finally:
                try:
                    client.close()
                except Exception:
                    # A close failure must not discard a response already received.
                    stop.set()
            immutable_write(folder / "responses" / f"{key}.private.json", envelope)
            try:
                result = complete(conn, request, token, envelope)
                if result["input_tokens"] > RESERVED_INPUT_TOKENS:
                    stop.set()
                return {"request_hash": key, "status": "done", "decision": result["decision"],
                        "cost_usd": result["cost_usd"]}
            except StopDispatch:
                stop.set()
                row = get_receipt(conn, key)
                return {"request_hash": key, "status": row["status"],
                        **({"cost_usd": str(row["cost_usd"])} if row["cost_usd"] is not None else {})}

    with connect(dsn) as conn:
        lock = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_KEY,)).fetchone()
        if not lock["locked"]:
            raise StopDispatch("Another title-category coordinator owns the global PG lock")
        try:
            register_run(conn, manifest, expected_sha)
            recovery = reconcile(conn, manifest, folder)
            if recovery["blocked"]:
                raise StopDispatch("Existing incomplete paid/reserved attempts need explicit local reconciliation")
            pending = []
            for request in manifest["requests"]:
                row = get_receipt(conn, request["request_hash"])
                if row is not None:
                    validate_receipt(row, request)
                    if row["status"] != "done":
                        raise StopDispatch("Existing durable attempt prevents automatic resend")
                    cached += 1
                    # Conservative: count all reused receipts in the frozen manifest budget.
                    known += Decimal(str(row["cost_usd"]))
                else:
                    pending.append(request)
            futures, next_start = {}, 0.0
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                index = 0
                while futures or (index < len(pending) and sent < limit and not stop.is_set()):
                    while index < len(pending) and sent < limit and len(futures) < concurrency and not stop.is_set():
                        if known + reservation * (len(futures) + 1) > budget:
                            report["budget_guard_stopped"] = True
                            stop.set()
                            break
                        remaining = next_start - time.monotonic()
                        if remaining > 0:
                            time.sleep(remaining)
                        if stop.is_set():
                            break
                        request = pending[index]
                        token = reserve(conn, request, manifest["run_id"])
                        if token is None:
                            cached += 1
                            index += 1
                            continue
                        futures[pool.submit(work, request, token)] = request
                        sent += 1
                        index += 1
                        next_start = time.monotonic() + 1 / requests_per_second
                    if not futures:
                        break
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        request = futures.pop(future)
                        try:
                            outcome = future.result()
                        except Exception as error:
                            stop.set()
                            outcome = {"request_hash": request["request_hash"], "status": "local_stop",
                                       "error_type": type(error).__name__}
                        outcomes.append(outcome)
                        known += Decimal(outcome.get("cost_usd", "0"))
                # Context manager drains already dispatched replies before returning.
            report.update(status="stopped" if stop.is_set() else "complete" if not pending or index == len(pending) else "pilot_complete",
                          dispatched=sent, cached=cached, known_cost_usd=str(known), outcomes=outcomes,
                          remaining_unclaimed=len(pending) - index)
            # PG is the final accounting authority, including malformed but known-usage replies.
            rows = conn.execute("SELECT status,request_sent,input_tokens,output_tokens,cost_usd "
                "FROM prod2_title_category_receipts WHERE request_hash=ANY(%s)",
                ([r["request_hash"] for r in manifest["requests"]],)).fetchall()
            statuses = {}
            for row in rows:
                statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            report.update(receipt_statuses=statuses,
                known_cost_usd=str(sum((row["cost_usd"] for row in rows if row["cost_usd"] is not None), Decimal("0"))),
                input_tokens=sum(row["input_tokens"] or 0 for row in rows),
                output_tokens=sum(row["output_tokens"] or 0 for row in rows),
                uncertain_attempts=statuses.get("uncertain", 0),
                unknown_cost_attempts=sum(row["request_sent"] and row["cost_usd"] is None for row in rows))
            immutable_write(folder / "reports" / f"execute-{time.time_ns()}.private.json", report)
            return report
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
