"""Isolated, no-network regression checks for the nine-case explicit follow-up."""
from __future__ import annotations
from copy import deepcopy
from contextlib import nullcontext
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
    def cursor(self):return nullcontext(self)
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


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(review, 'ROOT', tmp_path)
    original = tmp_path/'original'
    monkeypatch.setattr(review, 'ORIGINAL', original)
    folder = tmp_path/'tmp/prod2-sector-reading-completion-20261020/run-04'
    plan = {'folder': str(folder), 'ids': list(review.IDS)}
    put(folder/'manifest.private.json', plan)
    put(folder/'runtime-bindings.private.json', {'scripts/recover_prod2_sector_reviews.py': 'a'*64})
    outcomes = []
    costs = {'attempts': 2, 'llm1_calls': 2, 'embedding_attempts': 0, 'embedding_calls': 0,
        'jev_calls': 0, 'usage_unknown_attempts': 0, 'known_cost_usd': .0353206}
    per_case = {}
    for cid in review.IDS:
        value = {'convocation_id': cid, 'status': 'completion_stopped',
            'error_type': review.EXCLUDED_PAID.get(cid, 'LimitReached'), 'no_automatic_retry': True}
        outcomes.append(value)
        put(folder/str(cid)/'outcome.private.json', value)
        put(original/str(cid)/'outcome.private.json', fresh_record())
        per_case[cid] = {'attempts': int(cid in review.EXCLUDED_PAID),
            'llm1_calls': int(cid in review.EXCLUDED_PAID), 'usage_unknown_attempts': 0}
    put(folder/'report.private.json', {'ids': list(review.IDS), 'records': outcomes,
        'llm1_complete': 0, 'costs': costs})
    put(folder/'handoff-guards/dispatch-stop.private.json', {'reason': 'unexpected_local_exception',
        'error_type': 'ValidationError', 'convocation_id': 1048156, 'phase': 'explicit_sector_review_completion'})
    monkeypatch.setattr(review, 'audit_summary', lambda path: deepcopy(
        per_case[int(Path(path).name)] if Path(path).name.isdigit() else costs))
    monkeypatch.setattr(review,'prepare_case',lambda plan,cid:{'selection_context':{'convocation_id':cid}})
    from buenapro_worker.prod2 import llm1_completion
    monkeypatch.setattr(llm1_completion,'_current_selection_receipt',lambda *args:None)
    return SimpleNamespace(folder=folder, plan=plan, costs=costs, per_case=per_case)


def test_continuation_authorization_binds_exact_seven_and_preserves_all_old_bytes(continuation):
    c = continuation
    before = {p: p.read_bytes() for p in c.folder.rglob('*.private.json')}
    auth = review.continuation_baseline(c.folder, c.plan)
    assert auth['ids'] == [1048369,1048448,1048462,1048805,1049089,1049583,1050007]
    assert auth['excluded_paid_ids'] == [1047330,1048156]
    assert auth['max_new_llm1_calls'] == 7 and auth['max_total_llm1_calls'] == 9
    assert auth['model_workers'] == 2 and auth['max_new_embedding_calls'] == 10
    assert auth['new_embedding_allowed_ids'] == [1048805]
    assert auth['generation_options'] == {'max_output_tokens':65536,'timeout_ms':300000}
    assert auth['no_luna_or_model_fallback'] is True
    review.once(c.folder/review.AUTHORIZATION, auth)
    assert review.verify_continuation(c.folder, c.plan) == auth
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize('file', ['manifest.private.json','report.private.json','runtime-bindings.private.json',
    'handoff-guards/dispatch-stop.private.json','1048369/outcome.private.json','1047330/outcome.private.json'])
def test_continuation_any_baseline_bytes_change_fails_closed(continuation, file):
    c = continuation
    review.once(c.folder/review.AUTHORIZATION, review.continuation_baseline(c.folder,c.plan))
    path = c.folder/file
    path.write_bytes(path.read_bytes()+b'\n')  # Same JSON value, different frozen evidence.
    with pytest.raises(ValueError, match='baseline changed'):
        review.verify_continuation(c.folder,c.plan)


@pytest.mark.parametrize('change', ['paid_unstarted','uncertain','new_claim','started','outcome2','guard2','new_scope','wrong_stop'])
def test_continuation_rejects_any_new_payment_or_broader_or_already_started_scope(continuation, change):
    c = continuation
    if change == 'paid_unstarted': c.per_case[1048369]['attempts'] = 1
    elif change == 'uncertain': c.costs['usage_unknown_attempts'] = 1
    elif change == 'new_claim': put(c.folder/'1048369/gemini_extraction-new.claim.private.json', {})
    elif change == 'started': put(c.folder/review.CONTINUATION_STARTED, {})
    elif change == 'outcome2': put(c.folder/'1048369'/review.CONTINUATION_OUTCOME, {})
    elif change == 'guard2': (c.folder/review.CONTINUATION_GUARD).mkdir()
    elif change == 'new_scope': c.plan['ids'] = list(review.IDS)+[42]
    elif change == 'wrong_stop': put(c.folder/'handoff-guards/dispatch-stop.private.json', {'reason':'sent_provider_attempt_with_uncertain_usage'})
    with pytest.raises(ValueError): review.continuation_baseline(c.folder,c.plan)


def test_continuation_pg_checks_every_unstarted_case_before_any_models(continuation,monkeypatch):
    c = continuation
    auth = review.continuation_baseline(c.folder,c.plan)
    record = review.read_private(review.ORIGINAL/str(review.UNSTARTED_IDS[0])/'outcome.private.json')
    pg = FakePG(record)
    # Use the same legitimate observation/deadline for all seven, no manufactured refresh.
    for cid in review.UNSTARTED_IDS: put(review.ORIGINAL/str(cid)/'outcome.private.json',record)
    from buenapro_worker.prod2 import llm1_completion
    selections=[]
    monkeypatch.setattr(llm1_completion,'_current_selection_receipt',
        lambda cursor,context,description:selections.append(context['convocation_id']))
    record['description']='Same exact retained title'
    for cid in review.UNSTARTED_IDS:put(review.ORIGINAL/str(cid)/'outcome.private.json',record)
    review.continuation_pg_preflight(pg,c.plan,auth)
    assert len(pg.statements) == 21
    assert {params[0] for _,params in pg.statements} == set(review.UNSTARTED_IDS)
    assert not set(review.EXCLUDED_PAID) & {params[0] for _,params in pg.statements}
    assert selections==list(review.UNSTARTED_IDS)


@pytest.mark.parametrize('block', ['prior_paid','canonical','stale','deadline'])
def test_continuation_pg_never_adopts_existing_payment_reading_or_fakes_freshness(continuation,block):
    c = continuation
    auth = review.continuation_baseline(c.folder,c.plan)
    record = fresh_record()
    if block == 'stale': record['temporal_observed_at'] = (datetime.now(timezone.utc)-timedelta(hours=7)).isoformat()
    for cid in review.UNSTARTED_IDS: put(review.ORIGINAL/str(cid)/'outcome.private.json',record)
    pg = FakePG(record,
        prior=[{'id':1,'request_hash':'x','claim_token':'y','status':'done'}] if block=='prior_paid' else [],
        current=[{'id':1}] if block=='canonical' else [],
        source_patch={'proposals_closes_at': None} if block=='deadline' else {})
    with pytest.raises(ValueError): review.continuation_pg_preflight(pg,c.plan,auth)


def test_continuation_same_folder_new_receipts_only_seven_and_no_automatic_replay(continuation,monkeypatch):
    c = continuation
    auth = review.continuation_baseline(c.folder,c.plan)
    review.once(c.folder/review.AUTHORIZATION,auth)
    before = {p:p.read_bytes() for p in c.folder.rglob('*.private.json')}
    class Owner:
        closed = broken = False
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,statement,params=None):
            assert 'pg_try_advisory_lock' in statement or 'pg_locks' in statement
            return Reply({'locked':True,'held':True})
    monkeypatch.setattr(review.psycopg,'connect',lambda *args,**kwargs:Owner())
    events=[]
    monkeypatch.setattr(review,'continuation_pg_preflight',lambda *args:events.append('all_pg_checked'))
    def read(settings,plan,cid,limits,guard,*,continuation=False):
        assert continuation and events[0]=='all_pg_checked'
        assert plan['folder']==str(c.folder) and cid in review.UNSTARTED_IDS
        assert limits.calls['llm1']==2 and limits.maximum['llm1']==9
        assert limits.maximum['embedding']==10
        assert guard.folder==c.folder/review.CONTINUATION_GUARD
        guard.before_request()
        value={'convocation_id':cid,'status':'llm1_complete'}
        review.once(c.folder/str(cid)/review.CONTINUATION_OUTCOME,value)
        events.append(cid)
        return value
    monkeypatch.setattr(review,'read_case',read)
    result=review.execute_continuation(c.plan,SimpleNamespace(database_url='unused'),auth)
    assert sorted(events[1:])==list(review.UNSTARTED_IDS)
    assert result['llm1_complete']==7 and result['excluded_paid_ids']==[1047330,1048156]
    assert all(p.read_bytes()==data for p,data in before.items())
    assert (c.folder/review.CONTINUATION_REPORT).exists()
    with pytest.raises(ValueError,match='already'):
        review.execute_continuation(c.plan,SimpleNamespace(database_url='unused'),auth)


@pytest.mark.parametrize('continuation', [False, True])
def test_only_unstarted_continuation_uses_explicit_output_cap_and_timeout(tmp_path,monkeypatch,continuation):
    folder, original = tmp_path/'run',tmp_path/'original'
    cid=review.UNSTARTED_IDS[0]
    monkeypatch.setattr(review,'ORIGINAL',original)
    put(original/'manifest.private.json',{'profile_id':'unchanged-profile'})
    put(original/str(cid)/'outcome.private.json',{'object_type':'Bien',**fresh_record()})
    case=folder/str(cid);case.mkdir(parents=True)
    if continuation:
        put(case/'outcome.private.json',{'convocation_id':cid,'status':'completion_stopped',
            'error_type':'LimitReached','no_automatic_retry':True})
    pdf=case/'retained.pdf';pdf.write_bytes(b'retained-source-not-transported')
    prepared={'pdf_path':pdf,'page_count':1,'document_set':[{'document_uuid':'fixed-source',
        'original_sha256':'a'*64,'source_url':'https://official.invalid/retained'}],
        'document_scope':'selected','document_set_hash':'b'*64,'document_set_identity':{},
        'selection_context':{},'source_document_manifest':{},'llm1_input':{'input':'unchanged'}}
    monkeypatch.setattr(review,'prepare_case',lambda *args:prepared)
    monkeypatch.setattr(review,'prepare_retrieved_input',lambda *args,**kwargs:prepared)
    monkeypatch.setattr(review,'prepare_input',lambda *args:(prepared,{'text':'unchanged'}))
    monkeypatch.setattr(review,'pg_preflight',lambda *args,**kwargs:None)
    monkeypatch.setattr(review,'resolve_llm1_model',lambda *args:('same-model',{}))
    monkeypatch.setattr(review,'extraction_request_hash',lambda *args,**kwargs:'r'*64)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(review.psycopg,'connect',lambda *args,**kwargs:Connection())
    observed={}
    def paid(*args,**kwargs):
        observed.update(kwargs)
        return 1,{'requires_human_review':False,'cost_usd':.01,'facets':[]},False,'r'*64,{}
    monkeypatch.setattr(review,'paid_selected_read',paid)
    monkeypatch.setattr(review,'persist_completed_llm1',lambda *args,**kwargs:{})
    class Settings:
        database_url='unused';gemini_model='same-model'
        def model_copy(self,**kwargs):return self
    guard=SimpleNamespace(before_request=lambda:None,observe_attempt=lambda attempt:None)
    plan={'folder':str(folder),'embedding_config':{},'cases':[{'convocation_id':cid,'original_outcome_sha256':'c'*64}]}
    limits=review.RunLimits(max_accepted=7,max_candidates=7,max_jev_calls=0,
        max_llm1_calls=9,max_embedding_calls=10,model_workers=2)
    result=review.read_case(Settings(),plan,cid,limits,guard,continuation=continuation)
    assert result['status']=='llm1_complete'
    expected={'after_attempt':guard.observe_attempt}
    if continuation:expected.update(max_output_tokens=65536,timeout_ms=300000)
    assert observed['generation_options']==expected
    assert observed['fallback_config'] is None
    assert observed['local_text_result']=={'text':'unchanged'}
    assert observed['prepared'] is prepared and observed['profile_id']=='unchanged-profile'
    if continuation:
        assert review.read_private(case/'outcome.private.json')['error_type']=='LimitReached'
        assert (case/review.CONTINUATION_OUTCOME).exists()


def test_paid_two_can_never_use_continuation_read_path(tmp_path):
    for cid in review.EXCLUDED_PAID:
        with pytest.raises(ValueError,match='exact seven'):
            review.read_case(None,{'folder':str(tmp_path)},cid,None,None,continuation=True)


def test_last_case_incompatible_selection_blocks_whole_continuation_before_payment(continuation,monkeypatch):
    c=continuation
    auth=review.continuation_baseline(c.folder,c.plan)
    review.once(c.folder/review.AUTHORIZATION,auth)
    before={p:p.read_bytes() for p in c.folder.rglob('*.private.json')}
    record={'description':'Original full title',**fresh_record()}
    for cid in review.UNSTARTED_IDS:put(review.ORIGINAL/str(cid)/'outcome.private.json',record)
    class Owner(FakePG):
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,statement,params=None):
            if 'pg_try_advisory_lock' in statement:return Reply({'locked':True})
            return super().execute(statement,params)
    owner=Owner(record)
    monkeypatch.setattr(review.psycopg,'connect',lambda *args,**kwargs:owner)
    from buenapro_worker.prod2 import llm1_completion
    def selection(cursor,context,title):
        assert title==record['description']
        if context['convocation_id']==review.UNSTARTED_IDS[-1]:raise ValueError('PG selected identity differs')
    monkeypatch.setattr(llm1_completion,'_current_selection_receipt',selection)
    monkeypatch.setattr(review,'read_case',lambda *args,**kwargs:pytest.fail('No first paid case may start'))
    with pytest.raises(ValueError,match='selected identity differs'):
        review.execute_continuation(c.plan,SimpleNamespace(database_url='unused'),auth)
    assert len(owner.statements)==21
    assert not (c.folder/review.CONTINUATION_STARTED).exists()
    assert not (c.folder/review.CONTINUATION_GUARD).exists()
    assert all(p.read_bytes()==data for p,data in before.items())
