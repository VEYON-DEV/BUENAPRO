"""Only terminal source/queue completion permits the next frozen batch."""
import copy
import json

import pytest

from buenapro_worker.prod2.keyword_chain import assess_final_report

RUN = "prod2-ia-datos-completo-test"


def report():
    return {"run_id": RUN, "pipeline": "prod2_selected_llm1_v1", "terminal_stage": "llm1",
        "source_coverage_complete": True, "queries_total": 2, "queries_complete": 2,
        "coverage": [{"coverage_complete": True, "errors": []},
                     {"coverage_complete": True, "errors": []}],
        "scope_proofs": [], "redis": {"queued": 0, "pending": 0, "payloads": 0, "seen": 3},
        "processed": 3, "accepted": 1, "llm1_complete": 1, "requires_review": 0,
        "costs": {"usage_unknown_attempts": 0, "attempts": 2, "jev_calls": 1,
                  "llm1_calls": 1, "known_cost_usd": 0.001},
        "records": [{"status": "llm1_complete"}, {"status": "rejected_below_60"},
                    {"status": "rejected_below_60"}]}


def gate(value, code=0):
    return assess_final_report(value, RUN, code)


def test_complete_zero_exit_advances_with_compact_immutable_summary():
    source = report()
    original = copy.deepcopy(source)
    source["records"][0]["raw_extraction_json"] = {"sensitive": "not in summary"}
    original = copy.deepcopy(source)
    result = gate(source)
    assert result["advance"] is True and result["state"] == "complete"
    assert result["reason"] == "source_and_queue_complete"
    assert result["summary"]["costs"]["known_cost_usd"] == 0.001
    assert result["summary"]["outcome_status_counts"] == {
        "llm1_complete": 1, "rejected_below_60": 2}
    assert "records" not in result["summary"] and "coverage" not in result["summary"]
    assert "sensitive" not in json.dumps(result)
    assert source == original


def test_exit_two_terminal_review_advances_without_authorizing_retry():
    source = report()
    source["llm1_complete"] = 0
    source["requires_review"] = 1
    source["records"][0]["status"] = "needs_review"
    source["costs"]["usage_unknown_attempts"] = 1
    result = gate(source, 2)
    assert result["advance"] is True and result["state"] == "complete_with_review"
    assert result["summary"]["costs"]["usage_unknown_attempts"] == 1
    assert result["summary"]["llm1_complete"] == 0
    assert "retry" not in result


def test_exit_two_alone_remains_review_and_does_not_invent_zero_usage():
    result = gate(report(), 2)
    assert result["state"] == "complete_with_review" and result["advance"] is True


@pytest.mark.parametrize("exit_code", [0, 2])
def test_current_embedding_dispatch_stop_blocks_next_batch_even_after_drain(exit_code):
    source = report()
    source["embedding_dispatch_stop_requested"] = True
    result = gate(source, exit_code)
    assert result["advance"] is False and result["reason"] == "embedding_provider_dispatch_stopped"


def test_explicit_no_new_embedding_failure_preserves_historical_review_advancement():
    source = report()
    source["embedding_dispatch_stop_requested"] = False
    source["costs"]["usage_unknown_attempts"] = 1
    assert gate(source, 2)["state"] == "complete_with_review"


@pytest.mark.parametrize("value", [None, 0, 1, "false", {}])
def test_malformed_embedding_dispatch_stop_never_advances(value):
    source = report()
    source["embedding_dispatch_stop_requested"] = value
    assert gate(source, 2)["reason"] == "invalid_embedding_dispatch_stop"


@pytest.mark.parametrize("change", ["accepted_not_done", "human_review", "uncertain_usage", "documents_missing"])
def test_review_is_not_hidden_by_exit_zero(change):
    source = report()
    if change == "accepted_not_done": source["llm1_complete"] = 0
    if change == "human_review": source["requires_review"] = 1
    if change == "uncertain_usage": source["costs"]["usage_unknown_attempts"] = 1
    if change == "documents_missing": source["records"][0]["status"] = "documents_missing"
    result = gate(source)
    assert result["state"] == "complete_with_review" and result["advance"] is True


@pytest.mark.parametrize("code", [1, -1, 3, None, True, False, 0.0, "0"])
def test_process_failure_never_advances(code):
    result = gate(report(), code)
    assert result["advance"] is False and result["state"] == "attention_required"
    assert result["reason"] == "invalid_exit_code"


@pytest.mark.parametrize("key,value", [
    ("run_id", "other-run"), ("pipeline", "other-pipeline"), ("terminal_stage", "llm2"),
    ("source_coverage_complete", False), ("source_coverage_complete", 1),
    ("queries_total", 0), ("queries_total", True), ("queries_total", "2"),
    ("queries_complete", True), ("queries_complete", 1),
    ("coverage", []), ("coverage", {}), ("scope_proofs", {}),
    ("accepted", True), ("accepted", -1), ("llm1_complete", True),
    ("llm1_complete", 2), ("processed", False), ("processed", 0),
    ("requires_review", True), ("requires_review", 4),
])
def test_malformed_identity_source_and_counts_do_not_advance(key, value):
    source = report()
    source[key] = value
    result = gate(source, 2)
    assert result["advance"] is False and result["state"] == "attention_required"


@pytest.mark.parametrize("key", ["queued", "pending", "payloads"])
def test_undrained_shared_queue_blocks_next_group(key):
    source = report()
    source["redis"][key] = 1
    result = gate(source, 2)
    assert result["advance"] is False and result["reason"] == "queue_incomplete"


@pytest.mark.parametrize("key", ["queued", "pending", "payloads", "seen"])
@pytest.mark.parametrize("value", [False, None, -1, "0"])
def test_queue_counters_are_exact_nonnegative_integers(key, value):
    source = report()
    source["redis"][key] = value
    result = gate(source)
    assert result["advance"] is False and result["reason"] == "invalid_queue_counts"


@pytest.mark.parametrize("location", ["coverage", "scope_proofs"])
@pytest.mark.parametrize("code", ["source_403", "source_429", "campaign_completed_cache_invalid",
    "completed_query_cache_missing_or_invalid_restore_or_new_epoch", "cached_page_binding_mismatch",
    "cache_or_callback_error", "complete_cache_binding_mismatch", "completed_cache_malformed"])
def test_real_access_and_cache_errors_override_claimed_completion(location, code):
    source = report()
    bad = {"coverage_complete": True, "errors": [{"code": code}]}
    if location == "coverage": source[location][0] = bad
    else: source[location] = [bad]
    result = gate(source, 2)
    assert result["advance"] is False
    assert result["reason"] == ("source_access_blocked" if code.startswith("source_") else "cache_integrity_failure")


@pytest.mark.parametrize("value", [False, 1, None])
def test_every_query_must_be_explicitly_complete(value):
    source = report()
    source["coverage"][1]["coverage_complete"] = value
    assert gate(source)["reason"] == "source_incomplete"


@pytest.mark.parametrize("bad", [None, [], {"errors": {}}, {"errors": [None]}, {"errors": [{"code": None}]}])
def test_bad_coverage_evidence_blocks_without_throwing(bad):
    source = report()
    source["coverage"][0] = bad
    assert gate(source)["advance"] is False


@pytest.mark.parametrize("costs", [None, {}, {"usage_unknown_attempts": True},
    {"usage_unknown_attempts": -1}, {"usage_unknown_attempts": 0, "known_cost_usd": float("nan")},
    {"usage_unknown_attempts": 0, "known_cost_usd": float("inf")},
    {"usage_unknown_attempts": 0, "jev_calls": False}])
def test_uncertain_or_corrupt_usage_cannot_be_assumed_zero(costs):
    source = report()
    source["costs"] = costs
    result = gate(source)
    assert result["advance"] is False and result["reason"] == "invalid_cost_summary"


@pytest.mark.parametrize("records", [None, {}, [None], [{"status": []}], [{"status": ""}], []])
def test_malformed_outcomes_do_not_raise_or_advance(records):
    source = report()
    source["records"] = records
    assert gate(source)["advance"] is False


@pytest.mark.parametrize("value", [None, [], "not a report"])
def test_non_object_report_blocks_without_exception(value):
    assert gate(value)["state"] == "attention_required"


def test_summary_whitelist_omits_arbitrary_extra_cost_payload_and_unknown_status_text():
    source = report()
    source["costs"]["provider_raw_response"] = {"private": "secret"}
    source["records"][0]["status"] = "unexpected private text"
    result = gate(source)
    assert result["state"] == "complete_with_review"
    assert "private" not in json.dumps(result)


def test_reading_metadata_may_be_omitted_when_aggregate_contract_is_complete():
    source = report()
    source.pop("records")
    source.pop("processed")
    source.pop("requires_review")
    assert gate(source)["state"] == "complete"
