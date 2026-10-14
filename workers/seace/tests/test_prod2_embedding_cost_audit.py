"""Embedding fees are tracked independently from OpenAI LLM generations."""
from copy import deepcopy

import pytest

from buenapro_worker.prod2.batch_runner import private_write
from buenapro_worker.prod2.selected_payments import audit_summary


ID = "12345678-1234-4567-890a-123456789abc"


def write(folder, stage, value, identity=ID):
    private_write(folder / ("embedding-" + identity + "." + stage + ".private.json"), value)


def received(*, known=True, complete=True):
    return {"attempt_id": ID, "request_hash": "a" * 64,
        "response": {"version": "prod2_embedding_response_v1", "model": "text-embedding-3-small",
            "request_sent": True, "provider_usage_known": known,
            "input_tokens": 5000 if known else None, "cost_usd": .0001 if known else None,
            "complete": complete, "response_sha256": "b" * 64}}


def test_embedding_started_response_outcome_and_copied_receipts_count_once(tmp_path):
    start = {"attempt_id": ID, "provider": "embedding", "status": "reserved",
        "provider_usage_known": False, "cost_usd": None}
    envelope = received()
    outcome = {**start, "status": "done", "request_sent": True, "provider_usage_known": True,
        "usage_known": True, "input_tokens": 5000, "cost_usd": .0001}
    for folder in (tmp_path, tmp_path / "duplicate-copy"):
        write(folder, "started", start)
        write(folder, "response", envelope)
        write(folder, "outcome", outcome)
    result = audit_summary(tmp_path)
    assert result["attempts"] == result["embedding_attempts"] == result["embedding_calls"] == 1
    assert result["llm1_calls"] == result["openai_calls"] == result["jev_calls"] == 0
    assert result["known_cost_usd"] == result["embedding_cost_usd"] == pytest.approx(.0001)
    assert result["embedding_input_tokens"] == 5000
    assert result["usage_unknown_attempts"] == result["embedding_usage_unknown_attempts"] == 0


def test_pre_send_failure_is_known_zero_with_no_billed_embedding_call(tmp_path):
    write(tmp_path, "started", {"attempt_id": ID, "status": "reserved"})
    write(tmp_path, "outcome", {"attempt_id": ID, "request_sent": False,
        "status": "failed_pre_send", "provider_usage_known": True,
        "input_tokens": 0, "cost_usd": 0})
    result = audit_summary(tmp_path)
    assert result["attempts"] == result["embedding_attempts"] == result["embedding_pre_send_failures"] == 1
    assert result["embedding_calls"] == result["known_cost_usd"] == result["usage_unknown_attempts"] == 0
    assert result["embedding_input_tokens"] == result["embedding_cost_usd"] == 0


@pytest.mark.parametrize("terminal", ["started_only", "timeout_after_post", "unknown_response"])
def test_embedding_uncertain_cost_is_visible_and_not_assumed_free(tmp_path, terminal):
    write(tmp_path, "started", {"attempt_id": ID, "status": "reserved", "provider_usage_known": False})
    if terminal == "timeout_after_post":
        write(tmp_path, "outcome", {"attempt_id": ID, "request_sent": True, "status": "uncertain"})
    if terminal == "unknown_response": write(tmp_path, "response", received(known=False))
    result = audit_summary(tmp_path)
    assert result["embedding_calls"] == result["embedding_usage_unknown_attempts"] == result["usage_unknown_attempts"] == 1
    assert result["embedding_unknown_dispatch_attempts"] == (terminal == "started_only")
    assert result["embedding_cost_usd"] == result["known_cost_usd"] == 0
    assert result["openai_calls"] == result["llm1_calls"] == 0


def test_known_paid_partial_response_is_counted_despite_local_outcome_failure(tmp_path):
    write(tmp_path, "response", received(complete=False))
    write(tmp_path, "outcome", {"attempt_id": ID, "request_sent": True,
        "status": "uncertain", "provider_usage_known": False, "cost_usd": None})
    result = audit_summary(tmp_path)
    assert result["embedding_input_tokens"] == 5000
    assert result["embedding_cost_usd"] == pytest.approx(.0001)
    assert result["usage_unknown_attempts"] == 0


def test_nonattempt_claim_stage_and_summary_do_not_duplicate_fees(tmp_path):
    write(tmp_path, "response", received())
    write(tmp_path, "claim", {"attempt_id": ID, "cost_usd": 999})
    private_write(tmp_path / "embedding-stage.private.json", {"known_cost_usd": 999})
    private_write(tmp_path / "embedding-result.response.private.json", {"known_cost_usd": 999})
    result = audit_summary(tmp_path)
    assert result["embedding_attempts"] == 1 and result["known_cost_usd"] == pytest.approx(.0001)


def test_mixed_jev_gemini_luna_and_embeddings_have_independent_call_counts(tmp_path):
    write(tmp_path, "response", received())
    private_write(tmp_path / "jev-attempt-test.outcome.private.json", {"attempt_id": "jev-attempt-test",
        "provider": "jev", "usage_known": True, "input_tokens": 1000, "cost_usd": .000042})
    for prefix, cost in (("gemini-selected-", .01), ("openai-selected-", .05)):
        private_write(tmp_path / (prefix + "test.response.private.json"), {"attempt_id": prefix + "test",
            "model_call_started": True, "provider_usage_known": True, "cost_usd": cost})
    result = audit_summary(tmp_path)
    assert result["attempts"] == 4 and result["llm1_calls"] == 2
    assert result["jev_calls"] == result["openai_calls"] == result["embedding_calls"] == 1
    assert result["known_cost_usd"] == pytest.approx(.060142)
    assert result["embedding_cost_usd"] == pytest.approx(.0001)


def test_conflicting_duplicate_embedding_receipts_keep_financial_review(tmp_path):
    original = received()
    changed = deepcopy(original)
    changed["response"]["cost_usd"] = .01
    write(tmp_path, "response", original)
    write(tmp_path / "bad-copy", "response", changed)
    result = audit_summary(tmp_path)
    assert result["embedding_attempts"] == result["embedding_receipt_conflicts"] == 1
    assert result["embedding_usage_unknown_attempts"] == 1
    assert result["known_cost_usd"] == 0


@pytest.mark.parametrize("cost", [None, float("inf"), -1, True])
def test_invalid_cost_never_counts_as_confirmed_free(tmp_path, cost):
    envelope = received()
    # private_write rejects nonfinite JSON itself; a corrupted private receipt
    # is tested by explicit JSON encoding rather than weakening that writer.
    if cost == float("inf"):
        import json
        envelope["response"]["cost_usd"] = cost
        (tmp_path / ("embedding-" + ID + ".response.private.json")).write_text(json.dumps(envelope))
    else:
        envelope["response"]["cost_usd"] = cost
        write(tmp_path, "response", envelope)
    result = audit_summary(tmp_path)
    assert result["embedding_usage_unknown_attempts"] == 1 and result["embedding_cost_usd"] == 0
