"""Finish only the frozen original PROD2 Redis candidates, without discovery.

The original queue and captures remain authoritative. A new immutable private
manifest binds the pending IDs and payloads before any source/provider request.
New enrichment uses preview selector V3, ficha context and total-page routing.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row
import redis

import run_prod2_selected as selected
from buenapro_worker.prod2.accepted_repository import _capture_row, _run_id
from buenapro_worker.prod2.analysis import LLM1_FICHA_CONTEXT_POLICY
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.document_selection import DOCUMENT_SELECTION_POLICY_V3
from buenapro_worker.prod2.model_routing import default_model_routing, resolve_llm1_model
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.selected_control import RunLimits, SharedPacer
from buenapro_worker.prod2.selected_payments import audit_summary
from buenapro_worker.prod2.staging import RedisStage, _CHECK_TYPES

POLICY = "prod2_frozen_pending_continuation_v1"
_SNAPSHOT = """-- prod2-pending-continuation-snapshot-v1
return cjson.encode({states=redis.call('HGETALL', KEYS[1]),
    payloads=redis.call('HGETALL', KEYS[2]), queue=redis.call('LRANGE', KEYS[3],0,-1),
    pending=redis.call('LRANGE', KEYS[4],0,-1)})
"""
_BOUND_FIELDS = {"pending_ids", "pending_payload_hashes", "pending_snapshot", "snapshot_hash",
                 "queue_before", "frozen_at"}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--original-folder", type=Path, required=True)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--expected-pending", type=int, default=90)
    p.add_argument("--max-jev-calls", type=int, default=180)
    p.add_argument("--max-llm1-calls", type=int, default=90)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--model-workers", type=int, default=2)
    p.add_argument("--request-interval", type=float, default=2)
    p.add_argument("--local", action="store_true")
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    p.add_argument("--dry-run", action="store_true", help="Offline; no DB, Redis, source or provider calls")
    return p


def safe_path(path):
    path = Path(path).absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Private continuation paths must not use symlinks")
    return path.resolve()


def original_proofs(original, old):
    proofs = []
    for path in sorted(original.glob("*/outcome.private.json")):
        value = read_private(path)
        conv = value.get("convocation_id")
        if (type(conv) is not int or conv <= 0 or path.parent.name != str(conv)
                or value.get("accepted") is not True):
            raise ValueError("Original outcomes do not bind the previously accepted members")
        proofs.append({"convocation_id": conv, "outcome_hash": hash_request(value)})
    if len(proofs) != old.get("max_accepted") or len({p["convocation_id"] for p in proofs}) != len(proofs):
        raise ValueError("Original accepted member evidence is incomplete")
    return proofs


def plan(args):
    if not (1 <= args.expected_pending <= 100000 and 1 <= args.max_jev_calls <= 200000
            and 1 <= args.max_llm1_calls <= 100000 and 1 <= args.workers <= 8
            and 1 <= args.model_workers <= 4 and 2 <= args.request_interval <= 60):
        raise ValueError("Continuation counts, workers and pacing must be bounded")
    original, folder = safe_path(args.original_folder), safe_path(args.folder)
    _run_id(folder.name)
    if folder == original or folder.is_relative_to(original) or original.is_relative_to(folder):
        raise ValueError("Use a separate continuation folder outside original captures")
    if not original.is_dir():
        raise ValueError("Original private capture folder is missing")
    marker = folder / "manifest.private.json"
    if folder.exists() and (not folder.is_dir() or (any(folder.iterdir()) and not marker.is_file())):
        raise ValueError("Continuation folder must be new or own a frozen manifest")
    old = read_private(original / "manifest.private.json")
    required = ("run_id", "gemini_model", "profile_id", "commercial_profiles", "published_from", "published_to",
                "resolved_years", "keywords", "objects", "cache_folder", "max_source_mib", "max_expanded_mib",
                "max_archive_members", "max_candidates", "max_accepted", "selector_high", "selector_low")
    if (old.get("pipeline") != "prod2_selected_llm1_v1" or old.get("terminal_stage") != "llm1"
            or old.get("jev_threshold") != 60 or any(k not in old for k in required)):
        raise ValueError("Original manifest lacks the frozen selected run context")
    _run_id(old["run_id"])
    if folder.name == old["run_id"]:
        raise ValueError("Continuation must own a new run identifier")
    start, end = date.fromisoformat(old["published_from"]), date.fromisoformat(old["published_to"])
    if (start > end or not isinstance(old["keywords"], list) or not old["keywords"]
            or any(not isinstance(k, str) or not k.strip() for k in old["keywords"])
            or not isinstance(old["resolved_years"], list) or not old["resolved_years"]
            or any(type(y) is not int or not 2000 <= y <= 2100 for y in old["resolved_years"])
            or not isinstance(old["objects"], list) or not old["objects"]
            or any(o not in {"good", "service"} for o in old["objects"])
            or not isinstance(old["commercial_profiles"], list) or not old["commercial_profiles"]
            or not (16 <= old["max_source_mib"] <= 128
                    and old["max_source_mib"] <= old["max_expanded_mib"] <= 1024)
            or not 1 <= old["max_archive_members"] <= 5000
            or not 0 <= old["selector_low"] < old["selector_high"] <= 100):
        raise ValueError("Original frozen source context or limits are invalid")
    prior = original_proofs(original, old)
    if old["max_candidates"] != args.expected_pending + len(prior):
        raise ValueError("Expected pending scope differs from the original candidate bound")
    manifest = {**old, "continuation_policy": POLICY, "run_id": folder.name,
        "original_run_id": old["run_id"], "original_manifest_hash": hash_request(old),
        "original_folder": str(original), "continuation_folder": str(folder),
        "original_staging_identity": selected.staging_identity(old), "previous_done": prior,
        "expected_pending": args.expected_pending, "expected_seen": old["max_candidates"],
        "execution_target": "local" if args.local else "production",
        "ssh_host": None if args.local else args.ssh_host,
        "no_discovery": True, "discovery_sweep_calls": 0, "new_candidates": 0,
        "document_previews": True, "selector_policy_version": DOCUMENT_SELECTION_POLICY_V3,
        "llm1_ficha_context_policy": LLM1_FICHA_CONTEXT_POLICY,
        "llm1_model_routing": default_model_routing(), "processing_workers": args.workers,
        "model_workers": args.model_workers, "request_interval": args.request_interval,
        "max_accepted": args.expected_pending, "max_candidates": args.expected_pending,
        "max_jev_calls": args.max_jev_calls, "max_llm1_calls": args.max_llm1_calls}
    # The old requested flags belong only to discovery, never this new run.
    manifest.pop("requested", None)
    resolve_llm1_model(manifest, 1, old["gemini_model"])
    if marker.exists():
        existing = read_private(marker)
        if {k: v for k, v in existing.items() if k not in _BOUND_FIELDS} != {
                k: v for k, v in manifest.items() if k not in _BOUND_FIELDS}:
            raise ValueError("Frozen continuation inputs changed; use identical flags")
        validate_frozen(existing, old)
    return manifest, old


def validate_payload(identity, payload, old):
    if not isinstance(identity, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", identity):
        raise ValueError("Pending identity is not a convocation ID")
    required = {"row", "query", "observed_at", "page_number"}
    if not isinstance(payload, dict) or not required.issubset(payload) or set(payload) - required - {"discovery_business_context"}:
        raise ValueError("Pending payload shape differs from native discovery")
    row, query = payload["row"], payload["query"]
    if (not isinstance(row, dict) or row.get("convocation_id") != int(identity)
            or type(row.get("convocation_id")) is not int or not isinstance(query, dict)
            or query.get("keyword") not in old["keywords"] or query.get("year") not in old["resolved_years"]
            or query.get("object_type") not in old["objects"]
            or type(payload["page_number"]) is not int or not 1 <= payload["page_number"] <= 498):
        raise ValueError("Pending payload ID, keyword or native scope differs")
    try:
        observed = datetime.fromisoformat(payload["observed_at"])
        start, end = date.fromisoformat(query["published_from"]), date.fromisoformat(query["published_to"])
        if not date.fromisoformat(old["published_from"]) <= start <= end <= date.fromisoformat(old["published_to"]):
            raise ValueError()
        capture = _capture_row(row, query, observed)
    except Exception:
        raise ValueError("Pending payload dates or normalized source row are invalid") from None
    if capture.rows[0] != row or capture.query != query:
        raise ValueError("Pending payload is not the exact normalized native source row")
    business = old.get("business_plan", {}).get("keyword_index", {}).get(query["keyword"])
    if ("discovery_business_context" in payload) != bool(business) or payload.get("discovery_business_context") != business:
        raise ValueError("Pending business context differs from the frozen keyword plan")
    return hash_request(payload)


def snapshot_queue(stage):
    raw = stage.client.eval(_CHECK_TYPES + _SNAPSHOT, len(stage.keys), *stage.keys)
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"states", "payloads", "queue", "pending"}:
            raise ValueError()
        # Redis Lua encodes an empty array as {}; accept only the empty map.
        for key in value:
            if value[key] == {}:
                value[key] = []
            if not isinstance(value[key], list) or any(not isinstance(v, str) for v in value[key]):
                raise ValueError()
        for key in ("states", "payloads"):
            pairs = value[key]
            if len(pairs) % 2 or len(set(pairs[::2])) != len(pairs) // 2:
                raise ValueError()
            value[key] = dict(zip(pairs[::2], pairs[1::2]))
        payloads = {}
        for identity, encoded in value["payloads"].items():
            payloads[identity] = json.loads(encoded, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        value["payloads"] = payloads
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Original Redis snapshot is malformed") from None
    return value


def snapshot_stats(value):
    return {"queued": len(value["queue"]), "pending": len(value["pending"]),
            "payloads": len(value["payloads"]), "seen": len(value["states"])}


def validate_queue(value, original, *, expected_seen):
    states, payloads, queue, pending = (value[k] for k in ("states", "payloads", "queue", "pending"))
    if (len(states) != expected_seen or pending or len(queue) != len(set(queue))
            or set(queue) != set(payloads) or set(payloads) != {i for i, state in states.items() if state == "queued"}
            or any(state not in {"queued", "done"} for state in states.values())
            or any(states.get(str(p["convocation_id"])) != "done" for p in original)):
        raise ValueError("Original Redis queue regressed or changed its authoritative state")


def bind_snapshot(base, old, value):
    validate_queue(value, base["previous_done"], expected_seen=base["expected_seen"])
    previous = {str(p["convocation_id"]) for p in base["previous_done"]}
    ids = value["queue"]
    if len(ids) != base["expected_pending"] or set(value["states"]) != previous | set(ids):
        raise ValueError("Original pending queue differs from the exact authorized count")
    hashes = {identity: validate_payload(identity, value["payloads"][identity], old) for identity in ids}
    members = [{"candidate_id": identity, "payload_hash": hashes[identity], "payload": value["payloads"][identity]}
               for identity in ids]
    return {**base, "pending_ids": [int(i) for i in ids], "pending_payload_hashes": hashes,
        "pending_snapshot": members, "snapshot_hash": hash_request(members), "queue_before": snapshot_stats(value),
        "frozen_at": datetime.now(timezone.utc).isoformat()}


def validate_frozen(manifest, old):
    ids, hashes, members = (manifest.get(k) for k in ("pending_ids", "pending_payload_hashes", "pending_snapshot"))
    if (not isinstance(ids, list) or len(ids) != manifest["expected_pending"] or any(type(i) is not int or i <= 0 for i in ids)
            or len(ids) != len(set(ids)) or not isinstance(hashes, dict) or set(hashes) != {str(i) for i in ids}
            or not isinstance(members, list) or len(members) != len(ids)
            or manifest.get("snapshot_hash") != hash_request(members)):
        raise ValueError("Frozen pending snapshot IDs or hashes changed")
    previous = {p["convocation_id"] for p in manifest["previous_done"]}
    if previous.intersection(ids):
        raise ValueError("Continuation snapshot includes previous accepted members")
    if manifest.get("queue_before") != {"queued": len(ids), "pending": 0, "payloads": len(ids), "seen": manifest["expected_seen"]}:
        raise ValueError("Frozen initial queue statistics changed")
    for identity, member in zip(map(str, ids), members):
        if (not isinstance(member, dict) or set(member) != {"candidate_id", "payload_hash", "payload"}
                or member["candidate_id"] != identity or member["payload_hash"] != hashes[identity]
                or validate_payload(identity, member["payload"], old) != hashes[identity]):
            raise ValueError("Frozen pending snapshot payload binding changed")


def read_outcomes(folder, manifest):
    result = {}
    allow = set(manifest["pending_ids"])
    for path in sorted(folder.glob("*/outcome.private.json")):
        value = read_private(path)
        conv = value.get("convocation_id")
        if (type(conv) is not int or conv not in allow or path.parent.name != str(conv)
                or type(value.get("accepted")) is not bool or conv in result
                or value.get("status") not in {"rejected_below_60", "llm1_complete", "needs_review", "documents_missing", "selection_needs_review"}
                or (value["status"] == "rejected_below_60" and value["accepted"] is not False)
                or (value["status"] == "llm1_complete" and value["accepted"] is not True)):
            raise ValueError("Continuation outcome does not bind an authorized durable classification")
        result[conv] = value
    return result


def validate_resume_queue(value, manifest, old, outcomes):
    validate_frozen(manifest, old)
    validate_queue(value, manifest["previous_done"], expected_seen=manifest["expected_seen"])
    previous = {str(p["convocation_id"]) for p in manifest["previous_done"]}
    allowed = set(manifest["pending_payload_hashes"])
    if set(value["states"]) != previous | allowed or not set(value["payloads"]).issubset(allowed):
        raise ValueError("Original queue contains candidates outside the frozen continuation")
    for identity in allowed:
        if value["states"][identity] == "done":
            if int(identity) not in outcomes:
                raise ValueError("Acknowledged continuation member has no durable outcome")
        elif validate_payload(identity, value["payloads"][identity], old) != manifest["pending_payload_hashes"][identity]:
            raise ValueError("Queued continuation payload changed since its frozen snapshot")


class BoundStage:
    """Validate every reservation and require a durable outcome before its ack."""
    def __init__(self, stage, manifest, old, folder):
        self.stage, self.manifest, self.old, self.folder = stage, manifest, old, folder

    def next(self):
        job = self.stage.next()
        if job is None:
            return None
        identity, payload = job
        try:
            expected = self.manifest["pending_payload_hashes"].get(identity)
            if expected is None or validate_payload(identity, payload, self.old) != expected:
                raise ValueError("Reservation differs from the frozen pending allowlist")
        except Exception:
            selected.STOP.set()
            raise  # Keep the Redis reservation pending and recoverable.
        return job

    def ack(self, identity):
        if identity not in self.manifest["pending_payload_hashes"]:
            selected.STOP.set()
            raise ValueError("Cannot acknowledge an unauthorized continuation member")
        outcomes = read_outcomes(self.folder, self.manifest)
        if int(identity) not in outcomes:
            selected.STOP.set()
            raise ValueError("Cannot acknowledge without a durable continuation outcome")
        return self.stage.ack(identity)


class CountingPacer(SharedPacer):
    def __init__(self, seconds):
        super().__init__(seconds)
        self.requests = 0
        self.count_lock = threading.Lock()

    def __call__(self, request=None):
        super().__call__(request)
        with self.count_lock:
            self.requests += 1


def report(folder, manifest, stage, pacer, *, began, errors=()):
    outcomes = read_outcomes(folder, manifest)
    records = [outcomes[i] for i in manifest["pending_ids"] if i in outcomes]
    rejected = [r["convocation_id"] for r in records if r["status"] == "rejected_below_60"]
    complete = [r["convocation_id"] for r in records if r["status"] == "llm1_complete"]
    review = [r["convocation_id"] for r in records if r["status"] not in {"rejected_below_60", "llm1_complete"}]
    missing = [i for i in manifest["pending_ids"] if i not in outcomes]
    stats, costs = stage.stats(), audit_summary(folder)
    return {"policy": POLICY, "run_id": manifest["run_id"], "original_run_id": manifest["original_run_id"],
        "terminal_stage": "llm1", "discovery_sweep_calls": 0, "new_candidates": 0,
        "source_requests_this_execution": pacer.requests, "previous_members_reprocessed": 0,
        "authorized_ids": manifest["pending_ids"], "processed": len(records),
        "rejected_below_60": len(rejected), "rejected_ids": rejected,
        "accepted": sum(r["accepted"] for r in records), "llm1_complete": len(complete), "llm1_complete_ids": complete,
        "needs_review": len(review), "needs_review_ids": review, "pending_ids": missing,
        "complete": not missing and not review and not errors and stats["payloads"] == 0
            and costs["usage_unknown_attempts"] == 0,
        "queue_before": manifest["queue_before"], "queue_after": stats, "costs": costs,
        "model_routing": manifest["llm1_model_routing"], "records": records,
        "worker_error_types": list(errors), "stopped": selected.STOP.is_set(),
        "elapsed_this_execution_seconds": round(time.monotonic() - began, 3),
        "completed_at": datetime.now(timezone.utc).isoformat()}


def main():
    args = parser().parse_args()
    os.umask(0o077)
    base, old = plan(args)
    if args.dry_run:
        print(json.dumps({"policy": POLICY, "run_id": base["run_id"], "expected_pending": args.expected_pending,
            "discovery_sweep_calls": 0, "new_candidates": 0, "source_calls": 0, "provider_calls": 0, "redis_calls": 0,
            "database_writes": 0, "snapshot_requires_locked_redis": True, "model_routing": base["llm1_model_routing"]}))
        return 0
    selected.STOP.clear()
    folder = Path(base["continuation_folder"])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    pacer, began = CountingPacer(args.request_interval), time.monotonic()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in handlers:
        signal.signal(sig, lambda *_: selected.STOP.set())
    try:
        with selected.runtime(args) as (settings, redis_url):
            settings = settings.model_copy(update={"gemini_model": old["gemini_model"]})
            resolve_llm1_model(base, 1, settings.gemini_model)
            jev_key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY")
            if not jev_key:
                raise ValueError("Jev configuration is missing")
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                    application_name="buenapro-prod2-pending:" + folder.name, connect_timeout=15,
                    keepalives_idle=30, keepalives_interval=10, keepalives_count=3) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                    raise RuntimeError("Another PROD2 coordinator owns the executor")
                try:
                    for relation in ("prod2_document_selection_cache", "prod2_document_events"):
                        if not conn.execute("SELECT to_regclass(%s) AS relation", (relation,)).fetchone()["relation"]:
                            raise RuntimeError("Required selector/publication schema is missing")
                    previous = {r["convocation_id"] for r in conn.execute("""SELECT convocation_id FROM prod2_accepted_run_members
                        WHERE run_id=%s AND accepted AND opportunity_id IS NOT NULL""", (old["run_id"],)).fetchall()}
                    if previous != {p["convocation_id"] for p in base["previous_done"]}:
                        raise ValueError("Original PostgreSQL membership differs from previous done evidence")
                    if hash_request(read_private(Path(base["original_folder"]) / "manifest.private.json")) != base["original_manifest_hash"]:
                        raise ValueError("Original manifest changed before queue binding")
                    if original_proofs(Path(base["original_folder"]), old) != base["previous_done"]:
                        raise ValueError("Original accepted outcomes changed before queue binding")
                    client = redis.Redis.from_url(redis_url, decode_responses=True)
                    try:
                        stage = RedisStage(client, base["original_staging_identity"])
                        stage.recover()
                        current = snapshot_queue(stage)
                        marker = folder / "manifest.private.json"
                        manifest = read_private(marker) if marker.exists() else bind_snapshot(base, old, current)
                        if not marker.exists():
                            private_write(marker, manifest)
                        validate_resume_queue(current, manifest, old, read_outcomes(folder, manifest))
                        members_path = folder / "members.private.json"
                        if members_path.exists():
                            if read_private(members_path) != manifest["pending_snapshot"]:
                                raise ValueError("Private pending member capture changed")
                        else:
                            private_write(members_path, manifest["pending_snapshot"])
                        costs = audit_summary(folder)
                        limits = RunLimits(max_accepted=args.expected_pending, max_candidates=args.expected_pending,
                            max_jev_calls=args.max_jev_calls, max_llm1_calls=args.max_llm1_calls,
                            model_workers=args.model_workers, jev_calls=costs["jev_calls"], llm1_calls=costs["llm1_calls"])
                        bound, errors = BoundStage(stage, manifest, old, folder), []
                        with ThreadPoolExecutor(max_workers=args.workers) as pool:
                            workers = [pool.submit(selected.consume, settings, bound, manifest, folder, pacer, limits, jev_key)
                                       for _ in range(args.workers)]
                            for worker in workers:
                                try:
                                    worker.result()
                                except Exception as error:
                                    selected.STOP.set()
                                    errors.append(type(error).__name__)
                        # No consumers remain when pending reservations are recovered.
                        stage.recover()
                        value = report(folder, manifest, stage, pacer, began=began, errors=errors)
                        private_write(folder / ("report-" + str(time.time_ns()) + ".private.json"), value)
                        print(json.dumps({k:v for k,v in value.items() if k != "records"}, ensure_ascii=False), flush=True)
                        return 0 if value["complete"] else 2
                    finally:
                        client.close()
                finally:
                    if not conn.closed and not conn.broken:
                        conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
