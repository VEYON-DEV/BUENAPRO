"""Pure final-report gate for sequential, frozen PROD2 keyword batches.

Review outcomes can finish a batch without completing every document reading.
This gate authorizes only advancement to another batch, never a payment retry,
source refresh or mutation of historical evidence. No IO occurs in this module.
"""
from __future__ import annotations

from collections import Counter
import math

PIPELINE = "prod2_selected_llm1_v1"
_COUNT_FIELDS = ("processed", "accepted", "llm1_complete", "accepted_unread", "requires_review",
                 "queries_total", "queries_complete")
_COST_COUNT_FIELDS = (
    "attempts", "jev_calls", "llm1_calls", "openai_calls", "embedding_attempts",
    "embedding_calls", "embedding_unknown_dispatch_attempts", "embedding_pre_send_failures",
    "embedding_usage_unknown_attempts", "embedding_receipt_conflicts",
    "embedding_input_tokens", "usage_unknown_attempts",
)
_COST_NUMBER_FIELDS = ("embedding_cost_usd", "known_cost_usd", "elapsed_seconds")


def _count(value):
    return type(value) is int and value >= 0


def _number(value):
    return _count(value) or (type(value) is float and math.isfinite(value) and value >= 0)


def _summary(report):
    """Whitelist aggregates; never copy raw records, arbitrary text or payloads."""
    if not isinstance(report, dict):
        return {}
    result = {key: report[key] for key in _COUNT_FIELDS if _count(report.get(key))}
    if isinstance(report.get("run_id"), str):
        result["run_id"] = report["run_id"]
    redis = report.get("redis")
    if isinstance(redis, dict):
        result["redis"] = {key: redis[key] for key in ("queued", "pending", "payloads", "seen")
                           if _count(redis.get(key))}
    costs = report.get("costs")
    if isinstance(costs, dict):
        result["costs"] = {key: costs[key] for key in _COST_COUNT_FIELDS if _count(costs.get(key))}
        result["costs"].update({key: costs[key] for key in _COST_NUMBER_FIELDS if _number(costs.get(key))})
    records = report.get("records")
    if isinstance(records, list):
        # Status values are a bounded machine vocabulary; do not return arbitrary
        # strings from a malformed report as potentially sensitive summary text.
        known = {"llm1_complete", "accepted_unread", "rejected_below_60", "needs_review",
                 "selection_needs_review", "selection_complete", "documents_missing"}
        result["outcome_status_counts"] = dict(Counter(row["status"] for row in records
            if isinstance(row, dict) and isinstance(row.get("status"), str) and row["status"] in known))
    return result


def _decision(summary, state, reason):
    return {"advance": state != "attention_required", "state": state,
            "reason": reason, "summary": summary}


def _error_reason(nodes):
    """Stop access and cache failures even if a malformed report claims success."""
    for node in nodes:
        if not isinstance(node, dict):
            return "invalid_coverage_evidence"
        errors = node.get("errors", [])
        if not isinstance(errors, list):
            return "invalid_coverage_evidence"
        for error in errors:
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                return "invalid_coverage_evidence"
            code = error["code"].lower()
            if code in {"source_403", "source_429"}:
                return "source_access_blocked"
            # A cache code in an errors list is never evidence of a valid
            # completed pass. Preserve unknown future cache failures as blockers.
            if "cache" in code:
                return "cache_integrity_failure"
    return None


def assess_final_report(report, run_id, exit_code):
    """Classify advancement using only a supplied final report and process exit.

    Callers still verify the report belongs to the completed process and preserve
    its immutable receipts. An attention_required result does not authorize any
    retry. In particular, uncertain provider usage remains visible in summaries.
    """
    summary = _summary(report)
    def attention(reason):
        return _decision(summary, "attention_required", reason)

    if type(exit_code) is not int or exit_code not in {0, 2}:
        return attention("invalid_exit_code")
    if not isinstance(report, dict) or not isinstance(run_id, str) or not run_id:
        return attention("invalid_report")
    if report.get("run_id") != run_id:
        return attention("report_identity_mismatch")
    if report.get("pipeline") != PIPELINE or report.get("terminal_stage") != "llm1":
        return attention("report_contract_mismatch")
    if "embedding_dispatch_stop_requested" in report:
        if type(report["embedding_dispatch_stop_requested"]) is not bool:
            return attention("invalid_embedding_dispatch_stop")
        if report["embedding_dispatch_stop_requested"]:
            return attention("embedding_provider_dispatch_stopped")
    coverage, proofs = report.get("coverage"), report.get("scope_proofs", [])
    if not isinstance(coverage, list) or not isinstance(proofs, list):
        return attention("invalid_coverage_evidence")
    if reason := _error_reason(coverage + proofs):
        return attention(reason)
    if report.get("source_coverage_complete") is not True:
        return attention("source_incomplete")
    total, complete = report.get("queries_total"), report.get("queries_complete")
    if not _count(total) or total == 0 or not _count(complete):
        return attention("invalid_query_counts")
    if complete != total or len(coverage) != total or any(
            row.get("coverage_complete") is not True for row in coverage):
        return attention("source_incomplete")
    redis = report.get("redis")
    if not isinstance(redis, dict) or any(not _count(redis.get(key))
            for key in ("queued", "pending", "payloads", "seen")):
        return attention("invalid_queue_counts")
    if any(redis[key] != 0 for key in ("queued", "pending", "payloads")):
        return attention("queue_incomplete")
    accepted, done = report.get("accepted"), report.get("llm1_complete")
    # accepted_unread (below the reading threshold) is absent from reports written before it existed.
    unread = report.get("accepted_unread", 0)
    if not _count(accepted) or not _count(done) or not _count(unread) or done + unread > accepted:
        return attention("invalid_reading_counts")
    for key in ("processed", "requires_review"):
        if key in report and not _count(report[key]):
            return attention("invalid_reading_counts")
    if "processed" in report and (accepted > report["processed"]
            or report.get("requires_review", 0) > report["processed"]):
        return attention("invalid_reading_counts")
    costs = report.get("costs")
    if not isinstance(costs, dict) or not _count(costs.get("usage_unknown_attempts")):
        return attention("invalid_cost_summary")
    if any(key in costs and not _count(costs[key]) for key in _COST_COUNT_FIELDS):
        return attention("invalid_cost_summary")
    if any(key in costs and not _number(costs[key]) for key in _COST_NUMBER_FIELDS):
        return attention("invalid_cost_summary")
    records = report.get("records", [])
    if not isinstance(records, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("status"), str) or not row["status"] for row in records):
        return attention("invalid_outcome_records")
    if "records" in report and "processed" in report and len(records) != report["processed"]:
        return attention("invalid_outcome_records")
    review = (exit_code == 2 or accepted != done + unread or report.get("requires_review", 0) > 0
        or costs["usage_unknown_attempts"] > 0 or any(
            row["status"] not in {"llm1_complete", "accepted_unread", "rejected_below_60"} for row in records))
    return _decision(summary, "complete_with_review" if review else "complete",
                     "terminal_with_review_preserved" if review else "source_and_queue_complete")
