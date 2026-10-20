"""Isolated, no-network regression checks for the nine-case explicit follow-up."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPT = Path(__file__).with_name('recover_prod2_sector_reviews.py')
spec = importlib.util.spec_from_file_location('sector_review_completion_under_test', SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


@pytest.fixture(autouse=True)
def no_provider_http(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Provider HTTP is forbidden in this isolated test suite')
    monkeypatch.setattr(httpx.Client, 'post', denied)
    monkeypatch.setattr(httpx.Client, 'get', denied)
    monkeypatch.setattr(httpx.AsyncClient, 'post', denied)
    monkeypatch.setattr(httpx.AsyncClient, 'get', denied)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def fresh_record():
    now = datetime.now(timezone.utc)
    return {'confirmed_open': True, 'requires_fresh_lifecycle': False, 'temporal_state': 'submission_open',
        'temporal_observed_at': now.isoformat(), 'closes_at': (now+timedelta(days=1)).isoformat(), 'detail_hash': 'd'*64}


class Reply:
    def __init__(self, value): self.value = value
    def fetchone(self): return self.value
    def fetchall(self): return self.value


class FakePG:
    def __init__(self, record, prior=(), current=(), source_patch=None):
        self.record, self.prior, self.current = record, list(prior), list(current)
        self.source_patch, self.statements = source_patch or {}, []
    def execute(self, statement, params):
        self.statements.append((statement, params))
        assert statement.lstrip().startswith('SELECT'), 'Preflight must remain read-only'
        if 'SELECT d.detail_hash' in statement:
            return Reply({'detail_hash': self.record['detail_hash'], 'proposals_closes_at': datetime.fromisoformat(self.record['closes_at']),
                'id_procedimiento': 1, 'opportunity_id': 'source', **self.source_patch})
        if 'prod2_document_extractions' in statement: return Reply(self.current)
        if 'prod2_evaluations' in statement: return Reply(self.prior)
        raise AssertionError('Unexpected preflight query')


def owned_claim(tmp_path, request='r'*64, **overrides):
    claim = {'id': 42, 'request_hash': request, 'claim_token': 'owned-token', **overrides}
    put(tmp_path/str(review.IDS[0])/('gemini_extraction-'+request+'.claim.private.json'), claim)
    return claim


def test_before_request_allows_only_its_exact_current_owned_claim(tmp_path):
    record, request = fresh_record(), 'r'*64
    claim = owned_claim(tmp_path, request)
    own = {**claim, 'status': 'running'}
    pg = FakePG(record, prior=[own])
    review.pg_preflight(pg, {'folder': str(tmp_path)}, review.IDS[0], record, request)
    assert len(pg.statements) == 3
    for patch in [{'claim_token': 'foreign-token'}, {'id': 99}, {'request_hash': 'other'}, {'status': 'uncertain'}, {'status': 'failed'}]:
        with pytest.raises(ValueError, match='Historical paid attempt'):
            review.pg_preflight(FakePG(record, prior=[{**own, **patch}]), {'folder': str(tmp_path)}, review.IDS[0], record, request)


def test_initial_preflight_never_adopts_even_a_matching_previous_payment(tmp_path):
    record, request = fresh_record(), 'r'*64
    claim = owned_claim(tmp_path, request)
    for status in ['running', 'done', 'uncertain']:
        with pytest.raises(ValueError, match='Historical paid attempt'):
            review.pg_preflight(FakePG(record, prior=[{**claim, 'status': status}]), {'folder': str(tmp_path)}, review.IDS[0], record)


def test_preflight_preserves_current_readings_and_rejects_changed_detail_or_deadline(tmp_path):
    record = fresh_record()
    with pytest.raises(ValueError, match='existing reading'):
        review.pg_preflight(FakePG(record, current=[{'id': 1}]), {'folder': str(tmp_path)}, review.IDS[0], record)
    for patch in [{'detail_hash': 'changed'}, {'proposals_closes_at': None},
        {'proposals_closes_at': datetime.fromisoformat(record['closes_at'])+timedelta(seconds=1)}]:
        with pytest.raises(ValueError, match='PG source/deadline'):
            review.pg_preflight(FakePG(record, source_patch=patch), {'folder': str(tmp_path)}, review.IDS[0], record)


def test_lifecycle_requires_explicit_recent_positive_open_and_a_future_precise_deadline():
    record, now = fresh_record(), datetime.now(timezone.utc)
    review.validate_lifecycle(record, now)
    for patch in [{'confirmed_open': False}, {'requires_fresh_lifecycle': True}, {'temporal_state': 'unknown'},
        {'temporal_observed_at': (now-timedelta(hours=7)).isoformat()},
        {'temporal_observed_at': (now+timedelta(minutes=2)).isoformat()},
        {'closes_at': (now-timedelta(seconds=1)).isoformat()},
        {'closes_at': '2026-10-20T12:00:00'}]:
        with pytest.raises(ValueError): review.validate_lifecycle({**record, **patch}, now)


@pytest.mark.parametrize('mismatch', [True, False])
def test_selector_cache_miss_is_impossible_to_turn_into_a_provider_request(tmp_path, monkeypatch, mismatch):
    original, output = tmp_path/'original', tmp_path/'followup'
    cid = review.SELECTOR_ID
    source = original/str(cid)
    code = '00000000-0000-4000-8000-000000000001'
    original_bytes = b'%PDF-isolated-not-rendered'
    record = {'documents': [{'document_uuid': code, 'original_sha256': hashlib.sha256(original_bytes).hexdigest()}], 'description': 'Mantenimiento de bombeo'}
    put(source/'outcome.private.json', record)
    put(original/'manifest.private.json', {'archive_inventory_policy': 'unchanged'})
    put(source/'selection.private.json', {'request_hash': 'a'*64, 'policy_version': 'policy', 'raw_response': {}})
    put(source/'document-previews.private.json', {'loose:'+code: {'preview_text': 'Bases de mantenimiento', 'content_hash': 'p'*64}})
    (source/(code+'.pdf')).write_bytes(original_bytes)
    monkeypatch.setattr(review, 'ORIGINAL', original)
    class SelectorMiss:
        def __init__(self, key, **kwargs): self.client = kwargs['client']
        def select(self, *args, **kwargs): return self.client.post('https://api.typesafe.ai/v1/systemone')
    monkeypatch.setattr(review, 'JevDocumentSelector', SelectorMiss)
    monkeypatch.setattr(review, 'document_selection_request_hash', lambda *args, **kwargs: ('b' if mismatch else 'a')*64)
    monkeypatch.setattr(review, 'prepare_selected_document_set', lambda *args, **kwargs: pytest.fail('Cache miss cannot reach preparation'))
    with pytest.raises(ValueError, match='Exact cached selector input|No new selector call'):
        review.prepare_case({'folder': str(output), 'cases': [{'convocation_id': cid, 'preparation': None}]}, cid)
    assert not (output/str(cid)/'selection-reprojected.private.json').exists()
    assert (source/(code+'.pdf')).read_bytes() == original_bytes


def test_old_terminal_partition_and_zero_llm1_requirement_are_checked_without_any_provider(tmp_path, monkeypatch):
    records = []
    for cid in range(1, 149): records.append({'convocation_id': cid, 'status': 'closed_without_reading'})
    for cid in review.IDS:
        record = {'convocation_id': cid, 'status': 'selection_needs_review' if cid == review.SELECTOR_ID else 'needs_review',
            'accepted': True, 'confirmed_open': True, 'document_preparation_review': {'reason': 'retrieval_context_needs_expansion_or_review'}}
        records.append(record)
        put(tmp_path/str(cid)/'outcome.private.json', record)
    put(tmp_path/'report-001.private.json', {'records': records})
    monkeypatch.setattr(review, 'ORIGINAL', tmp_path)
    monkeypatch.setattr(review, 'audit_summary', lambda folder: {'llm1_calls': 0, 'usage_unknown_attempts': 0})
    assert len(review.checked_records(tmp_path)[0]) == 157
    for audit in [{'llm1_calls': 1, 'usage_unknown_attempts': 0}, {'llm1_calls': 0, 'usage_unknown_attempts': 1}]:
        monkeypatch.setattr(review, 'audit_summary', lambda folder, audit=audit: audit)
        with pytest.raises(ValueError, match='Prior paid reading or uncertainty'):
            review.checked_records(tmp_path)


def test_terminal_outcome_and_frozen_proof_are_never_overwritten(tmp_path):
    path = tmp_path/'receipt.private.json'
    review.once(path, {'status': 'completion_stopped'})
    before = path.read_bytes()
    review.once(path, {'status': 'completion_stopped'})
    with pytest.raises(ValueError, match='Frozen follow-up evidence changed'):
        review.once(path, {'status': 'llm1_complete'})
    assert path.read_bytes() == before


@pytest.mark.parametrize('gate', ['untracked', 'dirty'])
def test_uncommitted_runtime_cannot_reach_production_settings(tmp_path, monkeypatch, gate):
    folder = tmp_path/'tmp/followup'
    folder.mkdir(parents=True)
    monkeypatch.setattr(review, 'ROOT', tmp_path)
    monkeypatch.setattr(review, '__file__', str(tmp_path/'scripts/recover_prod2_sector_reviews.py'))
    monkeypatch.setattr(review, 'verify_plan', lambda folder: {'folder': str(folder)})
    def run(argv, **kwargs):
        if 'ls-files' in argv: return SimpleNamespace(returncode=1 if gate == 'untracked' else 0, stdout=b'')
        if 'diff' in argv: return SimpleNamespace(returncode=1, stdout=b'')
        raise AssertionError('Execution must stop before runtime hashes, credentials or provider setup')
    monkeypatch.setattr(review.subprocess, 'run', run)
    monkeypatch.setattr(review, 'production_settings', lambda **kwargs: pytest.fail('No production configuration access is allowed'))
    with pytest.raises(ValueError, match='Commit runtime before execution'):
        review.main(['execute', '--folder', str(folder), '--go'])
