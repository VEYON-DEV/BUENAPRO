"""Jev gating/ownership tests with synthetic metadata and no external I/O."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from buenapro_worker.prod2 import accepted_repository as subject
from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION, jev_request_hash
from buenapro_worker.prod2.discovery import validate_capture

NOW = datetime(2026, 10, 5, 17, tzinfo=timezone.utc)
PROFILE = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3"
TOKEN = "61c03018-1c05-47fe-8e7c-99caaae4d5de"
OPP = "bcbc122b-c585-48fc-b345-07221a994975"
CONV = 9000000001
COMMERCIAL = [{"descripcion": "Soporte software", "lineas": ["software"]}]
DIGEST = jev_request_hash("SOFTWARE", commercial_profiles=COMMERCIAL, model=JEV_MODEL)


def connection(*responses):
    conn, cursor = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.side_effect = responses
    cursor.fetchall.return_value = []
    return conn, cursor


def result(affinity=70, *, context=False, **overrides):
    value = {"affinity_index": affinity, "model": JEV_MODEL, "policy_version": JEV_POLICY_VERSION,
        "cache_key": "prod2:jev:" + DIGEST, "input_tokens": 100, "output_tokens": 10, "cost_usd": 0.0000042,
        "request_hash": DIGEST, **overrides}
    if context:
        value["request_context"] = {"description": "SOFTWARE", "commercial_profiles": COMMERCIAL}
    return value


def cached(status="done", **overrides):
    return {"cache_key": DIGEST, "convocation_id": CONV, "model": JEV_MODEL,
        "policy_version": JEV_POLICY_VERSION, "status": status, "claim_token": TOKEN,
        "result_json": result(), "expired": False, **overrides}


def query():
    return {"keyword": "SOFTWARE", "object_type": "service", "year": 2025,
        "published_from": "2026-04-01", "published_to": "2026-10-05", "seace_version": "3"}


def row():
    raw = {"procedure_id": 8000000001, "convocation_id": CONV, "nomenclature": "SYNTHETIC-1",
        "buyer_name": "SYNTHETIC BUYER", "published_at": "05/10/2026 10:00", "description": "SOFTWARE",
        "object_label": "Servicio", "reference_amount": "100.00", "currency": "PEN", "seace_version": "3"}
    return validate_capture({"query": query(), "observed_at": NOW.isoformat(), "rows": [raw],
        "page": {"number": 1, "page_size": 1, "total_results": 1, "total_pages": 1}}, now=NOW).rows[0]


def claim(conn, **overrides):
    return subject.claim_jev(conn, **{"convocation_id": CONV, "request_hash": DIGEST,
        "model": JEV_MODEL, "policy_version": JEV_POLICY_VERSION, **overrides})


def promote(conn, **overrides):
    return subject.promote_accepted(conn, **{"run_id": "test-selection", "row": row(), "query_manifest": query(),
        "observed_at": NOW, "jev_result": result(context=True, profile_id=PROFILE), **overrides})


def test_new_claim_has_no_discovery_fk_or_canonical_mutation():
    conn, cursor = connection(None)
    response = claim(conn)
    assert response["state"] == "claimed" and response["claim_token"]
    sqls = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO prod2_jev_decision_cache" in sql for sql in sqls)
    assert not any("INSERT INTO opportunities" in sql or "INSERT INTO prod2_discoveries" in sql for sql in sqls)


def test_same_semantic_request_reuses_result_across_convocations():
    conn, cursor = connection(cached())
    response = claim(conn, convocation_id=CONV+1)
    assert response["state"] == "cached" and response["result"]["affinity_index"] == 70
    assert not any(call.args[0].startswith("INSERT") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("status,expired", [("running", False), ("running", True), ("uncertain", True)])
def test_live_or_unknown_billing_never_yields_a_new_claim(status, expired):
    conn, cursor = connection(cached(status, expired=expired))
    assert claim(conn)["state"] == "blocked"
    assert not any("INSERT" in call.args[0] for call in cursor.execute.call_args_list)
    if status == "running" and expired:
        assert any("status='uncertain'" in call.args[0] for call in cursor.execute.call_args_list)


def test_legacy_done_jev_is_validated_and_reused_without_billing_claim():
    conn, cursor = connection(None)
    cursor.fetchall.return_value = [{"id": 9, "status": "done", "completed_at": NOW, "result_json": result()}]
    response = claim(conn)
    assert response["state"] == "cached" and response["reused_evaluation_id"] == 9
    assert any("kind='jev_affinity'" in call.args[0] and "request_hash=%s" in call.args[0]
               for call in cursor.execute.call_args_list)
    assert not any("'running'" in call.args[0] for call in cursor.execute.call_args_list if call.args[0].startswith("INSERT"))


@pytest.mark.parametrize("status", ["pending", "running", "uncertain", "failed"])
def test_legacy_noncompleted_reservation_remains_blocked(status):
    conn, cursor = connection(None)
    cursor.fetchall.return_value = [{"id": 9, "status": status}]
    assert claim(conn)["state"] == "blocked"
    assert "'uncertain'" in cursor.execute.call_args.args[0]


@pytest.mark.parametrize("change", [{"model": "other"}, {"policy_version": "other"},
    {"affinity_index": 101}, {"affinity_index": float("nan")}, {"cache_key": "prod2:jev:" + "a"*64}])
def test_invalid_legacy_done_response_does_not_turn_into_another_payment(change):
    conn, cursor = connection(None)
    cursor.fetchall.return_value = [{"id": 9, "status": "done", "completed_at": NOW, "result_json": result(**change)}]
    with pytest.raises(ValueError):
        claim(conn)
    assert not any(call.args[0].startswith("INSERT") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("status", ["running", "uncertain"])
def test_original_owner_can_complete_late_response(status):
    conn, cursor = connection(cached(status))
    assert subject.complete_jev(conn, request_hash=DIGEST, claim_token=TOKEN, result=result())["state"] == "done"
    assert "status='done'" in cursor.execute.call_args.args[0]


def test_completion_replay_is_immutable_and_wrong_owner_is_rejected():
    conn, cursor = connection(cached())
    assert subject.complete_jev(conn, request_hash=DIGEST, claim_token=TOKEN, result=result())["cached"] is True
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)
    conn, _ = connection(cached())
    with pytest.raises(subject.AcceptedPersistenceError, match="immutable"):
        subject.complete_jev(conn, request_hash=DIGEST, claim_token=TOKEN, result=result(80))
    conn, _ = connection(cached("running", claim_token=PROFILE))
    with pytest.raises(subject.AcceptedPersistenceError, match="not owned"):
        subject.complete_jev(conn, request_hash=DIGEST, claim_token=TOKEN, result=result())


@pytest.mark.parametrize("score", [0, 59, 59.999])
def test_below_threshold_never_creates_canonical_metadata_or_membership(monkeypatch, score):
    identity = MagicMock()
    monkeypatch.setattr(subject, "_identity", identity)
    conn, cursor = connection(cached(result_json=result(score)))
    with pytest.raises(subject.AcceptedPersistenceError, match="below"):
        promote(conn, jev_result=result(score, context=True))
    identity.assert_not_called()
    assert not any(call.args[0].startswith("INSERT") for call in cursor.execute.call_args_list)


def test_threshold_60_promotes_idempotently_without_fake_page_coverage(monkeypatch):
    identity = MagicMock(return_value=(OPP, False))
    monkeypatch.setattr(subject, "_identity", identity)
    promoted = result(60, context=True, profile_id=PROFILE)
    for _ in range(2):
        conn, cursor = connection(cached(result_json=result(60)), None, {"id": 11, "status": "done", "result_json": promoted})
        response = promote(conn, jev_result=promoted)
        assert response["opportunity_id"] == OPP and response["evaluation_id"] == 11 and response["created"] is False
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        assert not any("prod2_discovery_pages" in sql or "prod2_discovery_runs" in sql or "prod2_pilot_batches" in sql for sql in statements)
        mirror = next(call for call in cursor.execute.call_args_list if "INSERT INTO prod2_evaluations" in call.args[0])
        assert mirror.args[1][-3:] == (0, 0, 0)


def test_row_description_must_bind_the_exact_semantic_request_before_identity_write(monkeypatch):
    identity = MagicMock()
    monkeypatch.setattr(subject, "_identity", identity)
    changed = row()
    changed["description"] = "HARDWARE"
    conn, cursor = connection(cached())
    with pytest.raises(subject.AcceptedPersistenceError, match="row description"):
        promote(conn, row=changed)
    identity.assert_not_called()
    assert not any(call.args[0].startswith("INSERT") for call in cursor.execute.call_args_list)


def test_transient_context_must_recompute_to_the_durable_hash():
    changed = result(context=True)
    changed["request_context"]["description"] = "HARDWARE"
    conn, _ = connection(cached())
    with pytest.raises(subject.AcceptedPersistenceError, match="hash to"):
        promote(conn, jev_result=changed)


def test_rejected_membership_contains_no_canonical_or_document_rows():
    rejected = result(20)
    conn, cursor = connection(cached(result_json=rejected), None)
    response = subject.record_run_member(conn, run_id="test-selection", convocation_id=CONV,
        request_hash=DIGEST, accepted=False, result=rejected)
    assert response["accepted"] is False
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert not any("prod2_discoveries" in sql or "prod2_details" in sql or "prod2_documents" in sql for sql in statements)


def test_run_membership_cannot_change_its_accepted_decision_on_replay():
    conn, _ = connection(cached(), {"request_hash": DIGEST, "accepted": False, "result_json": result(20)})
    with pytest.raises(subject.AcceptedPersistenceError, match="immutable"):
        subject.record_run_member(conn, run_id="test-selection", convocation_id=CONV,
            request_hash=DIGEST, accepted=True, result=result())


@pytest.mark.parametrize("accepted,score", [(True, 20), (False, 60), (1, 70)])
def test_caller_cannot_bypass_threshold_via_membership_flag(accepted, score):
    conn, _ = connection(cached(result_json=result(score)))
    with pytest.raises(subject.AcceptedPersistenceError, match="fixed Jev threshold"):
        subject.record_run_member(conn, run_id="test-selection", convocation_id=CONV,
            request_hash=DIGEST, accepted=accepted, result=result(score))
