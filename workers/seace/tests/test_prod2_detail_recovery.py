"""Metadata-only recovery must never repeat Jev, downloads or original run writes."""
import importlib.util
from pathlib import Path
import sys
from unittest.mock import MagicMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_detail_recovery", ROOT / "scripts/recover_prod2_details.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def member():
    result = {"affinity_index": 73, "request_hash": "a" * 64}
    return {"accepted": True, "opportunity_id": "00000000-0000-4000-8000-000000000001",
            "request_hash": "a" * 64, "cache_status": "done", "result_json": result,
            "cache_result": result.copy(), "row_manifest": {"convocation_id": 123,
                "procedure_id": 456, "description": "SOFTWARE", "buyer_name": "SYNTHETIC BUYER",
                "nomenclature": "TEST-2026", "object_label": "Servicio"},
            "query_manifest": {"keyword": "software", "year": 2026, "object_type": "service",
                "published_from": "2026-04-06", "published_to": "2026-10-06", "seace_version": "3"}}


def conn_for(data):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = data
    return conn


@pytest.mark.parametrize('override', [None, {"accepted": False}, {"opportunity_id": None},
    {"cache_status": "uncertain"}, {"result_json": {"affinity_index": 59}},
    {"result_json": {"affinity_index": True}}, {"result_json": {"affinity_index": float('nan')}},
    {"cache_result": {"affinity_index": 74}}, {"row_manifest": {"convocation_id": 999}}])
def test_recovery_requires_already_accepted_exact_completed_membership(override):
    conn = conn_for(None if override is None else member() | override)
    with pytest.raises(ValueError):
        subject.member_proof(conn, 'synthetic-original-run', 123)
    assert all('INSERT' not in call.args[0] and 'UPDATE' not in call.args[0]
               for call in conn.execute.call_args_list)


def test_source_rejection_stops_before_any_further_http_request():
    budget = subject.SourceBudget(10, 2)
    budget.pacer = lambda request: None
    request = httpx.Request('GET', 'https://prod2.seace.gob.pe/')
    budget.request(request)
    with pytest.raises(RuntimeError, match='Source rejected'):
        budget.response(httpx.Response(429, request=request))
    with pytest.raises(RuntimeError):
        budget.request(request)
    assert budget.requests == 1


def test_source_request_cap_is_checked_before_request():
    budget = subject.SourceBudget(1, 2)
    budget.pacer = lambda request: None
    budget.request(None)
    with pytest.raises(RuntimeError):
        budget.request(None)
    assert budget.requests == 1


def test_recovered_duplicate_resource_events_save_only_references_and_replay_without_source(tmp_path, monkeypatch):
    proof = subject.member_proof(conn_for(member()), 'synthetic-original-run', 123)
    code = '00000000-0000-4000-8000-000000000009'
    doc = {"uuid": code, "seace_version": "3", "filename": "test.pdf", "stage": "Convocatoria",
           "type": "Bases", "published_at": "06/10/2026 10:00"}
    detail = {"convocatoria_id": 123, "documents": [doc], "document_events": [doc,
              doc | {"stage": "Integración", "type": "Bases Integradas"}],
              "document_inventory_complete": True, "schedule": []}
    capture = {"proof_hash": subject.hash_request(proof), "observed_at": "2026-10-06T17:00:00+00:00",
               "detail": detail}
    fetch = MagicMock(return_value=capture)
    persist = MagicMock(return_value={"detail_hash": "b" * 64})
    monkeypatch.setattr(subject, 'fetch_capture', fetch)
    monkeypatch.setattr(subject, 'persist_detail', persist)
    monkeypatch.setattr(subject, 'decide', lambda *a: {"state": "closed"})
    monkeypatch.setattr(subject, 'save_participation_assessment', MagicMock())
    original = tmp_path / 'synthetic-original-run' / '123'
    original.mkdir(parents=True)
    receipt = original / 'outcome.private.json'
    receipt.write_text('{"status":"needs_review"}')
    frozen = receipt.read_bytes()
    folder = tmp_path / 'recovery' / '123'
    conn = MagicMock()
    result = subject.recover_one(conn, proof, folder, object())
    replay = subject.recover_one(conn, proof, folder, object())
    assert result == replay
    assert result['unique_documents_count'] == 1 and result['publication_events_count'] == 2
    assert result['document_downloads'] == result['provider_calls'] == 0
    assert result['documents'][0]['source_url'] == subject.METADATA_URL + '?id=' + code
    assert fetch.call_count == 1 and receipt.read_bytes() == frozen
    assert not list(folder.glob('*.pdf')) and not list(folder.glob('*.zip'))


def test_dry_run_has_no_connections_or_source_calls(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['recover', '--run-id', 'synthetic-original-run',
        '--convocation-id', '123', '--folder', 'tmp/synthetic-recovery', '--dry-run'])
    connection = MagicMock(side_effect=AssertionError('No connection in dry-run'))
    monkeypatch.setattr(subject, 'production_settings', connection)
    assert subject.main() == 0
    assert '"provider_calls": 0' in capsys.readouterr().out
    connection.assert_not_called()


def test_main_blocks_original_folder_before_any_connection(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['recover', '--run-id', 'synthetic-original-run',
        '--convocation-id', '123', '--folder', 'tmp/synthetic-original-run'])
    connection = MagicMock()
    monkeypatch.setattr(subject, 'production_settings', connection)
    with pytest.raises(ValueError, match='separate private recovery'):
        subject.main()
    connection.assert_not_called()
