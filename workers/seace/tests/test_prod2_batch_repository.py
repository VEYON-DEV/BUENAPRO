"""Offline orchestration contracts; synthetic metadata, no DB/network/model calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock

import pytest

from buenapro_worker.prod2 import batch_repository as subject
from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION, jev_request_hash
from buenapro_worker.prod2.discovery import CaptureError, TruncatedQueryError, _hash, validate_capture
from buenapro_worker.prod2.participation import decide

NOW = datetime(2026, 10, 5, 17, tzinfo=timezone.utc)
PROFILE = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3"
TOKEN = "61c03018-1c05-47fe-8e7c-99caaae4d5de"
SHA = "a" * 64
CONV = 9000000001


def manifest(**overrides):
    return {"published_from": "2026-04-06", "published_to": "2026-10-05",
            "procedure_years": [2026, 2025], "keywords": ["SOFTWARE", "IA"],
            "object_types": ["good", "service"], "seace_version": "3", "profile_id": PROFILE,
            "commercial_profiles": [{"descripcion": "Soporte de software", "lineas": ["software"]}],
            "jev_model": JEV_MODEL, "jev_policy_version": JEV_POLICY_VERSION, **overrides}


def run(phase="discovery", **overrides):
    return {"run_id": "scratch-batch", "phase": phase, "status": "active",
            "coverage_status": "pending" if phase == "discovery" else "complete",
            "frozen_manifest": subject.plan_queries(manifest())[0], **overrides}


def task(kind="discovery", **overrides):
    return {"id": 7, "run_id": "scratch-batch", "kind": kind, "status": "running",
            "owner_token": TOKEN, "owner_live": True, "attempts": 1, "snapshot_restarts": 0,
            "task_key": SHA, "discovery_run_id": "scratch-pass", "id_convocatoria": CONV,
            "query_manifest": subject.plan_queries(manifest())[1][0],
            "checkpoint_json": {"next_page": 1}, "error_code": None, **overrides}


def connection(*responses):
    conn, cursor = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.side_effect = responses
    return conn, cursor


def owned(*responses, phase="discovery", task_overrides=None):
    return connection({"run_id": "scratch-batch"}, run(phase),
                      task("discovery" if phase == "discovery" else "enrich", **(task_overrides or {})), *responses)


def capture(query=None, **page):
    return {"query": query or task()["query_manifest"], "observed_at": NOW.isoformat(),
            "page": {"number": 1, "page_size": 15, "total_pages": 1, "total_results": 0, **page}, "rows": []}


def zero_proof(**overrides):
    return {"year": 2025, "object_type": "service", "keyword": "", "seace_version": "3",
            "published_from": "2026-04-06", "published_to": "2026-10-05",
            "observed_at": NOW.isoformat(), "total_results": 0, "counter": [0, 0, 0, 1, 1],
            "rows_count": 0, **overrides}


def ev(config=None, **overrides):
    config = config or run()["frozen_manifest"]
    return {"id": 3, "id_convocatoria": CONV, "kind": "jev_affinity", "status": "done",
            "profile_id": PROFILE, "model": JEV_MODEL, "policy_version": JEV_POLICY_VERSION,
            "request_hash": jev_request_hash("SOFTWARE", commercial_profiles=config["commercial_profiles"], model=JEV_MODEL),
            **overrides}


def member():
    return {"row_manifest": {"description": "SOFTWARE"}, "description": "SOFTWARE"}


def detail():
    return {"procedure_id": 8000000001, "convocatoria_id": CONV, "selection_type": "Concurso público",
            "item_states": ["Convocado"], "schedule": [
                {"stage": "Registro de participantes", "starts_at_text": "05/10/2026 08:00",
                 "ends_at_text": "06/10/2026 17:00"},
                {"stage": "Presentación de ofertas", "starts_at_text": "07/10/2026 08:00",
                 "ends_at_text": "07/10/2026 17:00"}]}


def stored_detail():
    return {"detail_hash": SHA, "detail_fetched_at": NOW - timedelta(minutes=1), "raw_detail": detail()}


def test_native_year_is_independent_of_publication_year_and_calendar_partitions():
    config, queries = subject.plan_queries(manifest(published_from="2025-12-30", published_to="2026-01-02"))
    assert len(queries) == 16
    assert {q["year"] for q in queries} == {2025, 2026}
    assert {(q["published_from"], q["published_to"]) for q in queries} == {
        ("2025-12-30", "2025-12-31"), ("2026-01-01", "2026-01-02")}
    assert config["pipeline"] == "metadata_jev_schedule_only"
    assert validate_capture(capture({**task()["query_manifest"], "year": 2025}), now=NOW).query["year"] == 2025


def test_cross_year_capture_and_publication_outside_window_are_distinct():
    query = {**task()["query_manifest"], "year": 2025, "published_from": "2025-12-01"}
    value = capture(query, total_results=1)
    value["rows"] = [{"procedure_id": 8000000001, "convocation_id": CONV, "buyer_name": "SYNTHETIC BUYER",
                     "nomenclature": "SYNTHETIC-1", "published_at": "05/10/2026 10:00", "description": "SOFTWARE",
                     "object_label": "Bien", "reference_amount": "100.00", "currency": "PEN", "seace_version": "3"}]
    assert validate_capture(value, now=NOW).rows[0]["convocation_id"] == CONV
    value["rows"][0]["published_at"] = "01/11/2025 10:00"
    with pytest.raises(CaptureError, match="outside query"):
        validate_capture(value, now=NOW)


@pytest.mark.parametrize("total", [499, 500, 100000])
def test_result_boundary_is_never_a_complete_capture(total):
    with pytest.raises(TruncatedQueryError):
        validate_capture(capture(total_results=total), now=NOW)


def test_all_current_keywords_are_seeded_without_a_ten_member_limit():
    config, queries = subject.plan_queries(manifest(keywords=[f"KW-{i}" for i in range(166)]))
    assert config["planned_query_count"] == 664 and len(queries) == 664
    conn, cursor = connection(run(frozen_manifest=config, manifest_hash=_hash(config)))
    assert subject.create_batch(conn, "scratch-batch", config)["seeded_queries"] == 664
    assert len(cursor.executemany.call_args.args[1]) == 664
    conn, _ = connection(run(frozen_manifest=config, manifest_hash="b" * 64))
    with pytest.raises(subject.BatchError, match="configuration changed"):
        subject.create_batch(conn, "scratch-batch", config)


def test_verified_broad_zero_only_omits_fully_covered_scope():
    config, queries = subject.plan_queries(manifest(zero_result_scopes=[zero_proof()]))
    assert len(queries) == 6 and config["preflight_skipped_query_count"] == 2
    assert all(not(q["year"] == 2025 and q["object_type"] == "service") for q in queries)
    _, partial = subject.plan_queries(manifest(zero_result_scopes=[zero_proof(published_to="2026-10-04")]))
    assert len(partial) == 8


@pytest.mark.parametrize("change", [{"keyword": "IA"}, {"counter": [1, 1, 1, 1, 1]},
                                    {"rows": [{}]}, {"rows_count": 1}, {"total_results": False},
                                    {"year": 2024}, {"seace_version": "2"}])
def test_nonzero_or_keyword_specific_preflight_cannot_remove_coverage(change):
    with pytest.raises(subject.BatchError):
        subject.plan_queries(manifest(zero_result_scopes=[zero_proof(**change)]))


@pytest.mark.parametrize("change", [{"cookies": "opaque"}, {"extra": {"authorization": "opaque"}},
                                    {"phase_policy": "enrich_first"}, {"pipeline": "pdfs"},
                                    {"procedure_years": [2026, 2026]}, {"max_snapshot_restarts": 0}])
def test_invalid_or_secret_configuration_is_rejected_before_db(change):
    conn = MagicMock()
    with pytest.raises(subject.BatchError):
        subject.create_batch(conn, "scratch-batch", manifest(**change))
    conn.cursor.assert_not_called()


def test_date_halves_cover_inclusively_without_holes_across_year_boundary():
    query = {**task()["query_manifest"], "year": 2025, "published_from": "2025-12-30", "published_to": "2026-01-02"}
    left, right = subject.split_query(query)
    assert (left["published_to"], right["published_from"]) == ("2025-12-31", "2026-01-01")
    assert left["year"] == right["year"] == 2025
    assert subject.split_query({**query, "published_to": "2025-12-30"}) is None


@pytest.mark.parametrize("change", [{"owner_live": False}, {"owner_token": PROFILE}, {"status": "pending"}])
def test_expired_or_wrong_owner_cannot_write_checkpoint(change):
    conn, cursor = owned(phase="enrichment", task_overrides=change)
    with pytest.raises(subject.BatchError, match="live lease"):
        subject.update_checkpoint(conn, 7, TOKEN, {"detail_saved": True})
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)


def test_checkpoint_cannot_fake_discovery_completion():
    conn, _ = owned()
    with pytest.raises(subject.BatchError, match="only by persist_page"):
        subject.update_checkpoint(conn, 7, TOKEN, {"next_page": 999})


def test_expired_enrichment_blocks_instead_of_reclaiming_billable_work():
    conn, cursor = connection(run("enrichment"), {"id": 7})
    assert subject.claim_task(conn, "scratch-batch", owner_id="test-worker")["status"] == "blocked"
    assert not any("owner_token=%s" in call.args[0] for call in cursor.execute.call_args_list)
    assert "clock_timestamp()" in cursor.execute.call_args_list[1].args[0]


def test_claim_keeps_existing_discovery_checkpoint_on_reclaim():
    resumed = task(checkpoint_json={"next_page": 3})
    conn, cursor = connection(run(), resumed, resumed)
    claimed = subject.claim_task(conn, "scratch-batch", owner_id="test-worker")
    assert claimed["checkpoint"] == {"next_page": 3}
    sql = cursor.execute.call_args.args[0]
    assert "clock_timestamp()" in sql and "checkpoint_json=" not in sql


def test_page_replay_does_not_move_checkpoint_backwards(monkeypatch):
    importer = MagicMock(return_value={"status": "running", "replayed": True})
    monkeypatch.setattr(subject, "import_capture", importer)
    conn, cursor = owned(task_overrides={"checkpoint_json": {"next_page": 2}})
    assert subject.persist_page(conn, 7, TOKEN, capture())["next_page"] == 2
    assert importer.call_args.kwargs["run_id"] == "scratch-pass"
    assert json.loads(cursor.execute.call_args.args[1][1])["next_page"] == 2


def test_page_skip_is_rejected_before_import(monkeypatch):
    importer = MagicMock()
    monkeypatch.setattr(subject, "import_capture", importer)
    value = capture(number=2, total_results=16, total_pages=2)
    value["rows"] = [{"procedure_id": 8000000001, "convocation_id": CONV, "buyer_name": "SYNTHETIC BUYER",
                      "nomenclature": "SYNTHETIC-1", "published_at": "05/10/2026 10:00", "description": "SOFTWARE",
                      "object_label": "Bien", "reference_amount": "100.00", "currency": "PEN", "seace_version": "3"}]
    conn, _ = owned()
    with pytest.raises(subject.BatchError, match="skip"):
        subject.persist_page(conn, 7, TOKEN, value)
    importer.assert_not_called()


def test_restart_retains_old_pass_and_membership_and_is_bounded():
    conn, cursor = owned()
    result = subject.restart_discovery_task(conn, 7, TOKEN)
    assert result["discovery_run_id"].endswith(":r1") and result["next_page"] == 1
    assert any("INSERT INTO prod2_batch_task_passes" in call.args[0] for call in cursor.execute.call_args_list)
    assert not any("DELETE" in call.args[0] for call in cursor.execute.call_args_list)
    conn, _ = owned(task_overrides={"snapshot_restarts": 3})
    assert subject.restart_discovery_task(conn, 7, TOKEN)["status"] == "blocked"


@pytest.mark.parametrize("remaining,blocked", [(1, 0), (0, 1)])
def test_discovery_gate_never_seeds_enrichment_before_all_leaves_complete(remaining, blocked):
    conn, cursor = connection(run(), {"total": 8, "remaining": remaining, "blocked": blocked})
    result = subject.advance_phase(conn, "scratch-batch")
    assert result["phase"] == "discovery"
    cursor.executemany.assert_not_called()


def test_gate_seeds_every_unique_candidate_then_metadata_enrichment():
    conn, cursor = connection(run(), {"total": 8, "remaining": 0, "blocked": 0})
    cursor.fetchall.return_value = [{"id_convocatoria": CONV + n} for n in range(350)]
    result = subject.advance_phase(conn, "scratch-batch")
    assert result["phase"] == "enrichment" and result["unique_candidates"] == 350
    assert len(cursor.executemany.call_args.args[1]) == 350


def test_all_verified_zero_proofs_can_finish_without_fake_keyword_pages():
    proofs = [zero_proof(year=y, object_type=o) for y in (2025, 2026) for o in ("good", "service")]
    config, queries = subject.plan_queries(manifest(zero_result_scopes=proofs))
    assert queries == []
    conn, cursor = connection(run(frozen_manifest=config), {"total": 0, "remaining": 0, "blocked": 0})
    cursor.fetchall.return_value = []
    assert subject.advance_phase(conn, "scratch-batch")["status"] == "complete"


@pytest.mark.parametrize("change", [{"status": "uncertain"}, {"profile_id": TOKEN}, {"model": "other"},
                                    {"policy_version": "other"}, {"request_hash": SHA}, {"id_convocatoria": CONV + 1}])
def test_jev_completion_is_bound_to_current_description_exact_profile_and_policy(change):
    _, cursor = connection(member(), ev(**change))
    with pytest.raises(subject.BatchError, match="does not bind"):
        subject._bound_jev(cursor, run(), CONV, 3)


def test_recovery_requires_exact_done_jev_and_preserves_checkpoint():
    conn, cursor = connection({"run_id": "scratch-batch"}, run("enrichment", status="blocked"),
                              task("enrich", status="blocked", expired=True, error_code="provider_transport"),
                              member(), ev(), {"blocked": 0})
    assert subject.recover_enrichment_task(conn, 7, 3)["status"] == "pending"
    updates = [call.args[0] for call in cursor.execute.call_args_list if "UPDATE" in call.args[0]]
    assert not any("UPDATE prod2_evaluations" in sql or "checkpoint_json=" in sql for sql in updates)
    conn, cursor = connection({"run_id": "scratch-batch"}, run("enrichment", status="blocked"),
                              task("enrich", status="blocked", expired=True, error_code="provider_transport"),
                              member(), ev(status="uncertain"))
    with pytest.raises(subject.BatchError):
        subject.recover_enrichment_task(conn, 7, 3)
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("status", ["running", "uncertain", "failed", "done"])
def test_unpaid_recovery_cannot_bypass_a_possibly_paid_request(status):
    conn, cursor = connection({"run_id": "scratch-batch"}, run("enrichment", status="blocked"),
        task("enrich", status="blocked", expired=True, error_code="source_403"), member(), {"id": 3, "status": status})
    with pytest.raises(subject.BatchError, match="may already have been billed"):
        subject.recover_unpaid_enrichment_task(conn, 7)
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)


def test_unpaid_recovery_reserves_pending_exact_key_before_resetting_task():
    conn, cursor = connection({"run_id": "scratch-batch"}, run("enrichment", status="blocked"),
        task("enrich", status="blocked", expired=True, error_code="metadata_task_failed"), member(),
        {"id": 3, "status": "pending"}, {"blocked": 0})
    assert subject.recover_unpaid_enrichment_task(conn, 7)["jev_evaluation_id"] == 3
    sqls = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO prod2_evaluations" in sql for sql in sqls)
    assert not any("UPDATE prod2_evaluations" in sql for sql in sqls)


@pytest.mark.parametrize("code,attempts", [("single_day_result_cap", 1), ("io_claims_exhausted", 3), ("source_403", 3)])
def test_discovery_recovery_does_not_reset_exhausted_or_capped_partitions(code, attempts):
    conn, cursor = connection({"run_id": "scratch-batch"}, run(status="blocked"),
        task(status="blocked", expired=True, error_code=code, attempts=attempts))
    with pytest.raises(subject.BatchError):
        subject.recover_discovery_task(conn, 7)
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)


def test_discovery_recovery_retains_pages_and_checkpoint_for_fresh_native_validation():
    conn, cursor = connection({"run_id": "scratch-batch"}, run(status="blocked"),
        task(status="blocked", expired=True, error_code="source_403"), {"blocked": 0})
    assert subject.recover_discovery_task(conn, 7)["status"] == "pending"
    assert not any("DELETE" in call.args[0] or "checkpoint_json=" in call.args[0]
                   for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("change", [{"detail_hash": "b" * 64}, {"id_convocatoria": CONV + 1}, {"is_current": False}])
def test_task_completion_rejects_foreign_or_stale_participation(change):
    assessment = {"detail_hash": SHA, "id_convocatoria": CONV, "is_current": True,
                  "policy_version": subject.PARTICIPATION_POLICY_VERSION, **change}
    conn, cursor = owned(member(), ev(), {"detail_hash": SHA, "document_inventory_complete": True},
                          assessment, phase="enrichment")
    with pytest.raises(subject.BatchError, match="Participation"):
        subject.complete_task(conn, 7, TOKEN, {"jev_evaluation_id": 3, "participation_assessment_id": 4, "detail_hash": SHA})
    assert not any("SET status='complete'" in call.args[0] for call in cursor.execute.call_args_list)


def test_assessment_uses_exact_official_snapshot_and_replay_does_not_reactivate_history():
    assessment = decide(detail(), NOW)
    conn, cursor = connection(stored_detail(), None, None, {"id": 4})
    result = subject.save_participation_assessment(conn, CONV, detail_hash=SHA, assessment=assessment)
    assert result == {"id": 4, "cached": False, "is_current": True}
    assert not any("UPDATE opportunities" in call.args[0] for call in cursor.execute.call_args_list)
    conn, cursor = connection(stored_detail(), {"id": 4, "is_current": False})
    assert subject.save_participation_assessment(conn, CONV, detail_hash=SHA, assessment=assessment)["is_current"] is False
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("field,value", [("convocatoria_id", CONV + 1), ("procedure_id", 1),
                                        ("item_states", ["Adjudicado"]), ("schedule", [])])
def test_even_self_consistent_assessment_cannot_bind_different_official_facts(field, value):
    assessment = decide(detail(), NOW)
    assessment["snapshot"][field] = value
    assessment["snapshot_sha256"] = _hash(assessment["snapshot"])
    assessment["assessment_sha256"] = _hash({k: v for k, v in assessment.items() if k != "assessment_sha256"})
    conn, cursor = connection(stored_detail())
    with pytest.raises(subject.BatchError):
        subject.save_participation_assessment(conn, CONV, detail_hash=SHA, assessment=assessment)
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)


def test_assessment_hash_tampering_and_same_time_contradictions_are_rejected():
    assessment = decide(detail(), NOW)
    bad = deepcopy(assessment)
    bad["state"] = "adjudicado"
    with pytest.raises(subject.BatchError, match="fingerprint"):
        subject.save_participation_assessment(MagicMock(), CONV, detail_hash=SHA, assessment=bad)
    conn, _ = connection(stored_detail(), None, {"as_of": NOW})
    with pytest.raises(subject.BatchError, match="Stale or contradictory"):
        subject.save_participation_assessment(conn, CONV, detail_hash=SHA, assessment=assessment)
