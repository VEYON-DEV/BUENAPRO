"""Opt-in finite handoff chain. Limits expand, paid/source identities do not.

No IO other than local evidence occurs without --go. An interrupted/failed
child is never retried. Fresh read-only PG/Redis evidence is required before
each subsequent reader invocation; budget deferral alone is not a failure.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

from .keyword_chain import _count, _number, _COST_COUNT_FIELDS, _COST_NUMBER_FIELDS
from .service import _canonical, _literal, _read, _write, _selected
from .handoff_runtime_recovery import RecoveryError

VERSION = "din_sector_chain_v1"
PROFILE = "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"
TENANT = "46ba7840-ae84-4de0-ac98-16fab9e5d5bb"
SHA = re.compile(r"[0-9a-f]{64}")
DOCUMENT_REVIEW_REASONS = {"source_has_no_usable_text", "incomplete_source_text",
                           "retrieval_context_needs_expansion_or_review"}


class ChainError(ValueError):
    """Only controlled codes may be copied to public output/state."""


def require(condition, reason):
    if not condition:
        raise ChainError(reason)


def file_hash(path):
    path = _literal(path)
    require(path.is_file(), "missing_bound_file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    require(isinstance(value, str), "invalid_timestamp")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(date.tzinfo is not None and date.utcoffset() is not None, "invalid_timestamp")
    return date


def bound_file(value):
    require(isinstance(value, dict) and set(value) == {"path", "sha256"}, "invalid_file_binding")
    require(isinstance(value["sha256"], str) and SHA.fullmatch(value["sha256"]), "invalid_sha256")
    require(isinstance(value["path"], str) and Path(value["path"]).is_absolute(), "absolute_file_binding_required")
    path = _literal(value["path"])
    require(path.is_absolute() and file_hash(path) == value["sha256"], "bound_file_changed")
    return path


def budget(number, total, extra_batches=8):
    require(type(total) is int and 1 <= total <= 157, "invalid_finite_scope")
    require(type(extra_batches) is int and 0 <= extra_batches <= 8, "invalid_extra_batches")
    require(type(number) is int and 1 <= number <= math.ceil(total / 10) + extra_batches, "invalid_batch_number")
    count = min(10 * number, total)
    documents = 10 * number
    return dict(max_candidates=count, max_accepted=count, max_new_documents=documents,
                max_llm1_calls=documents, max_jev_calls=3 * documents, max_embedding_calls=10 * documents)


def runtime_bindings(root):
    from .handoff_selection import runtime_bindings as reader_bindings
    return {**reader_bindings(root), **{str(path.relative_to(root)): file_hash(path)
            for path in sorted((root / "scripts").glob("*.py"))}}


def committed_runtime(root, plan):
    """Read Git only. Documentation-only later commits need not change code SHA."""
    names = sorted(plan["runtime_bindings"])
    subprocess.run(["git", "cat-file", "-e", plan["code_commit"] + "^{commit}"], cwd=root,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tracked = subprocess.run(["git", "ls-files", "-z", "--", *names], cwd=root,
                             check=True, capture_output=True).stdout.decode().split("\0")
    require(set(names) <= set(tracked), "uncommitted_runtime_file")
    diff = subprocess.run(["git", "diff", "--name-only", plan["code_commit"], "--", *names],
                          cwd=root, check=True, capture_output=True).stdout
    require(not diff.strip(), "runtime_differs_from_code_commit")


def matching_folder(value, root):
    import importlib
    scripts = _literal(root / "scripts")
    sys.path.insert(0, str(scripts))
    module = importlib.import_module("dinaut_sector_match_scope")
    require(Path(module.__file__).resolve() == scripts / "dinaut_sector_match_scope.py", "unexpected_matching_module")
    return module.sector_folder(value, root)


def load_plan(path, expected_sha256, root):
    require(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256), "explicit_plan_sha256_required")
    require(file_hash(path) == expected_sha256, "plan_sha256_mismatch")
    plan = _read(path)
    keys = {"version", "scope_id", "planning_only", "mapping_handoff", "selection",
            "selection_review", "reader_folder", "matching_folder", "embedding_config",
            "runtime", "runtime_bindings", "code_commit", "caffeinate", "extra_batches", "budget_note"}
    require(isinstance(plan, dict) and set(plan) in (keys, keys | {"runtime_recovery"}) and plan["version"] == VERSION,
            "invalid_chain_plan")
    require(type(plan["planning_only"]) is bool and type(plan["caffeinate"]) is bool,
            "invalid_chain_plan")
    require(isinstance(plan["code_commit"], str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", plan["code_commit"]),
            "explicit_code_commit_required")
    selection_path = bound_file(plan["selection"])
    handoff_path = bound_file(plan["mapping_handoff"])
    review_path = bound_file(plan["selection_review"])
    config = bound_file(plan["embedding_config"])
    require(file_hash(config) == file_hash(root / "docs/sources/prod2-embedding-reading.runtime.v5.json"),
            "embedding_v5_required")
    from .handoff_runtime_recovery import load_selection
    recovery = plan.get("runtime_recovery")
    if recovery is not None:
        bound_file(recovery)
    imported = load_selection(handoff_path, selection_path, plan["selection"]["sha256"], root=root,
        recovery_path=recovery["path"] if recovery else None,
        recovery_sha256=recovery["sha256"] if recovery else None, reader_folder=plan["reader_folder"])
    selection, review = _read(selection_path), _read(review_path)
    ids = [item["row"]["convocation_id"] for item in imported["items"]]
    budget(1, len(ids), plan["extra_batches"])
    require(1 <= len(ids) <= 157 and len(ids) == len(set(ids)), "invalid_finite_scope")
    require(plan["scope_id"] == selection["scope_id"] == review["scope_id"], "scope_identity_changed")
    require(review["selection_sha256"] == plan["selection"]["sha256"]
            and review["included_ids_priority_order"] == ids, "review_selection_changed")
    require(set(review["held_ids"]).isdisjoint(ids), "held_id_in_runtime_scope")
    for key in ("reader_folder", "matching_folder"):
        require(isinstance(plan[key], str) and Path(plan[key]).is_absolute(), "absolute_owned_folder_required")
        folder = _literal(plan[key])
        require(folder.is_absolute() and folder.is_relative_to(root / "tmp"), "invalid_owned_folder")
    require(plan["reader_folder"] != plan["matching_folder"], "overlapping_owned_folders")
    matching_folder(plan["matching_folder"], root)
    runtime = plan["runtime"]
    require(isinstance(runtime, dict) and set(runtime) == {"ssh_host", "ssh_key"}
            and isinstance(runtime["ssh_host"], str) and runtime["ssh_host"]
            and not any(c.isspace() for c in runtime["ssh_host"]), "invalid_runtime")
    require(isinstance(runtime["ssh_key"], str) and Path(runtime["ssh_key"]).is_absolute(), "absolute_ssh_key_required")
    _literal(runtime["ssh_key"])
    require(isinstance(plan["runtime_bindings"], dict), "invalid_runtime_bindings")
    required = {"scripts/run_din_sector_chain.py", "scripts/run_prod2_selected.py",
                "scripts/run_dinaut_sector_matches.py",
                "workers/seace/src/buenapro_worker/prod2/din_sector_chain.py"}
    require(required <= plan["runtime_bindings"].keys(), "missing_runtime_binding")
    require(plan["runtime_bindings"] == runtime_bindings(root), "runtime_binding_changed")
    selected = _selected(root)
    requested = selected.plan(selected.parser().parse_args(reader_argv(plan, root, budget(1, len(ids), plan["extra_batches"]))[2:]))
    require(requested.get("handoff_selection") == imported["binding"], "reader_configuration_binding_changed")
    return plan, imported, review


def check_costs(costs):
    require(isinstance(costs, dict), "invalid_cost_summary")
    require(all(_count(costs.get(key)) for key in _COST_COUNT_FIELDS)
            and all(_number(costs.get(key)) for key in _COST_NUMBER_FIELDS), "invalid_cost_summary")
    require(all(costs[key] == 0 for key in ("usage_unknown_attempts", "embedding_usage_unknown_attempts",
            "embedding_unknown_dispatch_attempts", "embedding_receipt_conflicts")), "uncertain_provider_usage")


def typed_document_review(record):
    proof = record.get("document_preparation_review")
    return (record.get("status") == "needs_review" and record.get("error_type") == "EmbeddingPreparationReview"
        and record.get("error_chain") == [{"error_type": "EmbeddingPreparationReview"}]
        and isinstance(proof, dict) and set(proof) == {"reason", "receipt_sha256"}
        and proof.get("reason") in DOCUMENT_REVIEW_REASONS
        and isinstance(proof.get("receipt_sha256"), str) and bool(SHA.fullmatch(proof["receipt_sha256"])))


def validate_document_review(record, folder):
    require(typed_document_review(record), "untyped_document_preparation_review")
    path = folder / str(record["convocation_id"]) / "embedding-preparation-review.private.json"
    proof, receipt = record["document_preparation_review"], _read(path)
    require(file_hash(path) == proof["receipt_sha256"] and receipt.get("reason") == proof["reason"]
            and receipt.get("full_pdf_fallback") is False and isinstance(receipt.get("corpus_hash"), str)
            and SHA.fullmatch(receipt["corpus_hash"]), "document_review_receipt_changed")
    if proof["reason"] == "retrieval_context_needs_expansion_or_review":
        require(set(receipt) == {"reason", "corpus_hash", "retrieval_hash", "embedding_cost_usd", "full_pdf_fallback"}
                and _number(receipt["embedding_cost_usd"])
                and (receipt["retrieval_hash"] is None or isinstance(receipt["retrieval_hash"], str)
                     and SHA.fullmatch(receipt["retrieval_hash"])), "invalid_document_review_receipt")
    else:
        require(set(receipt) == {"reason", "corpus_hash", "unresolved_pages", "provider_calls", "full_pdf_fallback"}
                and type(receipt["provider_calls"]) is int and receipt["provider_calls"] == 0
                and isinstance(receipt["unresolved_pages"], list)
                and all(type(page) is int and page > 0 for page in receipt["unresolved_pages"]),
                "invalid_document_review_receipt")
    return dict(convocation_id=record["convocation_id"], **proof)


def assess_batch(report, run_id, ids, limits, previous=None):
    """Pure strict gate; queued budget-deferred work is intentionally resumable."""
    require(isinstance(report, dict) and report.get("run_id") == run_id
            and report.get("pipeline") == "prod2_selected_llm1_v1"
            and report.get("terminal_stage") == "llm1", "report_contract_changed")
    require(type(report.get("source_queries_repeated")) is int and report["source_queries_repeated"] == 0
            and report.get("source_coverage_complete") is True, "source_contract_changed")
    require(report.get("embedding_dispatch_stop_requested") is False, "provider_dispatch_stopped")
    check_costs(report.get("costs"))
    policy = report.get("execution_policy")
    require(isinstance(policy, dict) and policy.get("all_results") is False
            and policy.get("effective_limits") == {key: limits[key] for key in (
                "max_accepted", "max_candidates", "max_jev_calls", "max_llm1_calls")}
            and policy.get("max_new_documents") == limits["max_new_documents"], "report_execution_limits_changed")
    guards, queue = report.get("handoff_guards"), report.get("redis")
    require(isinstance(guards, dict) and guards.get("policy_version") == "prod2_handoff_guards_v1"
            and type(guards.get("dispatch_stopped")) is bool
            and type(guards.get("budget_deferred")) is bool, "invalid_handoff_guards")
    require(guards["dispatch_stopped"] is False, "durable_dispatch_stop")
    for field in ("candidate_admissions", "document_admissions", "max_candidates", "max_new_documents"):
        require(_count(guards.get(field)), "invalid_handoff_guards")
    require(guards["max_candidates"] == limits["max_candidates"]
            and guards["max_new_documents"] == limits["max_new_documents"]
            and guards["candidate_admissions"] <= limits["max_candidates"]
            and guards["document_admissions"] <= limits["max_new_documents"], "admission_limit_changed")
    require(isinstance(queue, dict) and all(_count(queue.get(key))
            for key in ("queued", "pending", "payloads", "seen")), "invalid_queue_counts")
    require(queue["seen"] == len(ids) and queue["pending"] == 0
            and queue["payloads"] == queue["queued"], "queue_scope_or_reservation_mismatch")
    for field in ("processed", "accepted", "llm1_complete", "requires_review"):
        require(_count(report.get(field)), "invalid_report_counts")
    records = report.get("records")
    require(isinstance(records, list) and all(isinstance(row, dict) for row in records), "invalid_outcomes")
    by_id = {}
    for row in records:
        identity = row.get("convocation_id")
        require(type(identity) is int and identity in ids and identity not in by_id, "duplicate_or_foreign_outcome")
        require(type(row.get("accepted")) is bool and isinstance(row.get("status"), str)
                and row["status"] in {"llm1_complete", "needs_review", "rejected_below_60",
                    "closed_without_reading", "opening_not_confirmed", "selection_needs_review",
                    "selection_complete", "documents_missing"}, "invalid_outcome")
        require("embedding_dispatch_stop" not in row and (not any(key in row for key in ("error_type", "error_chain"))
                or typed_document_review(row)), "technical_outcome_failure")
        by_id[identity] = object_hash(row)
    require(len(records) == report["processed"]
            and report["processed"] + queue["queued"] == len(ids)
            and report["accepted"] == sum(row["accepted"] for row in records)
            and report["llm1_complete"] == sum(row["status"] == "llm1_complete" for row in records)
            and report["requires_review"] <= len(records), "report_counts_disagree")
    require(report["accepted"] <= limits["max_accepted"] and report["costs"]["llm1_calls"] <= limits["max_llm1_calls"]
            and report["costs"]["jev_calls"] <= limits["max_jev_calls"]
            and report["costs"]["embedding_calls"] <= limits["max_embedding_calls"], "provider_limit_exceeded")
    coverage = report.get("coverage")
    require(isinstance(coverage, list) and len(coverage) == 1
            and coverage[0].get("coverage_complete") is True
            and coverage[0].get("selected_ids") == ids
            and not coverage[0].get("errors"), "handoff_coverage_changed")
    require(report.get("scope_proofs") == [] and type(report.get("queries_total")) is int
            and type(report.get("queries_complete")) is int
            and report["queries_total"] == report.get("queries_complete") == 1, "handoff_query_counts_changed")
    status = report.get("execution_status")
    require(status == "budget_deferred" if queue["queued"] else status in {"complete", "complete_with_reviews"},
            "invalid_execution_status")
    counters = {key: report[key] for key in ("processed", "accepted", "llm1_complete")}
    counters.update({key: report["costs"][key] for key in _COST_COUNT_FIELDS})
    counters.update({key: guards[key] for key in ("candidate_admissions", "document_admissions")})
    if previous:
        require(all(by_id.get(int(key)) == value for key, value in previous["outcomes"].items()),
                "terminal_outcome_changed")
        require(all(counters[key] >= value for key, value in previous["counters"].items()), "cumulative_count_regressed")
        require(report["costs"]["known_cost_usd"] >= previous["known_cost_usd"], "known_cost_regressed")
    return {"outcomes": by_id, "counters": counters, "known_cost_usd": report["costs"]["known_cost_usd"],
            "complete": queue["queued"] == 0, "processed": report["processed"]}


def validate_redis(snapshot, items, outcomes, expected_stats=None):
    """Validate one atomic read-only snapshot of this run's four keys only."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"dedup", "payloads", "queue", "pending", "stats"},
            "invalid_redis_snapshot")
    dedup, payloads, queued, pending, stats = (snapshot[key] for key in ("dedup", "payloads", "queue", "pending", "stats"))
    require(isinstance(dedup, dict) and isinstance(payloads, dict) and isinstance(queued, list)
            and isinstance(pending, list) and isinstance(stats, dict), "invalid_redis_snapshot")
    require(all(isinstance(v, str) for v in [*dedup, *payloads, *queued, *pending]), "invalid_redis_identity")
    expected = {str(item["row"]["convocation_id"]): item for item in items}
    terminal = {str(i) for i in outcomes}
    require(len(queued) == len(set(queued)) and len(pending) == len(set(pending))
            and not set(queued) & set(pending), "duplicate_queue_identity")
    require(set(dedup) == set(expected) and set(payloads) == set(queued) | set(pending)
            and terminal.isdisjoint(payloads) and terminal | set(payloads) == set(expected), "queue_outcome_partition_changed")
    require(all(dedup[i] == "done" for i in terminal)
            and all(dedup[i] == "queued" for i in queued) and all(dedup[i] == "pending" for i in pending), "dedup_state_changed")
    require(all(payloads[i] == expected[i] for i in payloads), "queued_payload_binding_changed")
    actual = dict(queued=len(queued), pending=len(pending), payloads=len(payloads), seen=len(dedup))
    require(stats == actual and all(_count(stats.get(k)) for k in actual), "redis_count_mismatch")
    if expected_stats is not None:
        require(actual == expected_stats, "fresh_redis_report_mismatch")
    return actual


def _pg_snapshot(conn, run_id):
    """Only SELECTs, scoped member references and the executor's exact lock."""
    lock = conn.execute("""SELECT count(*) AS count FROM pg_locks WHERE locktype='advisory'
        AND classid::bigint=(hashtext('prod2_accepted_executor')::bigint & 4294967295)
        AND objid=0 AND objsubid=2 AND granted""").fetchone()["count"]
    members = conn.execute("""SELECT m.*,c.status AS cache_status,c.model AS cache_model,
        c.policy_version AS cache_policy,c.result_json AS cache_result
        FROM prod2_accepted_run_members m JOIN prod2_jev_decision_cache c ON c.cache_key=m.request_hash
        WHERE m.run_id=%s ORDER BY m.convocation_id""", (run_id,)).fetchall()
    return lock, members


def _redis_snapshot(client, run_id):
    from .staging import RedisStage
    keys = RedisStage(client, run_id).keys
    with client.pipeline(transaction=True) as pipe:
        pipe.hgetall(keys[0]); pipe.hgetall(keys[1]); pipe.lrange(keys[2], 0, -1); pipe.lrange(keys[3], 0, -1)
        pipe.llen(keys[2]); pipe.llen(keys[3]); pipe.hlen(keys[1]); pipe.hlen(keys[0])
        dedup, payloads, queue, pending, q, p, payload_count, seen = pipe.execute()
    import json
    return dict(dedup=dedup, payloads={key: json.loads(value) for key, value in payloads.items()},
                queue=queue, pending=pending, stats=dict(queued=q, pending=p, payloads=payload_count, seen=seen))


def validate_members(members, manifest, items, records):
    from .analysis import jev_request_hash
    from .discovery_registry import normalize_entry
    from .accepted_repository import _result, _same_result
    expected = {item["row"]["convocation_id"]: item for item in items}
    seen, compact = set(), []
    for member in members:
        conv = member["convocation_id"]
        require(type(conv) is int and conv in expected and conv not in seen, "foreign_or_duplicate_pg_member")
        seen.add(conv)
        result = member["result_json"]
        require(member["run_id"] == manifest["run_id"] and member["cache_status"] == "done"
                and type(member["accepted"]) is bool and _same_result(result, member["cache_result"]), "pg_decision_not_exact")
        _result(result, model=member["cache_model"], policy_version=member["cache_policy"], request_hash=member["request_hash"])
        require(member["request_hash"] == jev_request_hash(expected[conv]["row"]["description"],
                commercial_profiles=manifest["commercial_profiles"], model=member["cache_model"])
                and member["accepted"] == (result["affinity_index"] >= 60), "pg_decision_binding_changed")
        if member["accepted"]:
            require(member["opportunity_id"] is not None and isinstance(member["row_manifest"], dict), "pg_promotion_incomplete")
            normalized = normalize_entry(dict(row=member["row_manifest"], query=member["query_manifest"],
                page_number=1, observed_at=member["observed_at"].isoformat()))
            require(normalized["content_hash"] == expected[conv]["handoff_provenance"]["listing_content_hash"],
                    "pg_listing_binding_changed")
        if conv in records:
            record = records[conv]
            require(record["accepted"] == member["accepted"]
                    and record.get("affinity_index") == result["affinity_index"], "pg_outcome_decision_changed")
        compact.append(dict(convocation_id=conv, accepted=member["accepted"], request_hash=member["request_hash"],
                            decision_sha256=object_hash(result)))
    for conv, record in records.items():
        if record["status"] == "rejected_below_60" or (record["accepted"] and not record.get("llm1_prior_preserved")):
            require(conv in seen, "missing_pg_member")
    return compact


def historical_reading_proof(conn, record, profile):
    """Verify the immutable former reading, without claiming current readiness.

    The reader already validated this exact paid bundle before refreshing the
    ficha. A document becoming unlisted cannot invalidate payment preservation;
    its durable identity/content must still match that original proof exactly.
    Neither is_listed nor is_current is rewritten or used as a readiness claim.
    """
    from .prior_reading_guard import _PRIOR_READINGS_SQL
    from .repository import _id, _sha, _uuid
    from .llm1_completion import _source_reference
    expected = record.get("prior_reading_preserved", {}).get("canonical")
    require(record.get("llm1_prior_preserved") is True and record.get("matching_eligible") is False
            and isinstance(expected, dict), "historical_hold_proof_required")
    conv = record["convocation_id"]
    rows = conn.execute(_PRIOR_READINGS_SQL, (conv, profile)).fetchall()
    rows = [row for row in rows if row.get("evaluation_id") == record.get("llm1_evaluation_id")
            and row.get("extraction_id") == record.get("extraction_id")]
    require(len(rows) == 1, "historical_reading_identity_ambiguous")
    row = rows[0]
    require(row.get("id_convocatoria") == conv and row.get("extraction_convocation_id") == conv
            and str(row.get("profile_id")) == profile and row.get("kind") == "gemini_extraction"
            and row.get("status") == "done" and row.get("policy_version") == "prod2_document_extraction_v1"
            and row.get("quality") in {"auto", "reviewed", "corrected"}
            and type(row.get("requires_human_review")) is bool and _count(row.get("facet_count")),
            "historical_reading_contract_changed")
    evaluation, extraction = _id(row["evaluation_id"]), _id(row["extraction_id"])
    require(_id(row["extraction_evaluation_id"]) == evaluation, "historical_extraction_owner_changed")
    opportunity, code, digest = _uuid(row["opportunity_id"]), _uuid(row["document_uuid"]), _sha(row["sha256_original"])
    request = _sha(row["request_hash"])
    require(opportunity == _uuid(row["canonical_opportunity_id"]) == _uuid(row["canonical_opportunity_exists"])
            and row.get("download_sha256") == digest and type(row.get("download_bytes")) is int
            and row["download_bytes"] > 0 and isinstance(row.get("completed_at"), datetime)
            and row["completed_at"].tzinfo is not None, "historical_source_reference_changed")
    bundle, raw = row.get("result_json"), row.get("raw_extraction_json")
    require(isinstance(bundle, dict) and isinstance(raw, dict) and raw
            and bundle.get("raw_extraction_json") == raw and bundle.get("document_uuid") == code
            and bundle.get("document_sha256") == bundle.get("original_sha256") == digest
            and bundle.get("cache_key") == "prod2:llm1:" + request
            and type(bundle.get("requires_human_review")) is bool
            and bundle["requires_human_review"] == row["requires_human_review"]
            and all(_count(bundle.get(k)) for k in ("input_tokens", "output_tokens"))
            and _number(bundle.get("cost_usd")), "historical_paid_bundle_changed")
    for bkey, rkey in (("model", "extraction_model"), ("prompt_version", "extraction_prompt_version"),
                       ("schema_version", "extraction_schema_version")):
        require(isinstance(bundle.get(bkey), str) and bundle[bkey] == row.get(rkey), "historical_model_schema_changed")
    require(bundle["model"] == row.get("reserved_model"), "historical_reserved_model_changed")
    _sha(bundle["analysis_sha256"]); _sha(bundle["prompt_hash"])
    proof = dict(evaluation_id=evaluation, extraction_id=extraction, request_hash=request,
        document_uuid=code, document_sha256=digest, source_url=_source_reference(row["source_url"], code),
        opportunity_id=opportunity, model=bundle["model"], prompt_version=bundle["prompt_version"],
        schema_version=bundle["schema_version"], requires_human_review=row["requires_human_review"],
        facet_count=row["facet_count"], paid_result_sha256=object_hash(bundle), raw_extraction_sha256=object_hash(raw))
    require(proof == expected and record.get("llm1_request_hash") == request
            and record.get("llm1_model") == bundle["model"], "historical_frozen_proof_changed")
    return proof


def fresh_readback(plan, imported, report_path=None, *, now=utcnow, expected_limits=None):
    """Explicit runtime-only readback. Never mutates PG/Redis or calls a source/provider."""
    from .runtime import production_staging_settings
    from .selected_payments import audit_summary
    from .prior_reading_guard import preserve_prior_reading, POLICY
    import psycopg
    from psycopg.rows import dict_row
    import redis
    folder = _literal(plan["reader_folder"])
    ids = imported["binding"]["candidate_ids"]
    manifest = _read(folder / "manifest.private.json") if report_path else None
    report = _read(report_path) if report_path else None
    records = {row["convocation_id"]: row for row in report["records"]} if report else {}
    if report:
        recovery_baseline = False
        if _literal(report_path).parent != folder:
            from .handoff_runtime_recovery import validate_applied
            require(plan.get("runtime_recovery") is not None and plan.get("_recovery_chain_folder") is not None,
                    "report_outside_reader_folder")
            require(_literal(report_path) == validate_applied(plan, plan["_recovery_chain_folder"], root=Path(__file__).resolve().parents[5]),
                    "report_outside_reader_folder")
            recovery_baseline = True
        if plan.get("runtime_recovery") and not recovery_baseline:
            from .handoff_runtime_recovery import report_context
            recovery = plan["runtime_recovery"]
            require(report.get("runtime_recovery") == report_context(recovery["path"], recovery["sha256"],
                root=Path(__file__).resolve().parents[5], reader_folder=folder), "recovery_execution_runtime_not_reported")
        require(expected_limits is not None and expected_limits in [budget(n, len(ids), plan["extra_batches"])
            for n in range(1, math.ceil(len(ids) / 10) + plan["extra_batches"] + 1)], "explicit_finite_readback_limits_required")
        assess_batch(report, folder.name, ids, expected_limits)
        require(manifest.get("handoff_selection") == imported["binding"] and manifest.get("run_id") == folder.name,
                "reader_manifest_binding_changed")
        require(report.get("handoff_selection") == manifest["handoff_selection"], "report_selection_binding_changed")
        require(not (folder / "handoff-guards/dispatch-stop.private.json").exists(), "durable_dispatch_stop")
        require(audit_summary(folder) == report["costs"], "fresh_audit_report_mismatch")
        local = {}
        for path in folder.glob("*/outcome.private.json"):
            row = _read(path)
            require(type(row.get("convocation_id")) is int and path.parent.name == str(row["convocation_id"])
                    and row["convocation_id"] not in local, "invalid_local_outcome_identity")
            local[row["convocation_id"]] = row
        require(local == records, "local_report_outcome_mismatch")
    else:
        require(not folder.exists(), "reader_folder_already_exists")
    document_reviews = [validate_document_review(row, folder) for row in records.values()
                        if row.get("document_preparation_review") is not None]
    with production_staging_settings(**plan["runtime"]) as (settings, redis_url):
        with psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=15,
                application_name="buenapro-din-sector-readback",
                options="-c default_transaction_read_only=on -c statement_timeout=30000") as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            require(conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on", "pg_not_read_only")
            locks, members = _pg_snapshot(conn, folder.name)
            require(locks == 0, "another_reader_coordinator_active")
            pg = validate_members(members, manifest, imported["items"], records) if manifest else []
            require(manifest is not None or not members, "existing_pg_run_members")
            readings = []
            for conv, record in records.items():
                if record["status"] != "llm1_complete":
                    continue
                if record.get("llm1_prior_preserved") is True and record.get("matching_eligible") is False:
                    historical = historical_reading_proof(conn, record, manifest["profile_id"])
                    readings.append(dict(convocation_id=conv, evaluation_id=historical["evaluation_id"],
                        extraction_id=historical["extraction_id"], historical_only=True, matching_eligible=False,
                        paid_result_sha256=historical["paid_result_sha256"]))
                    continue
                proof = preserve_prior_reading(conn, convocation_id=conv, profile_id=manifest["profile_id"], policy=POLICY)
                require(proof is not None and proof["status"] == "llm1_complete", "pg_reading_not_current_verified")
                for key in ("llm1_evaluation_id", "llm1_request_hash", "extraction_id", "llm1_model"):
                    require(record.get(key) == proof.get(key), "pg_reading_reference_changed")
                if record.get("llm1_receipt_hash"):
                    require(record["llm1_receipt_hash"] == proof["prior_reading_preserved"]["canonical"]["paid_result_sha256"],
                            "pg_reading_receipt_changed")
                readings.append({"convocation_id": conv, **{key: proof[key] for key in (
                    "llm1_evaluation_id", "llm1_request_hash", "extraction_id")}})
            client = redis.Redis.from_url(redis_url, decode_responses=True)
            try:
                snapshot = _redis_snapshot(client, folder.name)
            finally:
                client.close()
            if report:
                stats = validate_redis(snapshot, imported["items"], records, report["redis"])
            else:
                require(snapshot == dict(dedup={}, payloads={}, queue=[], pending=[],
                    stats=dict(queued=0, pending=0, payloads=0, seen=0)), "existing_redis_run")
                stats = snapshot["stats"]
            conn.rollback()
    return dict(version=VERSION, checked_at_utc=now().isoformat(), run_id=folder.name,
        folder=str(folder), selection_sha256=plan["selection"]["sha256"],
        report_sha256=file_hash(report_path) if report_path else None,
        exact_ids_verified=True, active_coordinators=0, pg_members=pg, readings=readings,
        outcome_ids=sorted(records), remaining_ids=sorted(set(ids) - set(records)), redis=stats,
        document_preparation_reviews=document_reviews,
        source_calls=0, model_calls=0, pg_writes=0, redis_writes=0)


def check_readback(value, plan, report_path, ids, now):
    require(isinstance(value, dict) and value.get("version") == VERSION
            and value.get("run_id") == Path(plan["reader_folder"]).name
            and value.get("folder") == plan["reader_folder"]
            and value.get("selection_sha256") == plan["selection"]["sha256"]
            and value.get("report_sha256") == (file_hash(report_path) if report_path else None)
            and value.get("exact_ids_verified") is True and type(value.get("active_coordinators")) is int
            and value["active_coordinators"] == 0, "invalid_readback_binding")
    require(0 <= (now - aware(value["checked_at_utc"])).total_seconds() <= 120, "stale_readback")
    if report_path:
        report = _read(report_path)
        require(isinstance(value.get("outcome_ids"), list) and isinstance(value.get("remaining_ids"), list)
                and all(type(i) is int for i in value["outcome_ids"] + value["remaining_ids"]), "invalid_readback_ids")
        require(value.get("redis") == report["redis"]
                and value.get("outcome_ids") == sorted(row["convocation_id"] for row in report["records"])
                and value.get("remaining_ids") == sorted(set(ids) - set(value["outcome_ids"])), "readback_partition_changed")


def reader_argv(plan, root, limits):
    argv = [sys.executable, str(root / "scripts/run_prod2_selected.py"), "--folder", plan["reader_folder"],
        "--mapping-handoff", plan["mapping_handoff"]["path"], "--selection", plan["selection"]["path"],
        "--selection-sha256", plan["selection"]["sha256"], "--preserve-prior-readings",
        "--document-storage", "official", "--llm1-fallback", "none", "--llm1-input", "embeddings",
        "--embedding-config", plan["embedding_config"]["path"], "--llm1-scope", "qualification",
        "--llm1-model-routing", "pages20", "--llm1-commercial-taxonomy", "commercial_taxonomy_v1",
        "--document-previews", "--archive-7z", "--selector-policy", "v3", "--llm1-ficha-context",
        "--search-workers", "1", "--processing-workers", "2",
        "--model-workers", "2", "--request-interval", "2", "--ssh-host", plan["runtime"]["ssh_host"],
        "--ssh-key", plan["runtime"]["ssh_key"]]
    for key, value in limits.items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    if plan.get("runtime_recovery"):
        argv += ["--runtime-recovery", plan["runtime_recovery"]["path"],
                 "--runtime-recovery-sha256", plan["runtime_recovery"]["sha256"]]
    return argv


def matching_selection(report, review, report_path, now):
    """Preserve every disposition; matching's freeze alone admits gated cases."""
    reasons = {row["convocation_id"]: row for row in review["decisions"] if row["decision"] == "included"}
    cases, excluded = [], []
    for record in report["records"]:
        conv = record["convocation_id"]
        ready = (record["status"] == "llm1_complete" and record.get("confirmed_open") is True
                 and record.get("requires_fresh_lifecycle") is False and record.get("matching_eligible") is True)
        try:
            ready = ready and aware(record["temporal_observed_at"]) <= now < aware(record["closes_at"])
        except (KeyError, ValueError, TypeError):
            ready = False
        if not ready:
            excluded.append(dict(convocation_id=conv, reason="reader_or_fresh_lifecycle_not_matching_ready"))
        cases.append(dict(source_key="seace_prod2", source_id=conv,
            sector_categories=[reasons[conv]["sector"]], selection_reason=reasons[conv]["reason"],
            lifecycle={key: record.get(key) for key in ("confirmed_open", "temporal_state",
                "temporal_observed_at", "requires_fresh_lifecycle", "matching_eligible", "closes_at")}))
    return dict(version="dinaut_sector_selection_v1", profile_id=PROFILE, tenant_id=TENANT,
        reader_evidence=dict(path=str(report_path), sha256=file_hash(report_path)), cases=cases), excluded


def assess_matching(progress, selection):
    from decimal import Decimal, InvalidOperation
    require(isinstance(progress, dict) and progress.get("complete") is True
            and progress.get("held", False) is False
            and progress.get("uncertain_cost_not_zero", False) is False, "matching_incomplete_or_held")
    require(type(progress.get("uncertain_provider_attempts")) is int
            and progress["uncertain_provider_attempts"] == 0, "uncertain_matching_provider_usage")
    require(all(_count(progress.get(key)) for key in ("target", "selected", "excluded"))
            and progress["selected"] == len(selection["cases"])
            and progress["target"] + progress["excluded"] == progress["selected"], "matching_counts_disagree")
    expected = {(row["source_key"], row["source_id"]) for row in selection["cases"]}
    results = progress.get("results")
    require(isinstance(results, list) and len(results) == progress["target"], "matching_results_incomplete")
    seen = set()
    for result in results:
        require(isinstance(result, dict) and type(result.get("source_id")) is int, "invalid_matching_result")
        key = result.get("source_key"), result["source_id"]
        require(key in expected and key not in seen and result.get("match_id") is not None,
                "duplicate_or_foreign_matching_result")
        seen.add(key)
    cost = progress.get("known_cost_usd")
    require(type(cost) in (str, int, float), "invalid_matching_cost")
    try:
        cost = Decimal(str(cost))
    except InvalidOperation:
        raise ChainError("invalid_matching_cost") from None
    require(cost.is_finite() and cost >= 0, "invalid_matching_cost")


@contextmanager
def instance_lock(folder):
    path = _literal(folder / "chain.lock")
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run(plan_path, expected_sha256, folder, *, project_root, go=False, now=utcnow,
        readback=fresh_readback, popen=subprocess.Popen):
    root, folder = _literal(project_root), _literal(folder)
    plan, imported, review = load_plan(plan_path, expected_sha256, root)
    if plan.get("runtime_recovery"):
        plan = {**plan, "_recovery_chain_folder": str(folder), "_recovery_plan_sha256": expected_sha256}
    if not go:
        return {"status": "prepared", "plan_sha256": expected_sha256,
                "selected_ids": len(imported["items"]), "runtime_started": False}
    require(plan["planning_only"] is False, "planning_only_requires_explicit_new_plan")
    committed_runtime(root, plan)
    require(folder.is_relative_to(root / "tmp") and folder not in {
        Path(plan["reader_folder"]), Path(plan["matching_folder"])}, "invalid_chain_folder")
    require(all(not folder.is_relative_to(Path(plan[key])) and not Path(plan[key]).is_relative_to(folder)
                for key in ("reader_folder", "matching_folder")), "overlapping_owned_folders")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = folder / "state.private.json"
    with instance_lock(folder):
        state = _read(state_path) if state_path.exists() else {"version": VERSION, "plan_sha256": expected_sha256}
        require(state.get("plan_sha256") == expected_sha256, "frozen_plan_changed")
        child, caffeine, forwarded, handlers, inflight = None, None, False, {}, None
        stopped = threading.Event()
        def checkpoint(status, **values):
            state.update(status=status, parent_pid=os.getpid(), updated_at_utc=now().isoformat(), **values)
            _write(state_path, state)
        def stop(*_):
            stopped.set()
        def drain(process):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait()  # Never SIGKILL a response in flight.
        def child_run(argv, label):
            nonlocal child, forwarded, inflight
            load_plan(plan_path, expected_sha256, root)
            committed_runtime(root, plan)
            require(not stopped.is_set(), "operator_stop")
            log_path = _literal(folder / (label + ".private.log"))
            receipt = dict(argv=argv, plan_sha256=expected_sha256, started_at_utc=now().isoformat())
            inflight = (label, receipt)
            _write(folder / (label + ".argv.private.json"), receipt)
            checkpoint("starting_child", child_stage=label)
            with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as log:
                forwarded = False
                child = popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                receipt["pid"] = child.pid
                checkpoint("running_child", child_pid=child.pid)
                while child.poll() is None:
                    if stopped.is_set() and not forwarded:
                        child.send_signal(signal.SIGTERM); forwarded = True
                    time.sleep(.2)
                receipt.update(exit_code=child.returncode, ended_at_utc=now().isoformat())
                child = None
            _write(folder / (label + ".exit.private.json"), receipt)
            inflight = None
            require(not stopped.is_set(), "operator_stop_requires_review")
            require(receipt["exit_code"] == 0, "child_failed_no_automatic_retry")
        try:
            require(state.get("status") in {None, "approved_batch", "complete"}, "interrupted_or_failed_chain_requires_review")
            if state.get("status") is None:
                require(not Path(plan["matching_folder"]).exists(), "existing_matching_folder_requires_review")
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    handlers[sig] = signal.getsignal(sig); signal.signal(sig, stop)
            if plan["caffeinate"]:
                require(sys.platform == "darwin" and Path("/usr/bin/caffeinate").is_file(), "caffeinate_unavailable")
                caffeine = popen(["/usr/bin/caffeinate", "-di", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ids = imported["binding"]["candidate_ids"]
            last_path = Path(state["report_path"]) if state.get("report_path") else None
            if plan.get("runtime_recovery") and last_path is None:
                from .handoff_runtime_recovery import validate_applied
                last_path = validate_applied(plan, folder, root=root)
                limits = budget(1, len(ids), plan["extra_batches"])
                state.update(report_path=str(last_path), report_sha256=file_hash(last_path),
                    reader_manifest_sha256=file_hash(Path(plan["reader_folder"]) / "manifest.private.json"),
                    limits=limits, gate=assess_batch(_read(last_path), Path(plan["reader_folder"]).name, ids, limits),
                    completed_batches=0, runtime_recovery=plan["runtime_recovery"])
            if last_path:
                require(file_hash(last_path) == state["report_sha256"], "approved_report_changed")
                require(file_hash(Path(plan["reader_folder"]) / "manifest.private.json") == state["reader_manifest_sha256"],
                        "reader_manifest_changed")
                assess_batch(_read(last_path), Path(plan["reader_folder"]).name, ids,
                             state["limits"], state.get("gate"))
            fresh = readback(plan, imported, last_path, now=now, expected_limits=state.get("limits"))
            check_readback(fresh, plan, last_path, ids, now())
            _write(folder / ("readback-" + str(time.time_ns()) + ".private.json"), fresh)
            if state.get("status") == "complete":
                require(file_hash(state["matching_progress_path"]) == state["matching_progress_sha256"], "matching_evidence_changed")
                match = _read(folder / "matching-selection.private.json")
                require(file_hash(folder / "matching-selection.private.json") == state["matching_selection_sha256"],
                        "matching_selection_changed")
                assess_matching(_read(state["matching_progress_path"]), match)
                return {"status": "complete", "exit_code": 0}
            number = state.get("completed_batches", 0) + 1
            while not state.get("gate", {}).get("complete"):
                require(number <= math.ceil(len(ids) / 10) + plan["extra_batches"], "finite_scope_limits_exhausted")
                limits = budget(number, len(ids), plan["extra_batches"])
                run_folder = Path(plan["reader_folder"])
                if state.get("reader_manifest_sha256"):
                    require(file_hash(run_folder / "manifest.private.json") == state["reader_manifest_sha256"],
                            "reader_manifest_changed")
                before = {str(p): file_hash(p) for p in run_folder.glob("report-*.private.json")}
                _write(folder / f"reader-{number:03d}.limits.private.json", limits)
                child_run(reader_argv(plan, root, limits), f"reader-{number:03d}")
                require(all(file_hash(p) == digest for p, digest in before.items()), "previous_report_changed")
                after = [p for p in run_folder.glob("report-*.private.json") if str(p) not in before]
                require(len(after) == 1, "missing_or_ambiguous_new_report")
                last_path = after[0]
                report = _read(last_path)
                manifest_sha = file_hash(run_folder / "manifest.private.json")
                require(not state.get("reader_manifest_sha256") or state["reader_manifest_sha256"] == manifest_sha,
                        "reader_manifest_changed")
                gate = assess_batch(report, run_folder.name, ids, limits, state.get("gate"))
                fresh = readback(plan, imported, last_path, now=now, expected_limits=limits)
                check_readback(fresh, plan, last_path, ids, now())
                rb_path = folder / f"readback-{number:03d}.private.json"
                _write(rb_path, fresh)
                checkpoint("approved_batch", completed_batches=number, limits=limits, gate=gate,
                    reader_manifest_sha256=manifest_sha,
                    report_path=str(last_path), report_sha256=file_hash(last_path),
                    readback_path=str(rb_path), readback_sha256=file_hash(rb_path))
                number += 1
            report = _read(last_path)
            match, excluded = matching_selection(report, review, last_path, now())
            match_path = folder / "matching-selection.private.json"
            _write(match_path, match)
            _write(folder / "matching-exclusions.private.json", excluded)
            if match["cases"]:
                require(not Path(plan["matching_folder"]).exists(), "existing_matching_folder_requires_review")
                common = ["--selection", str(match_path), "--expected-sha256", file_hash(match_path),
                          "--folder", plan["matching_folder"]]
                for stage in ("scope", "freeze", "prepare", "execute"):
                    argv = [sys.executable, str(root / "scripts/run_dinaut_sector_matches.py"), stage, *common,
                            "--ssh-host", plan["runtime"]["ssh_host"], "--ssh-key", plan["runtime"]["ssh_key"]]
                    if stage == "execute":
                        argv += ["--go", "--batch-size", "10", "--max-batches", str(math.ceil(len(match["cases"]) / 10)),
                                 "--model-workers", "2"]
                    child_run(argv, "matching-" + stage)
                progress_path = Path(plan["matching_folder"]) / "progress.private.json"
                progress = _read(progress_path)
                assess_matching(progress, match)
            else:
                progress_path = folder / "matching-empty.private.json"
                progress = dict(complete=True, target=0, selected=0, excluded=0,
                    results=[], known_cost_usd="0", uncertain_provider_attempts=0, model_calls=0)
                _write(progress_path, progress)
            checkpoint("complete", matching_progress_path=str(progress_path),
                       matching_progress_sha256=file_hash(progress_path), matching_candidates=len(match["cases"]),
                       matching_selection_sha256=file_hash(match_path), matching_exclusions=progress["excluded"],
                       matching_target=progress["target"], reader_matching_not_ready=len(excluded),
                       held_outside_scope=len(review["held_ids"]))
            return {"status": "complete", "exit_code": 0}
        except Exception as error:
            reason = str(error) if isinstance(error, (ChainError, RecoveryError)) else type(error).__name__
            checkpoint("attention_required", reason=reason)
            return {"status": "attention_required", "reason": reason, "exit_code": 2}
        finally:
            if child is not None and child.poll() is None:
                if forwarded:
                    child.wait()
                else:
                    drain(child)
            if child is not None and inflight is not None:
                label, receipt = inflight
                receipt.update(exit_code=child.returncode, ended_at_utc=now().isoformat(), interrupted=True)
                _write(folder / (label + ".exit.private.json"), receipt)
            drain(caffeine)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def main(*, project_root):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--go", action="store_true")
    parser.add_argument("--readback", type=Path, help="Only fresh PG/Redis readback of this report; still requires --go")
    parser.add_argument("--output", type=Path, help="Private readback receipt path")
    parser.add_argument("--limits-file", type=Path, help="Frozen reader-NNN.limits.private.json required for readback")
    parser.add_argument("--apply-recovery", action="store_true", help="Explicit nonpaid recovery only; never starts a reader")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(not (args.readback and args.apply_recovery), "choose_readback_or_recovery")
        if args.apply_recovery:
            from .handoff_runtime_recovery import apply_recovery
            require(args.go, "explicit_recovery_go_required")
            plan, imported, _ = load_plan(args.plan, args.expected_sha256, project_root)
            require(plan["planning_only"] is False, "planning_only_requires_explicit_new_plan")
            committed_runtime(project_root, plan)
            owned = _literal(args.folder)
            require(owned.is_relative_to(_literal(project_root) / "tmp") and all(
                not owned.is_relative_to(Path(plan[key])) and not Path(plan[key]).is_relative_to(owned)
                for key in ("reader_folder", "matching_folder")), "invalid_recovery_chain_folder")
            owned.mkdir(mode=0o700, parents=True, exist_ok=True)
            with instance_lock(owned):
                receipt = apply_recovery(plan, imported, owned, root=project_root,
                    plan_sha256=args.expected_sha256, go=True)
            result = {"status": "recovery_applied_no_reader_started", "exit_code": 0,
                      "baseline_sha256": receipt["baseline"]["sha256"]}
        elif args.readback:
            require(args.go and args.output is not None and args.limits_file is not None,
                    "explicit_readback_go_output_limits_required")
            plan, imported, _ = load_plan(args.plan, args.expected_sha256, project_root)
            if plan.get("runtime_recovery"):
                plan = {**plan, "_recovery_chain_folder": str(args.folder), "_recovery_plan_sha256": args.expected_sha256}
            value = fresh_readback(plan, imported, args.readback, expected_limits=_read(args.limits_file))
            check_readback(value, plan, args.readback, imported["binding"]["candidate_ids"], utcnow())
            _write(args.output, value)
            result = {"status": "readback_verified", "receipt_sha256": file_hash(args.output), "exit_code": 0}
        else:
            result = run(args.plan, args.expected_sha256, args.folder, project_root=project_root, go=args.go)
    except Exception as error:
        result = {"status": "attention_required", "reason": str(error) if isinstance(error, (ChainError, RecoveryError)) else type(error).__name__, "exit_code": 2}
    import json
    print(json.dumps(result))
    return result.get("exit_code", 0)
