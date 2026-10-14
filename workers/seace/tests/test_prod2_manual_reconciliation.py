"""Synthetic receipts/database only: no network and no paid provider calls."""
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from buenapro_worker.extraction.gemini import estimate_cost
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import manual_reconciliation as subject
from buenapro_worker.prod2.batch_runner import private_write
from buenapro_worker.prod2.luna_fallback import LUNA_FALLBACK_POLICY, validated_failure_receipt
from buenapro_worker.prod2.luna_policy import LUNA_REQUEST_POLICY, LUNA_SELECTED_TRANSPORT_POLICY
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.selected_extractor import SELECTED_TRANSPORT_POLICY
from buenapro_worker.prod2.pdf_preparation import SELECTED_DOCUMENTS_POLICY

PROFILE = "00000000-0000-4000-8000-000000000001"
TOKEN = "00000000-0000-4000-8000-000000000002"
CONV = 1044760


def inputs(*, luna=False):
    documents = [{"document_uuid": PROFILE, "original_sha256": "a" * 64}]
    context = {"document_scope": SELECTED_DOCUMENTS_POLICY, "document_set": documents,
               "selection_context": {"selector_request_hash": "c" * 64}}
    identity = deepcopy(context)
    context.update(document_set_identity=identity, document_set_hash=canonical_hash(identity))
    value = {"analysis_sha": "b" * 64, "original_sha": "a" * 64, "prompt_hash": "d" * 64,
             "model": "gpt-6-luna" if luna else "gemini-2.5-flash-lite", "manifest": [{"start_page": 1, "end_page": 3}],
             "document_context": context, "selected_transport_policy":
             LUNA_SELECTED_TRANSPORT_POLICY if luna else SELECTED_TRANSPORT_POLICY}
    if luna:
        value["provider_request"] = deepcopy(LUNA_REQUEST_POLICY)
    return value


def attempt(value, *, luna=False):
    context = value["document_context"]
    return {"attempt_id": ("openai" if luna else "gemini") + "-selected-" + ("2" if luna else "1") * 32,
        "model": value["model"], "prompt_hash": value["prompt_hash"], "analysis_sha256": value["analysis_sha"],
        "document_scope": SELECTED_DOCUMENTS_POLICY, "document_set_hash": context["document_set_hash"],
        "document_set_identity": deepcopy(context["document_set_identity"]),
        "selection_context": deepcopy(context["selection_context"]),
        "transport_policy": value["selected_transport_policy"], "page_count": 3, "part_count": 1,
        "max_output_tokens": LUNA_REQUEST_POLICY["max_output_tokens"] if luna else 24576,
        "timeout_ms": 300000 if luna else 180000}


def claim_for(value, eid):
    digest = canonical_hash(value)
    return {"status": "claimed", "id": eid, "claim_token": TOKEN,
        "evaluation_key": hash_request({"convocation": CONV, "kind": "gemini_extraction", "request_hash": digest,
            "model": value["model"], "policy_version": subject.EXTRACTION_POLICY, "profile_id": PROFILE})}


def gemini_fixture(folder, *, finish="STOP", error="JSONDecodeError", change=None, eid=425):
    value = inputs()
    common = attempt(value)
    usage = {"provider_usage_known": True, "usage": {"prompt_token_count": 1000,
        "candidates_token_count": 200, "thoughts_token_count": 10}, "input_tokens": 1000, "output_tokens": 210,
        "cost_usd": estimate_cost(value["model"], 1000, 210), "cost_is_estimate": True,
        "output_tokens_include_thinking": True, "finish_reason": finish, "finish_reasons": [finish]}
    started = {**deepcopy(common), "status": "generation_reserved"}
    response = {**deepcopy(common), **deepcopy(usage), "status": "response_received"}
    cleanup = {"attempt_id": common["attempt_id"], "remote_cleanup_pending": 0,
               "remote_cleanup_unknown": False, "remote_files_pending": []}
    outcome = {**deepcopy(common), **deepcopy(usage), **cleanup, "status": "response_parse_failure",
        "error_type": error, "model_call_started": True, "ended_at": "2026-10-08T12:00:00+00:00"}
    records = {"started": started, "response": response, "cleanup": cleanup, "outcome": outcome}
    claim = claim_for(value, eid)
    if change:
        change(records, claim)
    for kind, record in records.items():
        private_write(folder / (common["attempt_id"] + "." + kind + ".private.json"), record)
    private_write(folder / ("gemini_extraction-" + canonical_hash(value) + ".claim.private.json"), claim)
    return value, records, claim


def gemini_plan(folder, **options):
    value, records, claim = gemini_fixture(folder, **options)
    return subject.plan_terminal_failure(folder, CONV, PROFILE, value,
        expected_evaluation_id=claim["id"], attempt_id=records["outcome"]["attempt_id"])


def parent_failure(folder):
    value, records, claim = gemini_fixture(folder, finish="MAX_TOKENS", error="TruncatedResponseError", eid=484)
    out = records["outcome"]
    failure = {"policy_version": LUNA_FALLBACK_POLICY, "kind": "gemini_extraction", "provider": "gemini",
        "status": "failed", "reconciled": True, "failure_reason": "MAX_TOKENS", "convocation_id": CONV,
        "profile_id": PROFILE, "evaluation_id": claim["id"], "claim_token": TOKEN,
        "evaluation_key": claim["evaluation_key"], "request_hash": canonical_hash(value),
        "model": value["model"], "prompt_hash": value["prompt_hash"], "analysis_sha256": value["analysis_sha"],
        "document_set_hash": value["document_context"]["document_set_hash"], "document_scope": SELECTED_DOCUMENTS_POLICY,
        "transport_policy": SELECTED_TRANSPORT_POLICY, "source_attempt_id": out["attempt_id"], "attempt_id": out["attempt_id"],
        "source_document_manifest_hash": canonical_hash(value["manifest"]),
        "attempt_receipt_hash": canonical_hash(out), "started_receipt_hash": canonical_hash(records["started"]),
        "response_receipt_hash": canonical_hash(records["response"]),
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False, "remote_files_pending": [],
        "remote_cleanup_receipt_hash": canonical_hash(records["cleanup"]), "remote_cleanup_receipt_source": "cleanup",
        **{key: out[key] for key in ("provider_usage_known", "model_call_started", "usage", "input_tokens",
            "output_tokens", "cost_usd", "cost_is_estimate", "output_tokens_include_thinking", "finish_reason", "finish_reasons")}}
    assert validated_failure_receipt(claim, failure)
    private_write(folder / ("gemini_extraction-" + failure["request_hash"] + ".failure.private.json"), failure)
    return failure


def luna_fixture(folder, *, change=None):
    parent = parent_failure(folder)
    value = inputs(luna=True)
    common = attempt(value, luna=True)
    outcome = {**common, "status": "transport_failed", "error_type": "SelectedTransportError", "provider": "openai",
        "provider_request": deepcopy(LUNA_REQUEST_POLICY), "reasoning_effort": LUNA_REQUEST_POLICY["reasoning_effort"],
        "request_hash": canonical_hash(value), "ended_at": "2026-10-08T12:00:00+00:00", "model_call_started": False,
        "provider_usage_known": False, "cost_usd": 0, "remote_cleanup_pending": 0,
        "remote_cleanup_unknown": False, "remote_files_pending": []}
    if change:
        change(outcome)
    claim = claim_for(value, 487)
    private_write(folder / (common["attempt_id"] + ".outcome.private.json"), outcome)
    private_write(folder / ("gemini_extraction-" + canonical_hash(value) + ".claim.private.json"), claim)
    return value, outcome, parent


def luna_plan(folder, **options):
    value, outcome, parent = luna_fixture(folder, **options)
    return subject.plan_terminal_failure(folder, CONV, PROFILE, value,
        expected_evaluation_id=487, attempt_id=outcome["attempt_id"], parent_failure=parent)


def live_row(plan, *, status="uncertain"):
    return {"id": plan["evaluation_id"], "id_convocatoria": plan["convocation_id"], "profile_id": plan["profile_id"],
        "kind": plan["kind"], "model": plan["model"], "policy_version": subject.EXTRACTION_POLICY,
        "request_hash": plan["request_hash"], "evaluation_key": plan["evaluation_key"], "claim_token": plan["claim_token"],
        "status": status, "result_json": None, "input_tokens": None, "output_tokens": None, "cost_usd": None}


class Result:
    def __init__(self, rows):
        self.rows = rows
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, rows):
        self.rows = {row["id"]: deepcopy(row) for row in rows}
        self.updates = []
    @contextmanager
    def transaction(self):
        before = deepcopy(self.rows)
        try:
            yield
        except Exception:
            self.rows = before
            raise
    def execute(self, sql, params):
        if sql.startswith("SELECT *"):
            row = self.rows.get(params[0])
            return Result([row] if row else [])
        if sql.startswith("UPDATE"):
            payload, inp, out, cost, eid, token = params
            row = self.rows[eid]
            assert "status='failed'" in sql and "status='done'" not in sql
            assert row["claim_token"] == token and row["status"] in {"running", "uncertain"}
            row.update(status="failed", result_json=json.loads(payload), input_tokens=inp, output_tokens=out, cost_usd=cost)
            self.updates.append(eid)
            return Result([{"id": eid}])
        assert sql.startswith("SELECT id")
        conv, profile, excluded = params
        return Result([{"id": row["id"]} for row in self.rows.values() if row["id_convocatoria"] == conv
            and row["profile_id"] == profile and row["id"] != excluded and row["status"] in {"pending", "running", "uncertain"}])


@pytest.mark.parametrize("error", ["JSONDecodeError", "ValidationError"])
def test_paid_stop_closes_exact_owned_failure_idempotently_and_preserves_siblings(tmp_path, error):
    plan = gemini_plan(tmp_path, error=error)
    row = live_row(plan)
    done = {**row, "id": 600, "status": "done", "result_json": {"preserve": "paid extraction"}}
    pending = {**row, "id": 601, "status": "uncertain"}
    conn = Connection([row, done, pending])
    result = subject.apply_closed_failure(conn, plan)
    assert result["status"] == "failed" and result["does_not_authorize_retry"] is True
    assert result["other_unresolved_evaluation_ids"] == [601]
    assert result["failure"]["input_tokens"] == 1000 and result["failure"]["output_tokens"] == 210
    assert result["failure"]["failure_reason"] == "STOP_parse_failure_body_unavailable"
    assert conn.rows[600] == done and conn.rows[601] == pending
    assert subject.apply_closed_failure(conn, plan)["reused"] is True
    assert conn.updates == [425]
    assert '"selection_context":' not in json.dumps(plan)


@pytest.mark.parametrize("change", [
    lambda r,c: r["response"].update(provider_usage_known=False),
    lambda r,c: r["response"].update(output_tokens=200),
    lambda r,c: r["outcome"].update(cost_usd=0),
    lambda r,c: r["outcome"].update(prompt_hash="e" * 64),
    lambda r,c: r["started"].update(model="gemini-2.5-flash"),
    lambda r,c: r["response"].update(response_text="paid body must be recovered"),
    lambda r,c: r["cleanup"].update(remote_cleanup_pending=1),
    lambda r,c: c.update(id=999),
    lambda r,c: c.update(evaluation_key="f" * 64),
    lambda r,c: r["outcome"].update(model_call_started=False),
])
def test_offline_stop_rejects_unknown_mismatched_or_recoverable_payment(tmp_path, change):
    value, records, claim = gemini_fixture(tmp_path, change=change)
    with pytest.raises(subject.ManualReconciliationError):
        subject.plan_terminal_failure(tmp_path, CONV, PROFILE, value, expected_evaluation_id=425,
            attempt_id=records["outcome"]["attempt_id"])


@pytest.mark.parametrize("finish,error", [("MAX_TOKENS","TruncatedResponseError"), ("STOP","EmptyExtractionError")])
def test_stop_helper_never_relabels_other_failure_as_stop(tmp_path, finish, error):
    with pytest.raises(subject.ManualReconciliationError):
        gemini_plan(tmp_path, finish=finish, error=error)


def test_receipt_changed_after_plan_blocks_before_database_operation(tmp_path):
    plan = gemini_plan(tmp_path)
    path = tmp_path / (plan["source_attempt_id"] + ".outcome.private.json")
    path.write_text(path.read_text() + " ")
    conn = Connection([live_row(plan)])
    with pytest.raises(subject.ManualReconciliationError, match="changed after planning"):
        subject.apply_closed_failure(conn, plan)
    assert conn.updates == []


def test_completed_bundle_blocks_even_if_it_arrives_after_plan(tmp_path):
    plan = gemini_plan(tmp_path)
    private_write(tmp_path / ("gemini_extraction-" + plan["request_hash"] + ".response.private.json"), {"paid": "bundle"})
    with pytest.raises(subject.ManualReconciliationError, match="completed bundle"):
        subject.apply_closed_failure(Connection([live_row(plan)]), plan)


@pytest.mark.parametrize("field,value", [("status","done"),("claim_token",PROFILE),("request_hash","e"*64),
    ("evaluation_key","e"*64),("id_convocatoria",123),("profile_id",TOKEN),("model","wrong model"),
    ("result_json",{"received":"output"}),("cost_usd",99)])
def test_live_owner_or_output_mismatch_cannot_be_overwritten(tmp_path, field, value):
    plan = gemini_plan(tmp_path)
    row = live_row(plan)
    row[field] = value
    conn = Connection([row])
    with pytest.raises(subject.ManualReconciliationError):
        subject.apply_closed_failure(conn, plan)
    assert conn.updates == [] and conn.rows[row["id"]] == row


def test_even_rehashed_plan_cannot_edit_received_cost(tmp_path):
    plan = gemini_plan(tmp_path)
    plan["cost_usd"] = 4
    plan["plan_sha256"] = canonical_hash({k:v for k,v in plan.items() if k != "plan_sha256"})
    with pytest.raises(subject.ManualReconciliationError, match="changes the exact historical received usage"):
        subject.apply_closed_failure(Connection([live_row(plan)]), plan)


def test_unissued_luna_closes_at_zero_only_with_durable_known_parent(tmp_path):
    plan = luna_plan(tmp_path)
    parent = plan["parent_failure"]
    parent_row = {**live_row({**parent, "kind":"gemini_extraction"}, status="failed"),
        "result_json": parent, "input_tokens": parent["input_tokens"], "output_tokens": parent["output_tokens"],
        "cost_usd": parent["cost_usd"]}
    conn = Connection([live_row(plan), parent_row])
    result = subject.apply_closed_failure(conn, plan)
    assert conn.updates == [487] and conn.rows[484] == parent_row
    assert result["failure"]["failure_reason"] == "pre_generation_transport_failed"
    assert result["failure"]["usage_source"] == "pre_generation_no_request"
    assert result["failure"]["input_tokens"] == result["failure"]["output_tokens"] == result["failure"]["cost_usd"] == 0
    assert result["failure"]["provider_usage_known"] is False
    assert subject.apply_closed_failure(conn, plan)["reused"] is True


@pytest.mark.parametrize("change", [lambda o:o.update(model_call_started=True),lambda o:o.update(cost_usd=None),
    lambda o:o.update(provider_usage_known=True),lambda o:o.update(input_tokens=1),
    lambda o:o.update(request_hash="e"*64),lambda o:o.update(status="error_unknown")])
def test_unissued_luna_rejects_potentially_issued_or_unknown_attempts(tmp_path, change):
    with pytest.raises(subject.ManualReconciliationError):
        luna_plan(tmp_path, change=change)


def test_pre_generation_requires_absence_of_start_and_response_receipts(tmp_path):
    value, outcome, parent = luna_fixture(tmp_path)
    private_write(tmp_path / (outcome["attempt_id"] + ".started.private.json"), {"generation":"possibly issued"})
    with pytest.raises(subject.ManualReconciliationError, match="response or completed bundle"):
        subject.plan_terminal_failure(tmp_path, CONV, PROFILE, value, expected_evaluation_id=487,
            attempt_id=outcome["attempt_id"], parent_failure=parent)


def test_pre_generation_parent_not_failed_durably_blocks_target_change(tmp_path):
    plan = luna_plan(tmp_path)
    conn = Connection([live_row(plan)])
    with pytest.raises(subject.ManualReconciliationError, match="Parent failure is not durably reconciled"):
        subject.apply_closed_failure(conn, plan)
    assert conn.updates == []


def test_manual_closed_failure_is_recognized_only_with_owner_and_pinned_receipts(tmp_path):
    from buenapro_worker.prod2.temporary_storage import _payment_uncertain
    plan = gemini_plan(tmp_path)
    claim = json.loads((tmp_path / ("gemini_extraction-" + plan["request_hash"] + ".claim.private.json")).read_text())
    assert _payment_uncertain(tmp_path, list(tmp_path.glob("*.private.json"))) is True
    failure = subject.apply_closed_failure(Connection([live_row(plan)]), plan)["failure"]
    assert subject.validated_manual_failure_receipt(claim, failure) is True
    private_write(tmp_path / ("gemini_extraction-" + plan["request_hash"] + ".failure.private.json"), failure)
    assert _payment_uncertain(tmp_path, list(tmp_path.glob("*.private.json"))) is False
    body = tmp_path / (plan["source_attempt_id"] + ".response.private.json")
    body.write_text(body.read_text() + " ")
    assert subject.validated_manual_failure_receipt(claim, failure) is False
    assert _payment_uncertain(tmp_path, list(tmp_path.glob("*.private.json"))) is True


@pytest.mark.parametrize("field,value", [("status","done"),("reconciled",False),("cost_usd",None),
    ("provider_usage_known",False),("evaluation_id",999),("model","wrong model"),
    ("input_tokens",999),("model_call_started",False)])
def test_cleanup_manual_validator_rejects_rehashed_mutated_or_unknown_failure(tmp_path, field, value):
    plan = gemini_plan(tmp_path)
    failure = subject.apply_closed_failure(Connection([live_row(plan)]), plan)["failure"]
    claim = json.loads((tmp_path / ("gemini_extraction-" + plan["request_hash"] + ".claim.private.json")).read_text())
    failure[field] = value
    planned = {**failure, "reconciled": False}
    failure["plan_sha256"] = canonical_hash({k:v for k,v in planned.items() if k != "plan_sha256"})
    assert subject.validated_manual_failure_receipt(claim, failure) is False


def test_pre_generation_closed_no_call_is_recognized_without_falsifying_usage(tmp_path):
    from buenapro_worker.prod2.temporary_storage import _payment_uncertain
    plan = luna_plan(tmp_path)
    parent = plan["parent_failure"]
    parent_row = {**live_row({**parent, "kind":"gemini_extraction"}, status="failed"),
        "result_json": parent, "input_tokens": parent["input_tokens"], "output_tokens": parent["output_tokens"],
        "cost_usd": parent["cost_usd"]}
    failure = subject.apply_closed_failure(Connection([live_row(plan),parent_row]), plan)["failure"]
    claim = json.loads((tmp_path / ("gemini_extraction-" + plan["request_hash"] + ".claim.private.json")).read_text())
    assert failure["provider_usage_known"] is False
    assert subject.validated_manual_failure_receipt(claim, failure) is True
    private_write(tmp_path / ("gemini_extraction-" + plan["request_hash"] + ".failure.private.json"), failure)
    assert _payment_uncertain(tmp_path, list(tmp_path.glob("*.private.json"))) is False
