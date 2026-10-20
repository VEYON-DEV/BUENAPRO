"""One explicit pre-embedding wrapper recovery; never a general retry policy.

The external authorization binds an operator-reviewed committed runtime diff.
It returns the original handoff binding, preserving all paid fingerprints and
admission receipts. Only apply_recovery may change the one terminal queue item;
it calls neither the source nor a model, and never changes an old report/state.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .service import _canonical, _literal, _read

VERSION = "din_pre_embedding_runtime_recovery_v1"
CASE = 1048448
SCOPE = "prod2-din-ia-industrial-20261020"
ALLOWED_RUNTIME_FILES = {
    "scripts/run_prod2_selected.py",
    "workers/seace/src/buenapro_worker/prod2/din_sector_chain.py",
    "workers/seace/src/buenapro_worker/prod2/handoff_guards.py",
    "workers/seace/src/buenapro_worker/prod2/handoff_runtime_recovery.py",
}


class RecoveryError(ValueError):
    """Controlled codes only; never connection strings or source contents."""


def require(condition, reason):
    if not condition:
        raise RecoveryError(reason)


def sha(path):
    path = _literal(path)
    require(path.is_file(), "recovery_missing_file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def binding(path):
    return dict(path=str(_literal(path)), sha256=sha(path))


def bound(ref):
    require(isinstance(ref, dict) and set(ref) == {"path", "sha256"}
            and isinstance(ref["path"], str) and Path(ref["path"]).is_absolute(), "recovery_invalid_binding")
    path = _literal(ref["path"])
    require(sha(path) == ref["sha256"], "recovery_bound_file_changed")
    return path


def fsync_directory(path):
    descriptor = os.open(_literal(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def exclusive(path, data):
    """Append-only local evidence. A failed previous attempt is reconciled."""
    path = _literal(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == data, "recovery_existing_evidence_changed")
        fsync_directory(path.parent)
        return
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    fsync_directory(path.parent)


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def verify_commit(root, commit, runtime):
    require(isinstance(commit, str) and len(commit) == 40 and all(c in "0123456789abcdef" for c in commit),
            "recovery_explicit_commit_required")
    _git(root, "cat-file", "-e", commit + "^{commit}")
    for name, expected in runtime.items():
        require(hashlib.sha256(_git(root, "show", commit + ":" + name)).hexdigest() == expected,
                "recovery_commit_runtime_mismatch")


def _snapshot_files(snapshot, folder, *, archive=None):
    require(snapshot.get("version") == "din_pre_embedding_recovery_inputs_v1"
            and snapshot.get("reader_folder") == str(folder), "recovery_snapshot_identity_changed")
    rows = snapshot.get("all_case_files")
    require(isinstance(rows, list) and len(rows) == snapshot.get("total_files")
            and len({r["path"] for r in rows}) == len(rows), "recovery_snapshot_inventory_changed")
    for row in rows + snapshot.get("auxiliary_immutable_before_recovery", []):
        relative = Path(row["path"])
        require(not relative.is_absolute() and ".." not in relative.parts, "recovery_snapshot_path_escape")
        path = folder / relative
        if archive is not None and relative == Path(str(CASE)) / "outcome.private.json":
            path = archive
        require(sha(path) == row["sha256"] and path.stat().st_size == row["bytes"],
                "recovery_cached_input_changed")
    require(sum(row["bytes"] for row in rows) == snapshot.get("total_bytes"), "recovery_snapshot_bytes_changed")
    attempts = snapshot.get("paid_jev_attempts")
    require(isinstance(attempts, list) and len(attempts) == 4
            and snapshot.get("uncertain_jev_attempts") == 0
            and all(r.get("usage_known") is True and r.get("status") == "response_received"
                    and r.get("provider") == "jev" and r.get("http_status") == 200 for r in attempts),
            "recovery_uncertain_payment")


def baseline_report(report):
    """Remove exactly the bound prepayment failure, preserving cumulative costs."""
    from .din_sector_chain import assess_batch, budget, check_costs
    check_costs(report.get("costs"))
    costs = report["costs"]
    require(costs["attempts"] == costs["jev_calls"] == 4
            and costs["llm1_calls"] == costs["embedding_calls"] == costs["embedding_attempts"] == 0,
            "recovery_not_pre_embedding")
    failed = [row for row in report["records"] if row.get("convocation_id") == CASE]
    require(len(failed) == 1 and failed[0].get("status") == "needs_review"
            and failed[0].get("accepted") is True and failed[0].get("error_type") == "AttributeError"
            and failed[0].get("error_chain") == [{"error_type": "AttributeError"}]
            and not any(k in failed[0] for k in ("llm1_request_hash", "embedding_dispatch_stop", "document_preparation_review")),
            "recovery_wrong_terminal_failure")
    result = deepcopy(report)
    result["records"] = [row for row in result["records"] if row["convocation_id"] != CASE]
    require(len(result["records"]) == 5 and report["processed"] == 6, "recovery_wrong_terminal_cohort")
    result["processed"] -= 1; result["accepted"] -= 1
    result["requires_review"] = sum(bool(row.get("requires_human_review")) for row in result["records"])
    result["redis"]["queued"] += 1; result["redis"]["payloads"] += 1
    ids = report["handoff_selection"]["candidate_ids"]
    assess_batch(result, report["run_id"], ids, budget(1, len(ids)))
    return result


def prepare_authorization(*, root, old_plan_path, snapshot_path, health_path, new_commit, output):
    """Offline only. Root reviews its bytes/SHA before applying with a new plan."""
    from .handoff_selection import runtime_bindings
    root = _literal(root)
    old_plan = _read(old_plan_path); folder = _literal(old_plan["reader_folder"])
    snapshot = _read(snapshot_path); health = _read(health_path)
    _snapshot_files(snapshot, folder)
    manifest_path = folder / "manifest.private.json"; manifest = _read(manifest_path)
    reports = [row for row in snapshot["auxiliary_immutable_before_recovery"] if row["path"].startswith("report-")]
    require(len(reports) == 1, "recovery_ambiguous_old_report")
    report_path = folder / reports[0]["path"]; report = _read(report_path)
    base = baseline_report(report)
    old = manifest["handoff_selection"]; current = runtime_bindings(root)
    delta = {name: dict(old=old["runtime_bindings"].get(name), new=current.get(name))
             for name in old["runtime_bindings"].keys() | current.keys()
             if old["runtime_bindings"].get(name) != current.get(name)}
    require(set(delta) == ALLOWED_RUNTIME_FILES and all(row["new"] for row in delta.values()),
            "recovery_runtime_diff_not_allowlisted")
    verify_commit(root, new_commit, current)
    verify_commit(root, old_plan["code_commit"], old["runtime_bindings"])
    require(old.get("scope_id") == SCOPE and old == manifest["requested"]["handoff_selection"]
            and report["handoff_selection"] == old and folder.name == report["run_id"]
            and health.get("plan_sha256") == sha(old_plan_path)
            and health.get("pg", {}).get("coordinator_locks") == 0
            and health.get("dispatch_stop_marker") is False
            and health.get("redis") == report["redis"], "recovery_old_evidence_changed")
    from .din_sector_chain import check_costs
    check_costs(health.get("cost_audit_may_include_in_flight"))
    require(health["cost_audit_may_include_in_flight"] == report["costs"], "recovery_health_cost_changed")
    value = dict(version=VERSION, scope_id=SCOPE, convocation_id=CASE,
        reason="embedding_client_metadata_delegation_before_vector_claim", reader_folder=str(folder),
        queue_priority="recovered_pre_embedding_case_first_other_relative_order_preserved",
        old_plan=binding(old_plan_path), manifest=binding(manifest_path), old_report=binding(report_path),
        input_snapshot=binding(snapshot_path), stopped_health=binding(health_path),
        old_code_commit=old_plan["code_commit"], new_code_commit=new_commit,
        runtime_delta=delta, target_runtime_bindings=current,
        frozen_binding_sha256=hashlib.sha256(_canonical(old)).hexdigest(),
        failed_outcome=binding(folder / str(CASE) / "outcome.private.json"),
        old_chain_state=binding(folder.parents[1] / "chain/state.private.json"),
        guard_files=[dict(path=str(p.relative_to(folder)), sha256=sha(p))
                     for p in sorted((folder / "handoff-guards").rglob("*.private.json"))],
        baseline_sha256=hashlib.sha256(_canonical(base)).hexdigest(),
        limits=dict(max_candidates=10, max_accepted=10, max_new_documents=10,
                    max_jev_calls=30, max_llm1_calls=10, max_embedding_calls=100),
        source_calls=0, model_calls=0, historical_retries_authorized=False)
    exclusive(output, _canonical(value))
    return value


def authorization(path, expected_sha256, *, root, reader_folder):
    require(path is not None and expected_sha256 is not None, "recovery_explicit_pair_required")
    require(sha(path) == expected_sha256, "recovery_authorization_sha_mismatch")
    value = _read(path)
    require(value.get("version") == VERSION and value.get("scope_id") == SCOPE
            and type(value.get("convocation_id")) is int and value["convocation_id"] == CASE
            and value.get("reader_folder") == str(_literal(reader_folder))
            and value.get("historical_retries_authorized") is False
            and value.get("reason") == "embedding_client_metadata_delegation_before_vector_claim",
            "recovery_wrong_scope_or_case")
    require(value.get("queue_priority") == "recovered_pre_embedding_case_first_other_relative_order_preserved",
            "recovery_queue_priority_not_authorized")
    manifest = _read(bound(value["manifest"]))
    frozen = manifest["handoff_selection"]
    require(frozen == manifest["requested"]["handoff_selection"]
            and hashlib.sha256(_canonical(frozen)).hexdigest() == value["frozen_binding_sha256"],
            "recovery_frozen_binding_changed")
    from .handoff_selection import runtime_bindings
    current = runtime_bindings(root)
    require(current == value["target_runtime_bindings"], "recovery_live_runtime_changed")
    delta = {name: dict(old=frozen["runtime_bindings"].get(name), new=current.get(name))
             for name in frozen["runtime_bindings"].keys() | current.keys()
             if frozen["runtime_bindings"].get(name) != current.get(name)}
    require(delta == value["runtime_delta"] and set(delta) == ALLOWED_RUNTIME_FILES,
            "recovery_runtime_diff_not_allowlisted")
    verify_commit(root, value["new_code_commit"], current)
    require(_read(bound(value["old_plan"]))["code_commit"] == value["old_code_commit"], "recovery_old_commit_changed")
    report = _read(bound(value["old_report"]))
    require(hashlib.sha256(_canonical(baseline_report(report))).hexdigest() == value["baseline_sha256"],
            "recovery_baseline_changed")
    bound(value["input_snapshot"]); bound(value["stopped_health"])
    return value, frozen


def load_selection(handoff_path, selection_path, selection_sha256, *, root, recovery_path=None,
                   recovery_sha256=None, reader_folder=None):
    from .handoff_selection import load_selection as normal
    require((recovery_path is None) == (recovery_sha256 is None), "recovery_explicit_pair_required")
    imported = normal(handoff_path, selection_path, selection_sha256, root=root)
    if recovery_path is None:
        return imported
    value, frozen = authorization(recovery_path, recovery_sha256, root=root, reader_folder=reader_folder)
    proposed = {**imported["binding"], "runtime_bindings": frozen["runtime_bindings"]}
    require(proposed == frozen, "recovery_paid_or_source_inputs_changed")
    return {**imported, "binding": frozen}


def report_context(path, expected_sha256, *, root, reader_folder):
    value, _ = authorization(path, expected_sha256, root=root, reader_folder=reader_folder)
    return dict(authorization_sha256=expected_sha256, new_code_commit=value["new_code_commit"],
        runtime_bindings=value["target_runtime_bindings"], frozen_binding_identity_only=True)


# Compare the complete run snapshot inside the same Redis execution as the one
# identity change. A SHA marker reconciles an applied response lost in transit.
CAS = """-- din-pre-embedding-recovery-v1
local marker=redis.call('GET',KEYS[5])
if marker then if marker==ARGV[1] then return 0 else return redis.error_reply('recovery marker conflict') end end
local expected=cjson.decode(ARGV[4])
for i=1,2 do
 local actual=redis.call('HGETALL',KEYS[i])
 local count=0
 for _,_ in pairs(expected[i]) do count=count+1 end
 if #actual~=2*count then return redis.error_reply('recovery hash count changed') end
 for j=1,#actual,2 do if expected[i][actual[j]]~=actual[j+1] then return redis.error_reply('recovery hash changed') end end
end
for i=3,4 do
 local actual=redis.call('LRANGE',KEYS[i],0,-1)
 if #actual~=#expected[i] then return redis.error_reply('recovery queue count changed') end
 for j=1,#actual do if actual[j]~=expected[i][j] then return redis.error_reply('recovery queue changed') end end
end
if redis.call('HGET',KEYS[1],ARGV[2])~='done' or redis.call('HEXISTS',KEYS[2],ARGV[2])~=0 then return redis.error_reply('recovery target not terminal') end
redis.call('HSET',KEYS[1],ARGV[2],'queued')
redis.call('HSET',KEYS[2],ARGV[2],ARGV[3])
redis.call('LPUSH',KEYS[3],ARGV[2])
redis.call('SET',KEYS[5],ARGV[1])
return 1
"""


def recovery_paths(chain_folder):
    folder = _literal(chain_folder)
    return folder / "recovery-applied.private.json", folder / "recovery-baseline.private.json", folder / "original-1048448-outcome.private.json"


def validate_applied(plan, chain_folder, *, root):
    ref = plan["runtime_recovery"]
    value, _ = authorization(ref["path"], ref["sha256"], root=root, reader_folder=plan["reader_folder"])
    receipt_path, base_path, archive = recovery_paths(chain_folder)
    receipt = _read(receipt_path)
    require(receipt.get("version") == VERSION and receipt.get("authorization_sha256") == ref["sha256"]
            and receipt.get("baseline") == binding(base_path) and sha(base_path) == value["baseline_sha256"]
            and receipt.get("archived_outcome") == binding(archive)
            and sha(archive) == value["failed_outcome"]["sha256"]
            and receipt.get("plan_sha256") == plan.get("_recovery_plan_sha256"), "recovery_applied_evidence_changed")
    return base_path


def apply_recovery(plan, imported, chain_folder, *, root, plan_sha256, go=False, connect=None, redis_factory=None):
    """Explicit nonpaid operation. Takes the existing global reader lock.

    A partial local/CAS operation stays fail-closed; rerunning this exact
    authorization reconciles its marker and cannot enqueue another copy.
    """
    from .din_sector_chain import (_redis_snapshot, _pg_snapshot, validate_redis, validate_members, check_costs)
    from .selected_payments import audit_summary
    from .staging import RedisStage
    from .runtime import production_staging_settings
    require(go is True, "recovery_explicit_go_required")
    require(isinstance(plan_sha256, str) and len(plan_sha256) == 64
            and all(c in "0123456789abcdef" for c in plan_sha256), "recovery_explicit_new_plan_sha_required")
    require("runtime_recovery" in plan and plan["code_commit"], "recovery_plan_required")
    ref = plan["runtime_recovery"]
    value, frozen = authorization(ref["path"], ref["sha256"], root=root, reader_folder=plan["reader_folder"])
    require(value["new_code_commit"] == plan["code_commit"] and imported["binding"] == frozen,
            "recovery_new_plan_binding_changed")
    folder = _literal(plan["reader_folder"]); chain_folder = _literal(chain_folder)
    require(chain_folder.is_relative_to(_literal(root) / "tmp") and not chain_folder.is_relative_to(folder)
            and not folder.is_relative_to(chain_folder) and not (chain_folder / "state.private.json").exists(),
            "recovery_requires_new_chain_folder")
    receipt_path, base_path, archive = recovery_paths(chain_folder)
    require(not receipt_path.exists(), "recovery_already_applied_use_readback")
    report = _read(bound(value["old_report"])); baseline = baseline_report(report)
    original = folder / str(CASE) / "outcome.private.json"
    archive_exists = archive.exists()
    if archive_exists:
        require(sha(archive) == value["failed_outcome"]["sha256"], "recovery_archive_changed")
    else:
        bound(value["failed_outcome"])
    _snapshot_files(_read(bound(value["input_snapshot"])), folder, archive=archive if archive_exists else None)
    require(not (folder / "handoff-guards/dispatch-stop.private.json").exists(), "recovery_durable_stop")
    check_costs(audit_summary(folder))
    require(audit_summary(folder) == report["costs"], "recovery_paid_audit_changed")
    old_plan = _read(bound(value["old_plan"]))
    old_state = _read(bound(value["old_chain_state"]))
    guard_paths = {str(p.relative_to(folder)) for p in (folder / "handoff-guards").rglob("*.private.json")}
    require(guard_paths == {row["path"] for row in value["guard_files"]}, "recovery_guard_inventory_changed")
    for row in value["guard_files"]:
        path = folder / row["path"]
        require(sha(path) == row["sha256"], "recovery_guard_receipt_changed")
        require(_read(path).get("binding_hash") == value["frozen_binding_sha256"], "recovery_guard_binding_changed")
    for key in ("parent_pid", "child_pid"):
        pid = old_state.get(key)
        require(type(pid) is int and pid > 1, "recovery_missing_old_process_identity")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise RecoveryError("recovery_old_process_still_exists")
    import psycopg
    from psycopg.rows import dict_row
    import redis
    connect = connect or psycopg.connect; redis_factory = redis_factory or redis.Redis.from_url
    with production_staging_settings(**plan["runtime"]) as (settings, redis_url):
        with connect(settings.database_url, row_factory=dict_row, autocommit=True, connect_timeout=15,
                     application_name="buenapro-din-explicit-recovery",
                     options="-c default_transaction_read_only=on -c statement_timeout=30000") as conn:
            require(conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on", "recovery_pg_not_read_only")
            require(_pg_snapshot(conn, folder.name)[0] == 0, "recovery_other_coordinator_active")
            locked = conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]
            require(locked is True, "recovery_global_lock_unavailable")
            client = None
            try:
                require(_pg_snapshot(conn, folder.name)[0] == 1, "recovery_global_lock_count_changed")
                validate_members(_pg_snapshot(conn, folder.name)[1], _read(bound(value["manifest"])),
                                 imported["items"], {r["convocation_id"]:r for r in report["records"]})
                client = redis_factory(redis_url, decode_responses=True)
                keys = RedisStage(client, folder.name).keys
                marker = keys[0].rsplit(":", 1)[0] + ":runtime-recovery:" + ref["sha256"]
                applied = client.get(marker)
                snap = _redis_snapshot(client, folder.name)
                expected_report = baseline if applied == ref["sha256"] else report
                require(applied in (None, ref["sha256"]), "recovery_marker_changed")
                validate_redis(snap, imported["items"], [r["convocation_id"] for r in expected_report["records"]], expected_report["redis"])
                before_path = chain_folder / "recovery-redis-before.private.json"
                if applied is None:
                    exclusive(before_path, _canonical(snap))
                require(before_path.exists(), "recovery_missing_cas_before_evidence")
                before = _read(before_path)
                validate_redis(before, imported["items"], [r["convocation_id"] for r in report["records"]], report["redis"])
                expected_after = deepcopy(before)
                item = next(item for item in imported["items"] if item["row"]["convocation_id"] == CASE)
                expected_after["dedup"][str(CASE)] = "queued"
                expected_after["payloads"][str(CASE)] = item
                # Explicit technical priority: verify the completed corpus
                # first, preserving the relative order of every other ID.
                expected_after["queue"].insert(0, str(CASE))
                expected_after["stats"] = baseline["redis"]
                require(snap == (expected_after if applied is not None else before), "recovery_cas_snapshot_changed")
                if original.exists():
                    require(sha(original) == value["failed_outcome"]["sha256"], "recovery_wrong_original_outcome")
                    exclusive(archive, original.read_bytes())
                    original.unlink()  # Only after the identical immutable copy is fsynced.
                    fsync_directory(original.parent)
                require(sha(archive) == value["failed_outcome"]["sha256"], "recovery_archive_missing")
                if applied is None:
                    expected = [snap["dedup"], {k:_canonical(v).decode() for k,v in snap["payloads"].items()}, snap["queue"], snap["pending"]]
                    # Redis payload byte encoding is preserved in the comparison.
                    expected[1] = client.hgetall(keys[1])
                    require({k:json.loads(v) for k,v in expected[1].items()} == snap["payloads"], "recovery_payload_changed_before_cas")
                    reply = client.eval(CAS, 5, *keys, marker, ref["sha256"], str(CASE), _canonical(item).decode(), _canonical(expected).decode())
                    require(type(reply) is int and reply in (0, 1), "recovery_invalid_cas_reply")
                after = _redis_snapshot(client, folder.name)
                validate_redis(after, imported["items"], [r["convocation_id"] for r in baseline["records"]], baseline["redis"])
                require(after == expected_after, "recovery_cas_post_state_changed")
                require(client.get(marker) == ref["sha256"], "recovery_missing_cas_marker")
                _snapshot_files(_read(bound(value["input_snapshot"])), folder, archive=archive)
                exclusive(base_path, _canonical(baseline))
                receipt = dict(version=VERSION, authorization_sha256=ref["sha256"], plan_sha256=plan_sha256,
                    baseline=binding(base_path), archived_outcome=binding(archive), old_report=value["old_report"],
                    manifest=value["manifest"], input_snapshot=value["input_snapshot"],
                    redis_before=report["redis"], redis_after=after["stats"], active_coordinators_before=0,
                    global_lock_held=True, preserved_costs=report["costs"], source_calls=0, model_calls=0, pg_writes=0,
                    redis_requeued_ids=[CASE], at_utc=datetime.now(timezone.utc).isoformat())
                receipt["queue_priority"] = "recovered_pre_embedding_case_first_other_relative_order_preserved"
                exclusive(receipt_path, _canonical(receipt))
                return receipt
            finally:
                if client is not None: client.close()
                conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")


def main():
    """Preparation only. Applying requires the coordinator's SHA-bound new plan."""
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--old-plan", type=Path, required=True)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--health", type=Path, required=True)
    p.add_argument("--new-commit", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    prepare_authorization(root=args.root, old_plan_path=args.old_plan, snapshot_path=args.snapshot,
        health_path=args.health, new_commit=args.new_commit, output=args.output)
    print(json.dumps(dict(status="prepared_no_services_called", authorization_sha256=sha(args.output))))


if __name__ == "__main__":
    main()
