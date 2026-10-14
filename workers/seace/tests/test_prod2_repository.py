"""Offline contracts: no DB/API calls, synthetic public metadata only."""
from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
import json

import pytest

from buenapro_worker.prod2 import repository as subject

NOW = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)
CODE = "efdf87fb-9e04-4fa7-9c34-5912db454f7f"
PROFILE = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3"
OPPORTUNITY = "bcbc122b-c585-48fc-b345-07221a994975"
TOKEN = "61c03018-1c05-47fe-8e7c-99caaae4d5de"
SHA = "a" * 64
SOURCE = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml"


def detail(complete=True):
    return {"procedure_id": 1255251, "convocatoria_id": 1046958,
            "nomenclature": "TEST-2026-1", "entity": "SYNTHETIC BUYER", "object": "Servicio",
            "description": "SOFTWARE", "seace_version": "3",
            "schedule": [{"stage": "Registro de participantes", "starts_at_text": "05/10/2026 08:00",
                          "ends_at_text": "06/10/2026 10:00"},
                         {"stage": "Presentación de ofertas", "starts_at_text": "07/10/2026 08:00",
                          "ends_at_text": "07/10/2026 17:00"},
                         {"stage": "Otorgamiento de Buena Pro", "starts_at_text": "08/10/2026",
                          "ends_at_text": "08/10/2026"}],
            "documents": [{"uuid": CODE, "seace_version": "3", "filename": "bases.pdf",
                           "stage": "Otorgamiento de Buena Pro", "type": "Bases",
                           "published_at": "05/10/2026 10:00"}],
            "document_inventory_total": 1 if complete else 2, "document_inventory_complete": complete,
            "item_states": ["Adjudicado"]}


def connection(*responses):
    conn, cursor = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.side_effect = responses
    return conn, cursor


def discovery():
    return {"id_procedimiento": 1255251, "nomenclature": "TEST-2026-1", "buyer_name": "SYNTHETIC BUYER",
            "description": "SOFTWARE", "object_label": "Servicio", "opportunity_id": OPPORTUNITY}


def row():
    return {"procedure_id": 1255251, "convocatoria_id": 1046958, "entity": "SYNTHETIC BUYER",
            "published_at": "05/10/2026 10:00", "nomenclature": "TEST-2026-1", "object": "Servicio",
            "description": "SOFTWARE", "amount_text": "100.00", "currency": "PEN", "seace_version": "3"}


def query():
    return {"keyword": "SOFTWARE", "object_type": "service", "year": 2026,
            "published_from": "2026-01-01", "published_to": "2026-10-05", "seace_version": "3"}


def test_buena_pro_schedule_and_adjudicado_are_not_a_confirmed_winner():
    conn, cursor = connection(discovery(), None)
    result = subject.persist_detail(conn, detail(), observed_at=NOW, source_url=SOURCE)
    insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO prod2_details" in call.args[0])
    award = json.loads(insert.args[1][11])
    assert award["schedule_stage_present"] is True
    assert award["stage_documents_listed"] == [CODE]
    assert award["award_result"] == "unknown" and award["winner"] is None
    assert award["effective_award_date"] is None
    assert result["opportunity_id"] == OPPORTUNITY
    assert not any("UPDATE opportunities" in call.args[0] for call in cursor.execute.call_args_list)


def test_date_only_schedule_does_not_manufacture_a_closing_hour():
    prepared = subject.prepare_detail(detail())
    assert prepared["schedule"][2]["starts_at"] is None
    assert prepared["schedule"][2]["starts_at_text"] == "08/10/2026"
    assert prepared["schedule"][1]["ends_at"] == "2026-10-07T17:00:00-05:00"


@pytest.mark.parametrize("field", ["cookies", "html", "javax.faces.ViewState", "alf_ticket"])
def test_session_and_html_fields_are_rejected_before_database_access(field):
    data = detail()
    data[field] = "synthetic-opaque"
    conn = MagicMock()
    with pytest.raises(subject.Prod2PersistenceError, match="Unexpected fields"):
        subject.persist_detail(conn, data, observed_at=NOW, source_url=SOURCE)
    conn.cursor.assert_not_called()


@pytest.mark.parametrize("source", [SOURCE + "?alf_ticket=opaque", SOURCE + ";jsessionid=opaque",
                                    "http://prod2.seace.gob.pe/ficha", "https://outside.example/ficha"])
def test_unstable_or_nonofficial_source_references_are_rejected(source):
    with pytest.raises(subject.Prod2PersistenceError, match="stable official"):
        subject.persist_detail(MagicMock(), detail(), observed_at=NOW, source_url=source)


def test_partial_inventory_never_marks_unseen_documents_missing():
    conn, cursor = connection(discovery(), None)
    subject.persist_detail(conn, detail(False), observed_at=NOW, source_url=SOURCE)
    assert not any("SET is_listed=false" in call.args[0] for call in cursor.execute.call_args_list)


def test_complete_inventory_reconciles_documents_without_removing_download_history():
    conn, cursor = connection(discovery(), None)
    subject.persist_detail(conn, detail(), observed_at=NOW, source_url=SOURCE)
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("SET is_listed=false" in sql for sql in statements)
    assert not any("DELETE" in sql for sql in statements)
    document_call = next(call for call in cursor.execute.call_args_list if "INSERT INTO prod2_documents" in call.args[0])
    assert document_call.args[1][7] == subject.METADATA_URL + "?id=" + CODE


def publication_detail(complete=True):
    data = detail()
    first = {**data["documents"][0], "stage": "Convocatoria", "type": "Bases administrativas"}
    second = {**first, "stage": "Otorgamiento de Buena Pro", "type": "Acta",
              "published_at": "08/10/2026 11:00"}
    data.update(documents=[first], document_events=[first, second],
                document_inventory_total=2 if complete else 3, document_inventory_complete=complete)
    return data


def test_same_resource_keeps_both_publications_and_one_official_download_reference():
    conn, cursor = connection(discovery(), None)
    result = subject.persist_detail(conn, publication_detail(), observed_at=NOW, source_url=SOURCE)
    calls = cursor.execute.call_args_list
    resources = [call for call in calls if "INSERT INTO prod2_documents(" in call.args[0]]
    events = [call for call in calls if "INSERT INTO prod2_document_events(" in call.args[0]]
    assert result["documents_count"] == 1 and result["document_events_count"] == 2
    assert len(resources) == 1 and len(events) == 2
    assert events[0].args[1][2] != events[1].args[1][2]
    insert = next(call for call in calls if "INSERT INTO prod2_details(" in call.args[0])
    raw = json.loads(insert.args[1][14])
    assert len(raw["documents"]) == 1 and len(raw["document_events"]) == 2
    assert json.loads(insert.args[1][11])["stage_documents_listed"] == [CODE]
    assert not any("prod2_document_downloads" in call.args[0] for call in calls)


@pytest.mark.parametrize("mutation", ["duplicate", "name_conflict", "version_conflict", "wrong_first", "missing", "wrong_total"])
def test_corrupt_publication_inventory_is_rejected_before_any_database_call(mutation):
    data = deepcopy(publication_detail())
    if mutation == "duplicate":
        data["document_events"].append(deepcopy(data["document_events"][0]))
        data["document_inventory_total"] = 3
    elif mutation == "name_conflict":
        data["document_events"][1]["filename"] = "another.pdf"
    elif mutation == "version_conflict":
        data["document_events"][1]["seace_version"] = "2"
    elif mutation == "wrong_first":
        data["document_events"].reverse()
    elif mutation == "missing":
        data["document_events"] = []
    else:
        data["document_inventory_total"] = 1
    conn = MagicMock()
    with pytest.raises(subject.Prod2PersistenceError):
        subject.persist_detail(conn, data, observed_at=NOW, source_url=SOURCE)
    conn.transaction.assert_not_called()
    conn.cursor.assert_not_called()


def test_legacy_hash_contract_does_not_add_inferred_publications():
    legacy = subject.prepare_detail(detail())
    assert "document_events" not in legacy
    explicit = detail()
    explicit["document_events"] = deepcopy(explicit["documents"])
    new = subject.prepare_detail(explicit)
    assert {key: value for key, value in new.items() if key != "document_events"} == legacy
    assert subject.hash_request(new) != subject.hash_request(legacy)
    conn, cursor = connection(discovery(), None)
    subject.persist_detail(conn, detail(), observed_at=NOW, source_url=SOURCE)
    assert len([call for call in cursor.execute.call_args_list
                if "INSERT INTO prod2_document_events(" in call.args[0]]) == 1


def test_partial_and_complete_refresh_reconcile_resource_and_event_lists_without_deleting_history():
    for complete in (False, True):
        conn, cursor = connection(discovery(), None)
        subject.persist_detail(conn, publication_detail(complete), observed_at=NOW, source_url=SOURCE)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        assert any("UPDATE prod2_document_events SET is_listed=false" in sql for sql in statements) == complete
        assert any("UPDATE prod2_documents SET is_listed=false" in sql for sql in statements) == complete
        assert not any("DELETE" in sql or "prod2_document_downloads" in sql for sql in statements)


def test_stale_or_wrong_identity_detail_never_overwrites_saved_data():
    wrong = discovery()
    wrong["id_procedimiento"] = 1
    conn, cursor = connection(wrong)
    with pytest.raises(subject.Prod2PersistenceError, match="does not match"):
        subject.persist_detail(conn, detail(), observed_at=NOW, source_url=SOURCE)
    assert not any("INSERT" in call.args[0] for call in cursor.execute.call_args_list)
    conn, cursor = connection(discovery(), {"detail_hash": SHA, "detail_fetched_at": NOW})
    with pytest.raises(subject.Prod2PersistenceError, match="contradictory"):
        subject.persist_detail(conn, detail(), observed_at=NOW, source_url=SOURCE)


def test_pilot_selection_persists_only_selected_row_and_never_claims_query_coverage(monkeypatch):
    identity = MagicMock(return_value=(OPPORTUNITY, True))
    monkeypatch.setattr(subject, "_identity", identity)
    conn, cursor = connection({"requested_limit": 10}, None, {"total": 9})
    result = subject.upsert_pilot_discovery(conn, row(), query(), batch_id="pilot-10", observed_at=NOW)
    assert result["coverage"] == "pilot_selection"
    assert identity.call_count == 1
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO prod2_pilot_observations" in sql for sql in statements)
    assert not any("prod2_discovery_runs" in sql or "prod2_discovery_pages" in sql for sql in statements)


def test_eleventh_pilot_member_is_rejected_before_canonical_identity_mutation(monkeypatch):
    identity = MagicMock()
    monkeypatch.setattr(subject, "_identity", identity)
    conn, cursor = connection({"requested_limit": 10}, None, {"total": 10})
    with pytest.raises(subject.Prod2PersistenceError, match="limit"):
        subject.upsert_pilot_discovery(conn, row(), query(), batch_id="pilot-10", observed_at=NOW)
    identity.assert_not_called()
    assert not any("INSERT INTO prod2_discoveries" in call.args[0] for call in cursor.execute.call_args_list)


def claim(conn, **overrides):
    return subject.claim_evaluation(conn, 1046958, **{"kind": "jev_affinity", "request_hash": SHA,
           "model": "test-model", "policy_version": "v1", "profile_id": PROFILE, **overrides})


def test_completed_evaluation_is_a_cache_hit_without_a_new_claim():
    conn, cursor = connection({"id": 7, "status": "done", "result_json": {"label": "directa"}})
    assert claim(conn) == {"status": "cached", "id": 7, "result_json": {"label": "directa"}}
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("expired,expected", [(False, "busy"), (True, "uncertain")])
def test_concurrent_or_expired_api_claim_never_creates_another_billable_attempt(expired, expected):
    conn, cursor = connection({"id": 7, "status": "running", "expired": expired})
    assert claim(conn) == {"status": expected, "id": 7}
    assert not any("claim_token=%s" in call.args[0] for call in cursor.execute.call_args_list)
    assert any("FOR UPDATE" in call.args[0] for call in cursor.execute.call_args_list)


def test_new_pending_claim_is_durable_and_keys_include_input_profile_model_and_policy():
    keys = []
    for change in ({}, {"request_hash": "b" * 64}, {"profile_id": OPPORTUNITY},
                   {"model": "different"}, {"policy_version": "v2"}):
        conn, cursor = connection({"id": 7, "status": "pending"})
        result = claim(conn, **change)
        assert result["status"] == "claimed"
        keys.append(result["evaluation_key"])
        assert any("ON CONFLICT(evaluation_key) DO NOTHING" in call.args[0] for call in cursor.execute.call_args_list)
    assert len(set(keys)) == 5


def test_provider_result_requires_original_claim_owner_and_allows_late_uncertain_resolution():
    conn, cursor = connection(None)
    with pytest.raises(subject.Prod2PersistenceError, match="owned"):
        subject.complete_evaluation(conn, 7, TOKEN, {"label": "directa"}, input_tokens=120, cost_usd="0.00001")
    sql, params = cursor.execute.call_args.args
    assert "claim_token=%s" in sql and "('running','uncertain')" in sql
    assert params[-2:] == (7, TOKEN)


@pytest.mark.parametrize("format", ["pdf", "7z"])
def test_recorded_download_retains_original_hash_and_does_not_persist_temporary_url(format):
    manifest = {"document_uuid": CODE, "sha256": SHA, "bytes": 1234, "content_type": "application/pdf",
                "format": format, "source_url": "https://alfprod.seace.gob.pe/file?alf_ticket=synthetic"}
    conn, cursor = connection({"is_listed": True}, {"bytes": 1234, "format": format})
    subject.record_document_download(conn, 1046958, manifest)
    assert "alf_ticket" not in str(cursor.execute.call_args_list)
    assert SHA in cursor.execute.call_args.args[1]


@pytest.mark.parametrize("path", ["/Users/local/document.pdf", "documents/../private.pdf", "https://storage.example/file"])
def test_local_paths_and_urls_cannot_be_advertised_as_durable_storage_keys(path):
    manifest = {"document_uuid": CODE, "sha256": SHA, "bytes": 1234, "content_type": "application/pdf", "format": "pdf"}
    with pytest.raises(subject.Prod2PersistenceError, match="storage key"):
        subject.record_document_download(MagicMock(), 1046958, manifest, storage_key=path)


def extraction_result():
    return SimpleNamespace(raw_json={"requirements": {"provider": {"specific_experience":
               [{"descripcion": "Experiencia en soporte", "evidence": [{"page": 2}]}]}}},
               model="test-model", prompt_version="v1", schema_version="v2", input_tokens=100,
               output_tokens=80, cost_usd="0.00001", requires_human_review=False)


def test_extraction_requires_document_hash_binding_before_replacing_current_requirements():
    conn, cursor = connection({"opportunity_id": OPPORTUNITY}, {"id_convocatoria": 1046958,
        "kind": "gemini_extraction", "status": "done", "model": "test-model", "result_json": {
        "document_uuid": CODE, "document_sha256": "b" * 64}})
    with pytest.raises(subject.Prod2PersistenceError, match="document revision"):
        subject.persist_extraction(conn, 1046958, CODE, SHA, evaluation_id=7, result=extraction_result())
    assert not any("UPDATE prod2_document_extractions" in call.args[0] for call in cursor.execute.call_args_list)


def test_extraction_uses_prod2_tables_and_canonical_opportunity_without_fake_prod4_evidence():
    conn, cursor = connection({"opportunity_id": OPPORTUNITY}, {"id_convocatoria": 1046958,
        "kind": "gemini_extraction", "status": "done", "model": "test-model", "result_json": {
        "document_uuid": CODE, "document_sha256": SHA}}, None, {"format": "pdf"}, {"id": 20})
    result = subject.persist_extraction(conn, 1046958, CODE, SHA, evaluation_id=7, result=extraction_result())
    assert result["extraction_id"] == 20 and result["facet_count"] == 1
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO prod2_requirement_facets" in sql for sql in statements)
    assert not any("prod4_" in sql for sql in statements)


def test_effective_gemini_fallback_reuses_the_reservation_without_another_claim():
    result = extraction_result()
    result.model = "gemini-2.5-flash"
    reservation = {"id_convocatoria": 1046958, "kind": "gemini_extraction", "status": "done",
                   "model": "gemini-3.1-flash-lite", "result_json": {
                   "document_uuid": CODE, "document_sha256": SHA, "model": result.model}}
    conn, cursor = connection({"opportunity_id": OPPORTUNITY}, reservation, None, {"format": "pdf"}, {"id": 20})
    saved = subject.persist_extraction(conn, 1046958, CODE, SHA, evaluation_id=7, result=result,
               allowed_models=("gemini-3.1-flash-lite", "gemini-2.5-flash"))
    assert saved["extraction_id"] == 20
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert not any("INSERT INTO prod2_evaluations" in sql for sql in statements)
    insertion = next(call for call in cursor.execute.call_args_list if "INSERT INTO prod2_document_extractions" in call.args[0])
    assert insertion.args[1][5] == "gemini-2.5-flash"


@pytest.mark.parametrize("allowed", [None, ("gemini-3.1-flash-lite",), ("unconfigured",)])
def test_unconfigured_fallback_cannot_be_persisted(allowed):
    result = extraction_result()
    result.model = "gemini-2.5-flash"
    reservation = {"id_convocatoria": 1046958, "kind": "gemini_extraction", "status": "done",
                   "model": "gemini-3.1-flash-lite", "result_json": {
                   "document_uuid": CODE, "document_sha256": SHA, "model": result.model}}
    conn, cursor = connection({"opportunity_id": OPPORTUNITY}, reservation)
    with pytest.raises(subject.Prod2PersistenceError, match="allowlist|configured"):
        subject.persist_extraction(conn, 1046958, CODE, SHA, evaluation_id=7, result=result,
                                   allowed_models=allowed)
    assert not any("UPDATE prod2_document_extractions" in call.args[0] for call in cursor.execute.call_args_list)


def test_final_match_overrides_false_source_and_preserves_joint_profile_provenance():
    source = {"opportunity_id": OPPORTUNITY, "requires_human_review": False, "quality": "auto",
              "facet_count": 1, "sha256_original": SHA, "raw_extraction_json": extraction_result().raw_json}
    reservation = {"id_convocatoria": 1046958, "kind": "gemini_match", "status": "done",
                   "profile_id": PROFILE, "policy_version": "v1", "result_json": {
                   "extraction_id": 20, "profile_hash": SHA}}
    conn, cursor = connection(source, reservation, {"id": 22})
    result = subject.persist_match(conn, 1046958, profile_id=PROFILE, extraction_id=20,
        evaluation_id=9, profile_hash=SHA, score=85, verdict="verde", missing_actions_json=[],
        breakdown_json={"meta": {"source_key": "seace_prod4", "member_profile_hashes": {PROFILE: "b" * 32}}})
    assert result["match_id"] == 22
    insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO opportunity_matches" in call.args[0])
    meta = json.loads(insert.args[1][5])["meta"]
    assert meta["source_key"] == "seace_prod2" and meta["document_sha256"] == SHA
    assert meta["member_profile_hashes"] == {PROFILE: "b" * 32}


def test_preliminary_jev_result_cannot_be_stored_as_document_compliance():
    source = {"opportunity_id": OPPORTUNITY, "requires_human_review": False, "quality": "auto",
              "facet_count": 1, "sha256_original": SHA, "raw_extraction_json": extraction_result().raw_json}
    conn, cursor = connection(source, {"id_convocatoria": 1046958, "kind": "jev_affinity", "status": "done",
                                     "profile_id": PROFILE})
    with pytest.raises(subject.Prod2PersistenceError, match="profile reservation"):
        subject.persist_match(conn, 1046958, profile_id=PROFILE, extraction_id=20, evaluation_id=9,
            profile_hash=SHA, score=85, verdict="verde", breakdown_json={}, missing_actions_json=[])
    assert not any("INSERT INTO opportunity_matches" in call.args[0] for call in cursor.execute.call_args_list)


def test_only_generic_legal_facets_do_not_create_an_eligibility_verdict():
    source = {"requires_human_review": False, "quality": "auto", "facet_count": 1,
              "raw_extraction_json": {"requirements": {"provider": {"rnp": "Registro vigente"}}}}
    conn, cursor = connection(source)
    with pytest.raises(subject.Prod2PersistenceError, match="administrative"):
        subject.persist_match(conn, 1046958, profile_id=PROFILE, extraction_id=20, evaluation_id=9,
            profile_hash=SHA, score=100, verdict="verde", breakdown_json={}, missing_actions_json=[])
    assert not any("INSERT INTO opportunity_matches" in call.args[0] for call in cursor.execute.call_args_list)
