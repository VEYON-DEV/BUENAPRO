"""Isolated, no-network checks for the PROD2 review re-read script.

No provider, SEACE, Redis or production database is touched. The only database
tests are read-only and run against BUENAPRO_TEST_DATABASE_URL when it is set.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2.embedding_runtime import (
    INPUT_POLICY, EmbeddingPreparationReview, config_hash, embedding_permission)
from buenapro_worker.prod2.embedding_store import EmbeddingStore
from buenapro_worker.prod2.model_routing import default_model_routing
from buenapro_worker.prod2.partial_reading import SOURCE_COVERAGE_POLICY, SOURCE_COVERAGE_POLICY_KEY
from buenapro_worker.prod2.selected_control import LimitReached, RunLimits

SCRIPT = Path(__file__).with_name('reread_prod2_reviews.py')
spec = importlib.util.spec_from_file_location('reread_prod2_reviews_under_test', SCRIPT)
reread = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reread)

ROOT = Path(__file__).resolve().parents[1]
V5 = ROOT / 'docs/sources/prod2-embedding-reading.runtime.v5.json'
V6 = ROOT / 'docs/sources/prod2-embedding-reading.runtime.v6.json'
DATABASE_URL = os.environ.get('BUENAPRO_TEST_DATABASE_URL')
needs_db = pytest.mark.skipif(not DATABASE_URL, reason='BUENAPRO_TEST_DATABASE_URL not set')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Provider HTTP is forbidden in this isolated test suite')
    monkeypatch.setattr(httpx.Client, 'post', denied)
    monkeypatch.setattr(httpx.Client, 'get', denied)
    monkeypatch.setattr(httpx.AsyncClient, 'post', denied)
    monkeypatch.setattr(httpx.AsyncClient, 'get', denied)
    reread.STOP.clear()
    yield
    reread.STOP.clear()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def original_manifest(config):
    return {'llm1_input_policy': INPUT_POLICY,
            'embedding_reading': {'config': config, 'config_sha256': config_hash(config)},
            'llm1_visual_review_policy': 'prod2_ocr_text_layout_review_v2',
            'llm1_retrieved_transport_policy': config['reading']['transport_policy'],
            SOURCE_COVERAGE_POLICY_KEY: SOURCE_COVERAGE_POLICY,
            'llm1_model_routing': default_model_routing(),
            'profile_id': 'profile-1'}


def good_outcome(cid):
    # Shaped like the service pilot: no closes_at and no requires_fresh_lifecycle.
    return {'convocation_id': cid, 'description': 'Servicio de software', 'object_type': 'service',
            'accepted': True, 'status': 'needs_review', 'error_type': 'EmbeddingPreparationReview',
            'error_chain': [{'error_type': 'EmbeddingPreparationReview'}], 'detail_hash': 'd' * 64,
            'temporal_state': 'submission_open', 'confirmed_open': True,
            'temporal_observed_at': datetime.now(timezone.utc).isoformat(), 'temporal_reason_codes': [],
            'documents': []}


def good_receipt():
    return {'reason': reread.REVIEW_REASON, 'corpus_hash': 'c' * 64, 'retrieval_hash': 'e' * 64,
            'embedding_cost_usd': 0.0012, 'full_pdf_fallback': False}


def build_case(original, cid, outcome=None, receipt=None, preparations=1):
    case = original / str(cid)
    put(case / 'outcome.private.json', {**good_outcome(cid), **(outcome or {})})
    put(case / 'embedding-preparation-review.private.json', {**good_receipt(), **(receipt or {})})
    for number in range(preparations):
        preparation = case / 'preparation-cache' / ('selected-analysis-%02d' % number)
        put(preparation / 'manifest.private.json', {'max_analysis_bytes': 1000, 'analysis_sha256': 'a' * 64})
        (preparation / 'analysis.pdf').write_bytes(b'%PDF-analysis-' + str(cid).encode())
        (preparation / 'part-0001.pdf').write_bytes(b'%PDF-part')
    put(case / 'embedding-corpus-persistence.private.json', {'corpus_hash': 'c' * 64})
    cache = case / ('embedding-corpus-' + 'f' * 64)
    put(cache / 'corpus.private.json', {'identity': 'kept'})
    put(cache / 'ocr-page-0001.private.json', {'page': 1})


@pytest.fixture
def run(tmp_path, monkeypatch):
    original = tmp_path / 'runs/pilot'
    put(original / 'manifest.private.json', original_manifest(json.loads(V5.read_text())))
    ids = [1051075, 1051079, 1051128]
    for cid in ids:
        build_case(original, cid)
    # The PDF parts are fake bytes: the real byte-level check is exercised by its own module.
    monkeypatch.setattr(reread, '_result', lambda private, manifest, maximum: {
        **manifest, 'pdf_path': private / 'analysis.pdf', 'chunks': []})
    config = tmp_path / 'v6.json'
    config.write_text(V6.read_text())
    return SimpleNamespace(tmp=tmp_path, original=original, folder=tmp_path / 'rereads/pilot-v6',
                           ids=ids, config=config)


def cli(run, mode, ids=None, calls=3, extra=()):
    ids = run.ids if ids is None else ids
    return [mode, '--original', str(run.original), '--ids', *[str(i) for i in ids],
            '--embedding-config', str(run.config), '--folder', str(run.folder),
            '--max-llm1-calls', str(calls), *extra]


def plan_of(run, calls=3):
    plan, rejected = reread.frozen_plan(run.original, run.folder, run.ids, run.config, calls, 6)
    assert rejected == []
    reread.once(run.folder / 'manifest.private.json', plan)
    return plan


def lines_of(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{')]


# --- plan: what is accepted and what is rejected ---------------------------------------


def test_plan_freezes_the_expected_reviews_with_relative_evidence_and_no_paid_extras(run):
    plan, rejected = reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 6)
    assert rejected == []
    assert plan['ids'] == run.ids
    assert [case['convocation_id'] for case in plan['cases']] == run.ids
    assert plan['max_llm1_calls'] == 3 and plan['max_embedding_calls'] == 0
    assert plan['embedding_config'] == json.loads(V6.read_text())
    assert plan['embedding_config_sha256'] == config_hash(json.loads(V6.read_text()))
    assert plan['embedding_config']['retrieval']['context_selection_policy'] == 'prod2_specific_section_ranked_v1'
    assert plan['max_observation_age_seconds'] == 6 * 3600
    for flag in ('no_discovery', 'no_source_http', 'no_redis', 'no_jev', 'no_embedding_purchase', 'no_full_pdf_or_luna'):
        assert plan[flag] is True
    paths = {item['path'] for item in plan['cases'][0]['evidence']}
    assert {'1051075/outcome.private.json', '1051075/embedding-preparation-review.private.json',
            '1051075/embedding-corpus-persistence.private.json'} <= paths
    assert any(path.endswith('selected-analysis-00/analysis.pdf') for path in paths)
    assert any(path.endswith('ocr-page-0001.private.json') for path in paths)
    assert not any(Path(path).is_absolute() for path in paths)
    assert not run.folder.exists(), 'Planning validates; only main writes the manifest'


REJECTIONS = [
    ('finished reading', {'outcome': {'status': 'llm1_complete', 'error_type': None}}, 'not_expected_review'),
    ('other error', {'outcome': {'error_type': 'LimitReached'}}, 'not_expected_review'),
    ('other review reason', {'receipt': {'reason': 'incomplete_source_text'}}, 'not_expected_review'),
    ('full pdf fallback', {'receipt': {'full_pdf_fallback': True}}, 'review_receipt_invalid'),
    ('typed proof differs', {'outcome': {'document_preparation_review': {
        'reason': reread.REVIEW_REASON, 'receipt_sha256': '0' * 64}}}, 'review_receipt_invalid'),
    ('not accepted', {'outcome': {'accepted': False}}, 'not_accepted'),
    ('closed contract', {'outcome': {'temporal_state': 'closed'}}, 'not_open'),
    ('open not confirmed', {'outcome': {'confirmed_open': False}}, 'not_open'),
    ('fresh lifecycle required', {'outcome': {'requires_fresh_lifecycle': True}}, 'fresh_lifecycle_required'),
    ('no detail hash', {'outcome': {'detail_hash': None}}, 'outcome_incomplete'),
    ('other convocation', {'outcome': {'convocation_id': 999}}, 'outcome_identity'),
    ('no preparation', {'preparations': 0}, 'preparation_count'),
    ('two preparations', {'preparations': 2}, 'preparation_count'),
]


@pytest.mark.parametrize('label,change,reason', REJECTIONS, ids=[row[0] for row in REJECTIONS])
def test_plan_rejects_cases_that_are_not_the_expected_review_with_their_reason(run, label, change, reason):
    bad = run.ids[-1]
    shutil.rmtree(run.original / str(bad))
    build_case(run.original, bad, **change)
    plan, rejected = reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 6)
    assert [item['convocation_id'] for item in rejected] == [bad]
    assert rejected[0]['reason'] == reason and rejected[0]['detail']
    assert [case['convocation_id'] for case in plan['cases']] == run.ids[:-1]


def test_plan_rejects_missing_unreadable_and_unpaid_state(run):
    first, second, third = run.ids
    (run.original / str(first) / 'embedding-preparation-review.private.json').unlink()
    put(run.original / str(second) / 'gemini-selected-1.started.private.json',
        {'attempt_id': 'gemini-selected-1', 'model_call_started': True})
    put(run.original / str(third) / 'embedding-uncertain.started.private.json',
        {'attempt_id': 'uncertain', 'status': 'reserved', 'usage_known': False})
    plan, rejected = reread.frozen_plan(run.original, run.folder, run.ids + [4040], run.config, 3, 6)
    assert {item['convocation_id']: item['reason'] for item in rejected} == {
        first: 'unreadable_evidence', second: 'payment_state', third: 'payment_state', 4040: 'case_missing'}
    assert plan['cases'] == []


def test_plan_rejects_a_preparation_that_does_not_match_its_pdf_parts(run, monkeypatch):
    def broken(private, manifest, maximum):
        raise ValueError('hash differs')
    monkeypatch.setattr(reread, '_result', broken)
    plan, rejected = reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 6)
    assert {item['reason'] for item in rejected} == {'preparation_unverified'}


def test_main_plan_with_a_rejected_case_writes_no_manifest_and_exits_2(run, capsys):
    build_case(run.original, run.ids[1], outcome={'status': 'llm1_complete', 'error_type': None})
    assert reread.main(cli(run, '--plan')) == 2
    lines = lines_of(capsys)
    assert [(line['convocation_id'], line['status']) for line in lines[:3]] == [
        (run.ids[0], 'planned'), (run.ids[2], 'planned'), (run.ids[1], 'rejected')]
    assert lines[2]['reason'] == 'not_expected_review'
    assert lines[-1] == {'status': 'not_planned', 'rejected': [run.ids[1]], 'manifest_written': False}
    assert not run.folder.exists()


def test_plan_tells_which_observations_the_execution_would_refuse_as_stale(run, capsys):
    stale = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    build_case(run.original, run.ids[1], outcome={'temporal_observed_at': stale})
    assert reread.main(cli(run, '--plan')) == 0
    flags = {line['convocation_id']: line['within_observation_limit']
             for line in lines_of(capsys) if line['status'] == 'planned' and 'convocation_id' in line}
    assert flags == {run.ids[0]: True, run.ids[1]: False, run.ids[2]: True}
    # A wider age limit is a deliberate, separate plan in its own folder; the first manifest stays as it was.
    run.folder = run.tmp / 'rereads/wider'
    assert reread.main(cli(run, '--plan', extra=['--max-observation-hours', '12'])) == 0
    wider = [line for line in lines_of(capsys) if line['status'] == 'planned' and 'convocation_id' in line]
    assert all(line['within_observation_limit'] for line in wider)
    assert reread.read_private(run.folder / 'manifest.private.json')['max_observation_age_seconds'] == 12 * 3600


def test_main_plan_writes_one_immutable_manifest(run, capsys):
    assert reread.main(cli(run, '--plan')) == 0
    marker = run.folder / 'manifest.private.json'
    first = marker.read_bytes()
    output = lines_of(capsys)
    planned = [line for line in output if line['status'] == 'planned' and 'convocation_id' in line]
    assert [line['convocation_id'] for line in planned] == run.ids
    assert all(line['within_observation_limit'] is True and line['observed_age_hours'] < 0.1 for line in planned)
    summary = output[-1]
    assert summary['status'] == 'planned' and summary['max_embedding_calls'] == 0 and summary['provider_calls'] == 0
    assert reread.main(cli(run, '--plan')) == 0
    assert marker.read_bytes() == first
    with pytest.raises(ValueError, match='Frozen evidence changed'):
        reread.main(cli(run, '--plan', calls=2))
    assert marker.read_bytes() == first


@pytest.mark.parametrize('field,value,message', [
    ('corpus', {'chunk_target_tokens': 1}, "changes 'corpus'"),
    ('embedding', {'price_source': 'other'}, "changes 'embedding'"),
    ('reading', {'evidence_text': 'other'}, "changes 'reading'"),
])
def test_plan_refuses_a_configuration_whose_vectors_would_have_to_be_bought(run, field, value, message):
    config = json.loads(V6.read_text())
    config[field] = {**config[field], **value}
    run.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=message):
        reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 6)


def test_plan_refuses_changed_queries_and_unknown_retrieval_fields(run):
    config = json.loads(V6.read_text())
    config['retrieval']['queries'][0] = {**config['retrieval']['queries'][0], 'query': 'otra consulta'}
    run.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='retrieval queries'):
        reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 6)
    config = json.loads(V6.read_text())
    config['retrieval']['surprise'] = True
    run.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='Unknown retrieval'):
        reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 6)


def test_plan_accepts_the_unchanged_original_configuration(run):
    run.config.write_text(V5.read_text())
    plan, rejected = reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 6)
    assert rejected == [] and plan['embedding_config'] == json.loads(V5.read_text())


def test_plan_argument_and_folder_validation(run):
    with pytest.raises(ValueError, match='between 1 and the number of ids'):
        reread.frozen_plan(run.original, run.folder, run.ids, run.config, 4, 6)
    with pytest.raises(ValueError, match='between 1 and the number of ids'):
        reread.frozen_plan(run.original, run.folder, run.ids, run.config, 0, 6)
    with pytest.raises(ValueError, match='max-observation-hours'):
        reread.frozen_plan(run.original, run.folder, run.ids, run.config, 3, 0)
    with pytest.raises(ValueError, match='outside the original'):
        reread.frozen_plan(run.original, run.original / 'inside', run.ids, run.config, 3, 6)
    stray = run.tmp / 'stray'
    put(stray / 'something.json', {})
    with pytest.raises(ValueError, match='new or own a frozen manifest'):
        reread.frozen_plan(run.original, stray, run.ids, run.config, 3, 6)
    for bad in (['1', '1'], ['0'], ['-5'], []):
        with pytest.raises(ValueError):
            reread.parse_ids(bad)
    assert reread.parse_ids(['1,2', '3']) == [1, 2, 3]


# --- execute needs the plan and unchanged evidence ----------------------------------------


def fail_if_reached(monkeypatch):
    def reached(*args, **kwargs):
        raise AssertionError('Execution must stop before settings, database or providers')
    monkeypatch.setattr(reread, 'Settings', reached)
    monkeypatch.setattr(reread.psycopg, 'connect', reached)
    monkeypatch.setattr(reread, 'read_case', reached)


def test_execute_without_a_plan_fails_before_any_database_or_provider_access(run, monkeypatch):
    fail_if_reached(monkeypatch)
    with pytest.raises(ValueError, match='run --plan first'):
        reread.main(cli(run, '--execute'))
    assert not run.folder.exists()


def widen_context_maximum(path):
    config = json.loads(path.read_text())
    config['retrieval']['context_maximum_tokens'] = 46000
    path.write_text(json.dumps(config))


MUTATIONS = [
    ('original outcome', lambda run: (run.original / '1051075/outcome.private.json').write_bytes(
        (run.original / '1051075/outcome.private.json').read_bytes() + b'\n')),
    ('review receipt', lambda run: (run.original / '1051075/embedding-preparation-review.private.json').write_bytes(
        b'{"changed": true}')),
    ('prepared pdf', lambda run: next((run.original / '1051079').glob('preparation-cache/*/analysis.pdf')).write_bytes(b'x')),
    ('preparation manifest', lambda run: next((run.original / '1051079').glob('preparation-cache/*/manifest.private.json')).write_text('{}')),
    ('ocr cache page', lambda run: next((run.original / '1051128').glob('embedding-corpus-*/ocr-page-0001.private.json')).write_text('{}')),
    ('corpus persistence', lambda run: (run.original / '1051128/embedding-corpus-persistence.private.json').write_text('{}')),
    ('original manifest', lambda run: (run.original / 'manifest.private.json').write_bytes(
        (run.original / 'manifest.private.json').read_bytes() + b'\n')),
    ('embedding config', lambda run: widen_context_maximum(run.config)),
]


@pytest.mark.parametrize('label,mutate', MUTATIONS, ids=[row[0] for row in MUTATIONS])
def test_the_manifest_detects_any_changed_evidence(run, capsys, label, mutate):
    assert reread.main(cli(run, '--plan')) == 0
    args = SimpleNamespace(original=run.original, ids=run.ids, embedding_config=run.config, folder=run.folder,
                           max_llm1_calls=3, max_observation_hours=6)
    assert reread.verify_plan(args)['ids'] == run.ids
    mutate(run)
    with pytest.raises(ValueError, match='changed'):
        reread.verify_plan(args)


def test_the_manifest_detects_a_case_that_stopped_being_a_valid_review(run):
    assert reread.main(cli(run, '--plan')) == 0
    args = SimpleNamespace(original=run.original, ids=run.ids, embedding_config=run.config, folder=run.folder,
                           max_llm1_calls=3, max_observation_hours=6)
    (run.original / '1051128/embedding-preparation-review.private.json').unlink()
    with pytest.raises(ValueError, match='changed'):
        reread.verify_plan(args)


def test_execute_must_use_the_arguments_that_were_planned(run, monkeypatch):
    assert reread.main(cli(run, '--plan')) == 0
    fail_if_reached(monkeypatch)
    for argv in (cli(run, '--execute', calls=2), cli(run, '--execute', ids=run.ids[:2], calls=2),
                 cli(run, '--execute', extra=['--max-observation-hours', '48'])):
        with pytest.raises(ValueError, match='changed'):
            reread.main(argv)


# --- the lifecycle rule: closed contracts are never read ---------------------------------


def pilot_record(**change):
    return {'confirmed_open': True, 'temporal_state': 'submission_open', 'detail_hash': 'd' * 64,
            'temporal_observed_at': datetime.now(timezone.utc).isoformat(),
            'description': 'Servicio de software', **change}


def refusal(record, now, deadline, age=6 * 3600):
    with pytest.raises(reread.Refused) as error:
        reread.validate_lifecycle(record, now, deadline, max_age_seconds=age)
    return error.value.reason


def test_lifecycle_excludes_closed_and_stale_contracts_and_keeps_the_sector_rules():
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(days=1)
    reread.validate_lifecycle(pilot_record(), now, deadline, max_age_seconds=6 * 3600)
    for change in [{'confirmed_open': False}, {'confirmed_open': None}, {'temporal_state': 'closed'},
                   {'temporal_state': 'unknown'}, {'temporal_state': 'adjudicado'}, {'requires_fresh_lifecycle': True}]:
        assert refusal(pilot_record(**change), now, deadline) in {'not_open', 'fresh_lifecycle_required'}
    assert refusal(pilot_record(temporal_observed_at=(now - timedelta(hours=7)).isoformat()),
                   now, deadline) == 'stale_observation'
    assert refusal(pilot_record(temporal_observed_at=(now + timedelta(minutes=2)).isoformat()),
                   now, deadline) == 'stale_observation'
    assert refusal(pilot_record(temporal_observed_at='2026-10-27T12:00:00'), now, deadline) == 'timestamp_invalid'
    assert refusal(pilot_record(temporal_observed_at=None), now, deadline) == 'timestamp_invalid'
    assert refusal(pilot_record(), now, now - timedelta(seconds=1)) == 'closed'
    assert refusal(pilot_record(), now, now) == 'closed'
    assert refusal(pilot_record(), now, None) == 'no_deadline'
    assert refusal(pilot_record(), now, datetime(2026, 12, 1)) == 'no_deadline'


def test_only_the_observation_age_can_be_widened_never_the_deadline():
    now = datetime.now(timezone.utc)
    old = pilot_record(temporal_observed_at=(now - timedelta(hours=20)).isoformat())
    assert refusal(old, now, now + timedelta(days=1)) == 'stale_observation'
    reread.validate_lifecycle(old, now, now + timedelta(days=1), max_age_seconds=24 * 3600)
    assert refusal(old, now, now - timedelta(seconds=1), age=24 * 3600) == 'closed'
    assert refusal(pilot_record(temporal_state='closed'), now, now + timedelta(days=1), age=24 * 3600) == 'not_open'


# --- pg_preflight: existing readings and historical payments are preserved ------------------


class Reply:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class FakePG:
    def __init__(self, record, deadline, prior=(), current=(), detail_hash=None):
        self.record, self.deadline = record, deadline
        self.prior, self.current = list(prior), list(current)
        self.detail_hash = detail_hash or record['detail_hash']
        self.statements = []

    def cursor(self):
        return nullcontext(self)

    def execute(self, statement, params):
        self.statements.append((statement, params))
        assert statement.lstrip().startswith('SELECT'), 'Preflight must remain read-only'
        if 'SELECT d.detail_hash' in statement:
            return Reply({'detail_hash': self.detail_hash, 'proposals_closes_at': self.deadline})
        if 'prod2_document_extractions' in statement:
            return Reply(self.current)
        if 'prod2_evaluations' in statement:
            return Reply(self.prior)
        raise AssertionError('Unexpected preflight query')


def preflight_plan(tmp_path, hours=6):
    return {'folder': str(tmp_path), 'max_observation_age_seconds': hours * 3600}


def test_preflight_takes_the_deadline_from_postgres_for_pilot_outcomes(tmp_path):
    record = pilot_record()
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    pg = FakePG(record, deadline)
    reread.pg_preflight(pg, preflight_plan(tmp_path), 1, record)
    assert len(pg.statements) == 3
    for patch, reason in [({'deadline': None}, 'no_deadline'),
                          ({'deadline': datetime.now(timezone.utc) - timedelta(minutes=1)}, 'closed'),
                          ({'detail_hash': 'x' * 64}, 'detail_changed')]:
        values = {'deadline': deadline, **patch}
        with pytest.raises(reread.Refused) as error:
            reread.pg_preflight(FakePG(record, **values), preflight_plan(tmp_path), 1, record)
        assert error.value.reason == reason


def test_preflight_requires_an_outcome_deadline_to_match_postgres(tmp_path):
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    record = pilot_record(closes_at=deadline.isoformat())
    reread.pg_preflight(FakePG(record, deadline), preflight_plan(tmp_path), 1, record)
    other = pilot_record(closes_at=(deadline + timedelta(seconds=1)).isoformat())
    with pytest.raises(reread.Refused) as error:
        reread.pg_preflight(FakePG(other, deadline), preflight_plan(tmp_path), 1, other)
    assert error.value.reason == 'deadline_changed'


def test_preflight_preserves_current_readings_and_never_repays_a_historical_attempt(tmp_path):
    record = pilot_record()
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    with pytest.raises(reread.Refused, match='existing reading'):
        reread.pg_preflight(FakePG(record, deadline, current=[{'id': 1}]), preflight_plan(tmp_path), 1, record)
    request = 'r' * 64
    claim = {'id': 42, 'request_hash': request, 'claim_token': 'owned-token'}
    put(tmp_path / '1' / ('gemini_extraction-' + request + '.claim.private.json'), claim)
    own = {**claim, 'status': 'running'}
    reread.pg_preflight(FakePG(record, deadline, prior=[own]), preflight_plan(tmp_path), 1, record, request)
    for status in ('running', 'done', 'uncertain'):
        with pytest.raises(reread.Refused, match='Historical paid attempt'):
            reread.pg_preflight(FakePG(record, deadline, prior=[{**own, 'status': status}]),
                                preflight_plan(tmp_path), 1, record)
    for patch in [{'claim_token': 'foreign'}, {'id': 99}, {'request_hash': 'other'}, {'status': 'uncertain'}, {'status': 'failed'}]:
        with pytest.raises(reread.Refused, match='Historical paid attempt'):
            reread.pg_preflight(FakePG(record, deadline, prior=[{**own, **patch}]),
                                preflight_plan(tmp_path), 1, record, request)


def test_preflight_checks_the_paid_selector_receipt_with_the_original_description(tmp_path, monkeypatch):
    from buenapro_worker.prod2 import llm1_completion
    seen = []
    monkeypatch.setattr(llm1_completion, '_current_selection_receipt',
                        lambda cursor, context, description: seen.append((context, description)))
    record = pilot_record()
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    reread.pg_preflight(FakePG(record, deadline), preflight_plan(tmp_path), 1, record, selection_context={'k': 1})
    assert seen == [({'k': 1}, 'Servicio de software')]


# --- no embedding is ever bought ------------------------------------------------------------


def fresh_limits(calls=3):
    return RunLimits(max_accepted=3, max_candidates=3, max_jev_calls=0, max_llm1_calls=calls,
                     max_embedding_calls=reread.NO_EMBEDDING_CALLS, model_workers=2)


def test_the_retrieval_stage_cannot_reserve_an_embedding_or_llm1_request():
    limits = fresh_limits()
    guard = object()
    stage = reread.embedding_free_limits(limits, guard)
    assert reread.NO_EMBEDDING_CALLS == 0
    assert stage.maximum['embedding'] == 0 and stage.maximum['llm1'] == 0 and stage.maximum['jev'] == 0
    assert stage.model_slots is limits.model_slots and stage.handoff_guard is guard
    with pytest.raises(LimitReached):
        with embedding_permission(stage):
            pytest.fail('A permit to buy an embedding was granted')
    with pytest.raises(LimitReached):
        stage.reserve_request('llm1')
    assert limits.maximum['embedding'] == 0
    with pytest.raises(LimitReached):
        limits.reserve_request('embedding')


class Owner:
    closed = broken = False

    def __init__(self, locked=True, held=True):
        self.reply = {'locked': locked, 'held': held}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        assert 'pg_try_advisory_lock' in statement or 'pg_locks' in statement
        return Reply(self.reply)


SETTINGS = SimpleNamespace(database_url='unused', gemini_model='gemini-3.1-flash-lite')


def test_execute_hands_every_stage_a_zero_embedding_ceiling(run, monkeypatch):
    plan = plan_of(run)
    monkeypatch.setattr(reread.psycopg, 'connect', lambda *args, **kwargs: Owner())
    seen = []

    def fake_read(settings, plan, cid, limits, guard):
        seen.append((limits.maximum.copy(), limits.calls.copy(), limits.handoff_guard is guard))
        value = {'convocation_id': cid, 'status': 'llm1_complete'}
        reread.once(Path(plan['folder']) / str(cid) / 'outcome.private.json', value)
        return value

    monkeypatch.setattr(reread, 'read_case', fake_read)
    report = reread.execute(plan, SETTINGS)
    assert len(seen) == 3
    for maximum, calls, guarded in seen:
        assert maximum == {'jev': 0, 'llm1': 3, 'embedding': 0}
        assert calls['embedding'] == 0 and guarded is True
    assert report['embedding_calls'] == 0 and report['llm1_complete'] == 3


def wire_read_case(run, monkeypatch, retrieval=None):
    """Replace every provider-facing collaborator of read_case; return what it was called with."""
    plan = plan_of(run)
    pdf = run.tmp / 'analysis.pdf'
    pdf.write_bytes(b'%PDF-fake')
    prepared = {'pdf_path': pdf, 'page_count': 3, 'selection_context': {'sel': 1}, 'document_scope': 'selected',
                'document_set': [{'document_uuid': 'uuid-1', 'original_sha256': 'a' * 64,
                                  'source_url': 'https://official.invalid/doc'}],
                'document_set_hash': 'b' * 64, 'document_set_identity': {}, 'llm1_input': {'input': 'retrieved'},
                'source_document_manifest': {}, 'analysis_sha256': 'a' * 64}
    seen = SimpleNamespace(retrieval=None, paid=None, resolved=None, preflights=[], persisted=None)
    monkeypatch.setattr(reread, 'prepare_case', lambda plan, cid: dict(prepared))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(reread.psycopg, 'connect', lambda *args, **kwargs: Connection())
    monkeypatch.setattr(reread, 'pg_preflight', lambda conn, plan, cid, record, request_hash=None, selection_context=None:
                        seen.preflights.append((cid, request_hash, selection_context)))

    def default_retrieval(conn, settings, prepared, folder, limits, config, **kwargs):
        seen.retrieval = SimpleNamespace(limits=limits, config=config, kwargs=kwargs, folder=folder)
        put(Path(folder) / 'embedding-stage.private.json', {
            'attempt_ids': [], 'new_token_count': 0, 'known_cost_usd': 0.0, 'cached_count': 40, 'context_tokens': 41234})
        return {**prepared, 'retrieval_input_result': {'context_tokens': 41234}}

    monkeypatch.setattr(reread, 'prepare_retrieved_input', retrieval or default_retrieval)
    monkeypatch.setattr(reread, 'prepare_input', lambda prepared, folder: (
        prepared, {'context_tokens': 41234, 'reasons': ['context_trimmed_by_relevance']}))

    def resolve(manifest, pages, base):
        seen.resolved = (manifest, pages, base)
        return 'gemini-2.5-flash-lite', {'policy_version': 'routing', 'page_count': pages}

    monkeypatch.setattr(reread, 'resolve_llm1_model', resolve)
    monkeypatch.setattr(reread, 'extraction_request_hash', lambda *args, **kwargs: 'r' * 64)

    def paid(*args, **kwargs):
        seen.paid = SimpleNamespace(args=args, **kwargs)
        return 7, {'requires_human_review': False, 'cost_usd': 0.0123, 'facets': [1, 2, 3]}, False, 'r' * 64, {}

    monkeypatch.setattr(reread, 'paid_selected_read', paid)

    def persist(conn, **kwargs):
        seen.persisted = kwargs
        return {'extraction_id': 11}

    monkeypatch.setattr(reread, 'persist_completed_llm1', persist)

    class Settings:
        database_url = 'unused'
        gemini_model = 'gemini-3.1-flash-lite'

        def model_copy(self, update):
            return SimpleNamespace(database_url='unused', **update)

    guard = SimpleNamespace(before_request=lambda: None, observe_attempt=lambda attempt: None)
    return plan, seen, Settings(), guard


def test_read_case_reuses_stored_vectors_with_the_new_config_and_never_buys_an_embedding(run, monkeypatch):
    plan, seen, settings, guard = wire_read_case(run, monkeypatch)
    limits = fresh_limits()
    limits.handoff_guard = guard
    cid = run.ids[0]
    result = reread.read_case(settings, plan, cid, limits, guard)
    assert seen.retrieval.limits.maximum['embedding'] == 0 and seen.retrieval.limits.maximum['llm1'] == 0
    assert seen.retrieval.config == plan['embedding_config']
    assert seen.retrieval.config['retrieval']['context_selection_policy'] == 'prod2_specific_section_ranked_v1'
    assert seen.retrieval.kwargs['memoize_expansion'] is True and seen.retrieval.kwargs['convocation_id'] == cid
    assert Path(seen.retrieval.folder) == Path(plan['folder']) / str(cid)
    assert seen.paid.limits is limits and limits.maximum['embedding'] == 0 and limits.maximum['llm1'] == 3
    assert seen.paid.fallback_config is None
    assert seen.paid.generation_options == {'after_attempt': guard.observe_attempt}
    assert seen.paid.profile_id == 'profile-1' and seen.paid.object_type == 'service'
    assert seen.paid.anchor == 'uuid-1' and seen.paid.anchor_sha == 'a' * 64
    assert seen.resolved[0]['profile_id'] == 'profile-1' and 'llm1_model_routing' in seen.resolved[0]
    assert seen.resolved[1:] == (3, 'gemini-3.1-flash-lite')
    assert seen.persisted['convocation_id'] == cid and seen.persisted['extraction']['allowed_models'] == ['gemini-2.5-flash-lite']
    assert result['status'] == 'llm1_complete' and result['facet_count'] == 3
    assert result['context_tokens'] == 41234 and result['context_reasons'] == ['context_trimmed_by_relevance']
    assert result['llm1_cost_usd'] == 0.0123 and result['llm1_model'] == 'gemini-2.5-flash-lite'
    assert result['original_outcome_sha256'] == plan['cases'][0]['original_outcome_sha256']
    assert result['embedding_config_sha256'] == plan['embedding_config_sha256']
    assert result['embedding_stage']['attempt_ids'] == [] and result['embedding_stage']['known_cost_usd'] == 0
    assert 'error_type' not in result and 'error_chain' not in result
    stored = reread.read_private(Path(plan['folder']) / str(cid) / 'outcome.private.json')
    assert stored == json.loads(json.dumps(result, default=str))
    assert reread.case_line(result) == {'convocation_id': cid, 'status': 'llm1_complete', 'context_tokens': 41234,
                                        'facet_count': 3, 'cost_usd': 0.0123, 'model': 'gemini-2.5-flash-lite'}
    # The preflight is repeated at the provider boundary with the exact request.
    request_checked = seen.paid.before_request_factory(lambda: None)
    request_checked()
    assert seen.preflights[-1][:2] == (cid, 'r' * 64)


def test_missing_vectors_fail_the_case_without_paying_anything(run, monkeypatch):
    def library_like(conn, settings, prepared, folder, limits, config, **kwargs):
        # Exactly what ensure_vectors does before it would claim or send a missing batch.
        with embedding_permission(limits, stop_requested=kwargs['stop_requested']):
            pytest.fail('A permit to buy an embedding was granted')

    plan, seen, settings, guard = wire_read_case(run, monkeypatch, retrieval=library_like)
    cid = run.ids[0]
    with pytest.raises(reread.MissingVectors):
        reread.read_case(settings, plan, cid, fresh_limits(), guard)
    assert seen.paid is None and seen.persisted is None
    record, existed = reread.run_case(settings, plan, cid, fresh_limits(), guard)
    assert existed is False and record['status'] == 'failed' and record['reason'] == 'missing_vectors'
    assert [item['error_type'] for item in record['error_chain']] == ['MissingVectors', 'LimitReached']
    assert reread.read_private(Path(plan['folder']) / str(cid) / 'outcome.private.json') == record
    assert seen.paid is None


def test_an_embedding_that_was_somehow_bought_blocks_the_llm1_request(run, monkeypatch):
    def bought(conn, settings, prepared, folder, limits, config, **kwargs):
        put(Path(folder) / 'embedding-stage.private.json', {'attempt_ids': ['a1'], 'new_token_count': 5, 'known_cost_usd': 0.1})
        return prepared

    plan, seen, settings, guard = wire_read_case(run, monkeypatch, retrieval=bought)
    with pytest.raises(RuntimeError, match='must never buy embeddings'):
        reread.read_case(settings, plan, run.ids[0], fresh_limits(), guard)
    assert seen.paid is None


def test_a_review_that_stays_a_review_is_recorded_with_its_proof_and_costs(run, monkeypatch):
    def still_too_large(conn, settings, prepared, folder, limits, config, **kwargs):
        receipt = {'reason': reread.REVIEW_REASON, 'corpus_hash': 'c' * 64, 'retrieval_hash': 'd' * 64,
                   'embedding_cost_usd': 0, 'full_pdf_fallback': False}
        put(Path(folder) / 'embedding-preparation-review.private.json', receipt)
        raise EmbeddingPreparationReview('Retrieved context exceeds the economical reading contract')

    plan, seen, settings, guard = wire_read_case(run, monkeypatch, retrieval=still_too_large)
    record, existed = reread.run_case(settings, plan, run.ids[0], fresh_limits(), guard)
    assert record['status'] == 'needs_review' and record['error_type'] == 'EmbeddingPreparationReview'
    assert record['document_preparation_review']['reason'] == reread.REVIEW_REASON
    assert record['no_automatic_retry'] is True and record['llm1_cost_usd'] == 0
    assert seen.paid is None


# --- one case never stops the others; outcomes are never repeated --------------------------------


def test_a_failing_case_records_its_outcome_and_the_others_still_run(run, monkeypatch, capsys):
    assert reread.main(cli(run, '--plan')) == 0
    capsys.readouterr()
    monkeypatch.setattr(reread, 'Settings', lambda **kwargs: SETTINGS)
    monkeypatch.setattr(reread.psycopg, 'connect', lambda *args, **kwargs: Owner())
    first, second, third = run.ids

    def fake_read(settings, plan, cid, limits, guard):
        guard.before_request()
        if cid == first:
            raise ValueError('local failure')
        if cid == second:
            raise EmbeddingPreparationReview('context still too large')
        value = {'convocation_id': cid, 'status': 'llm1_complete', 'context_tokens': 30100, 'facet_count': 12,
                 'llm1_cost_usd': 0.02, 'llm1_model': 'gemini-3.1-flash-lite'}
        reread.once(Path(plan['folder']) / str(cid) / 'outcome.private.json', value)
        return value

    monkeypatch.setattr(reread, 'read_case', fake_read)
    assert reread.main(cli(run, '--execute')) == 1
    output = lines_of(capsys)
    by_id = {line['convocation_id']: line for line in output if 'convocation_id' in line}
    assert set(by_id) == set(run.ids)
    assert {key: by_id[third][key] for key in ('status', 'context_tokens', 'facet_count', 'cost_usd', 'model')} == {
        'status': 'llm1_complete', 'context_tokens': 30100, 'facet_count': 12, 'cost_usd': 0.02,
        'model': 'gemini-3.1-flash-lite'}
    assert by_id[first]['status'] == 'failed' and by_id[first]['error_type'] == 'ValueError'
    assert by_id[second]['status'] == 'needs_review' and by_id[second]['error_type'] == 'EmbeddingPreparationReview'
    for line in by_id.values():
        assert {'convocation_id', 'status', 'context_tokens', 'facet_count', 'cost_usd', 'model'} <= set(line)
    summary = output[-1]
    assert (summary['llm1_complete'], summary['needs_review'], summary['failed'], summary['pending']) == (1, 1, 1, [])
    assert summary['embedding_calls'] == 0 and summary['source_calls'] == 0 and summary['dispatch_stopped'] is False
    stored = reread.read_private(run.folder / str(first) / 'outcome.private.json')
    assert stored['status'] == 'failed' and stored['no_automatic_retry'] is True
    assert stored['original_outcome_sha256'] == reread.sha(run.original / str(first) / 'outcome.private.json')
    assert not (run.folder / 'handoff-guards/dispatch-stop.private.json').exists()
    assert len(list(run.folder.glob('report-*.private.json'))) == 1
    assert (run.folder / 'runtime-bindings.private.json').exists()


def test_an_existing_outcome_is_returned_and_never_read_again(run, monkeypatch):
    plan = plan_of(run)
    existing = {'convocation_id': run.ids[0], 'status': 'failed', 'error_type': 'ValueError', 'no_automatic_retry': True}
    put(run.folder / str(run.ids[0]) / 'outcome.private.json', existing)

    def reached(*args, **kwargs):
        raise AssertionError('An existing outcome must not be prepared, preflighted or paid again')

    for name in ('prepare_case', 'pg_preflight', 'prepare_retrieved_input', 'paid_selected_read'):
        monkeypatch.setattr(reread, name, reached)
    monkeypatch.setattr(reread.psycopg, 'connect', reached)
    assert reread.read_case(None, plan, run.ids[0], None, None) == existing


def test_a_second_execute_repeats_no_case_that_already_has_an_outcome(run, monkeypatch, capsys):
    assert reread.main(cli(run, '--plan')) == 0
    monkeypatch.setattr(reread, 'Settings', lambda **kwargs: SETTINGS)
    monkeypatch.setattr(reread.psycopg, 'connect', lambda *args, **kwargs: Owner())
    attempts = []

    def prepare(plan, cid):
        attempts.append(cid)
        raise ValueError('cannot prepare')

    monkeypatch.setattr(reread, 'prepare_case', prepare)
    assert reread.main(cli(run, '--execute')) == 1
    assert sorted(attempts) == sorted(run.ids)
    before = {cid: (run.folder / str(cid) / 'outcome.private.json').read_bytes() for cid in run.ids}
    capsys.readouterr()
    assert reread.main(cli(run, '--execute')) == 1
    assert sorted(attempts) == sorted(run.ids), 'No terminal outcome may be retried'
    assert {cid: (run.folder / str(cid) / 'outcome.private.json').read_bytes() for cid in run.ids} == before
    summary = lines_of(capsys)[-1]
    assert summary['already_recorded'] == sorted(run.ids) and summary['failed'] == 3
    assert len(list(run.folder.glob('report-*.private.json'))) == 2


def test_a_case_stopped_before_dispatch_stays_pending_and_can_run_later(run, monkeypatch, capsys):
    plan = plan_of(run)
    monkeypatch.setattr(reread.psycopg, 'connect', lambda *args, **kwargs: Owner())
    state = {'ceiling': True}
    second = run.ids[1]

    def fake_read(settings, plan, cid, limits, guard):
        if cid == second and state['ceiling']:
            raise LimitReached('Configured provider-call ceiling reached')
        value = {'convocation_id': cid, 'status': 'llm1_complete'}
        reread.once(Path(plan['folder']) / str(cid) / 'outcome.private.json', value)
        return value

    monkeypatch.setattr(reread, 'read_case', fake_read)
    report = reread.execute(plan, SETTINGS)
    assert report['pending'] == [second] and report['llm1_complete'] == 2
    assert not (run.folder / str(second) / 'outcome.private.json').exists()
    state['ceiling'] = False
    report = reread.execute(plan, SETTINGS)
    assert report['pending'] == [] and report['llm1_complete'] == 3
    assert sorted(report['already_recorded']) == sorted([run.ids[0], run.ids[2]])


def test_an_uncertain_paid_attempt_stops_every_remaining_case(run, monkeypatch):
    plan = plan_of(run)
    owner = Owner()
    guard = reread.OwnedReviewGuard(run.folder, owner=owner, stop=reread.STOP, max_candidates=3,
                                    max_new_documents=0, binding={'version': 'test'})
    limits = fresh_limits()
    limits.handoff_guard = guard
    first, second = run.ids[:2]

    def uncertain(settings, plan, cid, limits, guard):
        guard.before_request()
        guard.observe_attempt({'provider': 'gemini', 'request_sent': True, 'model_call_started': True,
                               'usage_known': False, 'attempt_id': 'attempt-1'})
        raise ValueError('response lost')

    monkeypatch.setattr(reread, 'read_case', uncertain)
    record, _ = reread.run_case(SETTINGS, plan, first, limits, guard)
    assert record['status'] == 'failed'
    record, _ = reread.run_case(SETTINGS, plan, second, limits, guard)
    assert record['status'] == 'pending' and record['error_type'] == 'LimitReached'
    assert not (run.folder / str(second) / 'outcome.private.json').exists()
    assert (run.folder / 'handoff-guards/dispatch-stop.private.json').exists()
    assert reread.STOP.is_set()


def test_execute_refuses_a_busy_coordinator_and_uncertain_payments(run, monkeypatch):
    plan = plan_of(run)
    monkeypatch.setattr(reread.psycopg, 'connect', lambda *args, **kwargs: Owner(locked=False))
    monkeypatch.setattr(reread, 'read_case', lambda *args: pytest.fail('No case may start without the lock'))
    with pytest.raises(ValueError, match='Another coordinator owns PROD2'):
        reread.execute(plan, SETTINGS)
    put(run.folder / str(run.ids[0]) / 'embedding-x.started.private.json',
        {'attempt_id': 'x', 'status': 'reserved', 'usage_known': False})
    with pytest.raises(ValueError, match='uncertain payments or embedding attempts'):
        reread.execute(plan, SETTINGS)


def test_the_owned_guard_stops_when_the_coordinator_lock_is_lost(tmp_path):
    guard = reread.OwnedReviewGuard(tmp_path, owner=Owner(), stop=reread.STOP, max_candidates=1,
                                    max_new_documents=0, binding={'version': 'test'})
    guard.before_request()
    guard.owner.reply = {'locked': True, 'held': False}
    with pytest.raises(ValueError, match='no longer owns its lock'):
        guard.before_request()
    guard.owner.broken = True
    with pytest.raises(ValueError, match='connection was lost'):
        guard.before_request()


def test_the_execution_receipt_is_bound_to_this_script_and_never_rewritten(run, monkeypatch):
    plan = plan_of(run)
    monkeypatch.setattr(reread.psycopg, 'connect', lambda *args, **kwargs: Owner())
    monkeypatch.setattr(reread, 'read_case', lambda settings, plan, cid, limits, guard: {
        'convocation_id': cid, 'status': 'llm1_complete'})
    reread.execute(plan, SETTINGS)
    receipt = run.folder / 'runtime-bindings.private.json'
    assert reread.read_private(receipt)['script_sha256'] == reread.file_digest(SCRIPT)
    reread.execute(plan, SETTINGS)
    put(receipt, {**reread.read_private(receipt), 'script_sha256': '0' * 64})
    with pytest.raises(ValueError, match='Frozen evidence changed'):
        reread.execute(plan, SETTINGS)


# --- packaging and the real schema ---------------------------------------------------------------


def test_the_prod2_image_ships_the_script_next_to_the_runner_it_imports():
    dockerfile = (ROOT / 'infra/docker/prod2.Dockerfile').read_text()
    copy = next(line for line in dockerfile.splitlines() if line.startswith('COPY scripts/'))
    assert 'scripts/reread_prod2_reviews.py' in copy and 'scripts/run_prod2_selected.py' in copy


@needs_db
def test_preflight_queries_are_valid_for_the_real_schema_and_read_only():
    with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        for statement in (reread.SOURCE_SQL, reread.CURRENT_READING_SQL, reread.PRIOR_ATTEMPTS_SQL):
            conn.execute('EXPLAIN ' + statement.replace('%s', '1')).fetchall()
        record = pilot_record()
        with pytest.raises(reread.Refused) as error:
            reread.pg_preflight(conn, preflight_plan(Path('/nonexistent')), 2_000_000_000, record)
        assert error.value.reason == 'detail_changed'


@needs_db
def test_the_embedding_store_cannot_claim_or_send_a_missing_vector_without_budget(tmp_path):
    class Client:
        model, dimensions, price_per_million = 'text-embedding-3-small', 1536, 0.02

        def embed(self, *args, **kwargs):
            pytest.fail('A missing vector must never reach the embedding provider')

        def close(self):
            return None

    stage = reread.embedding_free_limits(fresh_limits(), SimpleNamespace())
    with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        count = 'SELECT (SELECT count(*) FROM prod2_embedding_attempts) AS attempts, ' \
                '(SELECT count(*) FROM prod2_embedding_vectors) AS vectors'
        before = conn.execute(count).fetchone()
        store = EmbeddingStore(conn, model='text-embedding-3-small', dimensions=1536, price_per_million=0.02)
        with pytest.raises(LimitReached):
            store.ensure_vectors(['reread-test-never-stored-' + uuid4().hex], client=Client(), folder=tmp_path,
                                 before_request_factory=lambda request_hash: embedding_permission(stage))
        assert conn.execute(count).fetchone() == before
    assert list(tmp_path.iterdir()) == []
