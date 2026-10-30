"""Preparation, go gates and bounded dispatch without external I/O."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading
import time

import pytest

from buenapro_worker.prod2 import parallel_discovery as subject
from buenapro_worker.prod2.batch_plan import digest
from buenapro_worker.prod2.service import _lock, _write

ROOT = Path(__file__).resolve().parents[3]


def plan(**kwargs):
    return subject.build_plan(ROOT, run_id='synthetic-mapping', published_from='2026-10-04',
        published_to='2026-10-20', years=[2026], **kwargs)


@pytest.fixture
def prepared(tmp_path):
    value = plan()
    marker = tmp_path/'plan.private.json'
    subject.prepare(marker, value)
    return marker, value, tmp_path/value['run_id']


def test_offline_plan_exact_inventory_and_separate_unmeasured_aliases():
    base = plan()
    extended = plan(include_industrial_variants=True)
    assert len(base['keywords']) == 167 and len(base['queries']) == 334
    assert len(extended['keywords']) == 184 and len(extended['queries']) == 368
    assert extended['keywords'][:167] == base['keywords']
    assert extended['variant_inventory_sha256'] and base['variant_inventory_sha256'] is None
    assert all(base[key] is False for key in ('models','document_downloads','redis','publish_opportunities'))
    assert base['max_workers'] == 10 and base['ramp'] == {'pilot_queries':1,'warmup_queries':8,'warmup_workers':4}
    assert all(q['year']==2026 for q in base['queries'])
    assert base['coverage'].startswith('only_configured_')


@pytest.mark.parametrize('kwargs', [{'workers':0},{'workers':11},{'workers':True},
    {'request_interval':0.5},{'request_interval':float('nan')},{'max_http_requests':0},
    {'max_runtime_seconds':0},{'scope':'invalid'},
    {'scope':'digital','include_industrial_variants':True}])
def test_invalid_parallelism_or_budgets_fail_offline(kwargs):
    with pytest.raises(subject.DiscoveryError):
        plan(**kwargs)


def test_years_and_date_window_are_explicit_not_a_silent_current_year_filter():
    with pytest.raises(subject.DiscoveryError):
        subject.build_plan(ROOT,run_id='safe',published_from='2026-10-21',published_to='2026-10-20',years=[2026])
    with pytest.raises(subject.DiscoveryError):
        subject.build_plan(ROOT,run_id='safe',published_from='2026-10-04',published_to='2026-10-20',years=[])
    p=subject.build_plan(ROOT,run_id='safe',published_from='2026-10-04',published_to='2026-10-20',years=[2025,2026])
    assert len(p['queries'])==668 and p['years']==[2026,2025]


def test_plan_tamper_changed_runtime_or_scope_is_not_silently_replanned(prepared,monkeypatch):
    marker,value,_=prepared
    assert subject.load_plan(marker,digest(value),ROOT)==value
    changed=dict(value,max_workers=9)
    _write(marker,changed)
    with pytest.raises(subject.DiscoveryError,match='digest_mismatch'):
        subject.load_plan(marker,digest(value),ROOT)
    _write(marker,value)
    monkeypatch.setattr(subject,'code_bindings',lambda root:{'changed':'0'*64})
    with pytest.raises(subject.DiscoveryError,match='runtime_changed'):
        subject.load_plan(marker,digest(value),ROOT)


def test_preparation_does_not_overwrite_an_existing_reviewed_plan(prepared):
    marker,value,_=prepared
    before=marker.read_bytes()
    with pytest.raises(subject.DiscoveryError,match='not_be_overwritten'):
        subject.prepare(marker,value)
    assert marker.read_bytes()==before and marker.stat().st_mode & 0o077 == 0


def test_no_go_never_reads_plan_opens_pg_or_creates_run(tmp_path):
    folder=tmp_path/'missing'
    def forbidden():
        pytest.fail('No database/config/network before go')
    with pytest.raises(subject.DiscoveryError,match='explicit_go'):
        subject.run(tmp_path/'absent.json','0'*64,folder,ROOT,connect=forbidden,gate_factory=forbidden)
    assert not folder.exists()


def test_wrong_digest_never_calls_runtime_or_creates_run(prepared):
    marker,value,folder=prepared
    with pytest.raises(subject.DiscoveryError,match='digest_mismatch'):
        subject.run(marker,'0'*64,folder,ROOT,go=True,connect=lambda:pytest.fail('No PG'),gate_factory=None)
    assert not folder.exists()


def test_same_folder_exclusion_does_not_overwrite_other_owner_state(prepared):
    marker,value,folder=prepared
    folder.mkdir(mode=0o700)
    _write(folder/'state.private.json',{'status':'running','plan_sha256':digest(value)})
    before=(folder/'state.private.json').read_bytes()
    with _lock(folder), pytest.raises(subject.DiscoveryError,match='owned_by_another'):
        subject.run(marker,digest(value),folder,ROOT,go=True)
    assert (folder/'state.private.json').read_bytes()==before


def test_resume_requires_explicit_flag_and_new_source_recovery_evidence(prepared,monkeypatch):
    marker,value,folder=prepared
    folder.mkdir(mode=0o700)
    _write(folder/'state.private.json',{'status':'partial_or_blocked','plan_sha256':digest(value),'stop_reason':'source_403'})
    with pytest.raises(subject.DiscoveryError,match='explicit_resume'):
        subject.run(marker,digest(value),folder,ROOT,go=True)
    with pytest.raises(subject.DiscoveryError,match='recovery_evidence'):
        subject.run(marker,digest(value),folder,ROOT,go=True,resume=True)
    monkeypatch.setattr(subject,'execute_mapping',lambda *a,**k:{'status':'complete','queries_complete':332,'http':{'stop_reason':None}})
    result=subject.run(marker,digest(value),folder,ROOT,go=True,resume=True,access_recovered=True,
        connect=object(),gate_factory=object())
    assert result['status']=='complete'
    with pytest.raises(subject.DiscoveryError,match='completed_plan'):
        subject.run(marker,digest(value),folder,ROOT,go=True,resume=True,access_recovered=True)


@pytest.mark.parametrize('workers',[1,4,10])
def test_dispatch_is_bounded_and_results_equivalent(workers):
    lock=threading.Lock()
    active=maximum=0
    seen=[]
    def execute(query):
        nonlocal active,maximum
        with lock:
            active+=1
            maximum=max(maximum,active)
        time.sleep(.005)
        with lock:
            seen.append(query['id'])
            active-=1
        return {'query':query,'coverage_complete':True,'errors':[]}
    queries=[{'id':i} for i in range(25)]
    result=subject.bounded_batch(queries,workers,execute,threading.Event())
    assert len(result)==25 and sorted(seen)==list(range(25)) and maximum<=workers and active==0


def test_first_failure_stops_scheduling_unstarted_queries():
    stop=threading.Event()
    called=[]
    def execute(q):
        called.append(q)
        return {'query':q,'coverage_complete':False,'errors':[{'code':'source_403'}]}
    results=subject.bounded_batch(list(range(100)),1,execute,stop)
    assert len(results)==len(called)==1 and stop.is_set()


def test_worker_exception_is_sanitized_and_joins_owned_futures():
    stop=threading.Event()
    def execute(q):
        raise RuntimeError('private DSN and source response must not escape')
    result=subject.bounded_batch([{'id':i} for i in range(100)],4,execute,stop)
    assert 1<=len(result)<=4 and stop.is_set()
    assert 'private DSN' not in str(result)
    assert all(r['errors']==[{'code':'discovery_worker_failed'}] for r in result)


def test_owner_connection_loss_stops_source_without_reconnecting():
    stop=threading.Event()
    class Conn:
        calls=0
        def execute(self,sql):
            self.calls+=1
            raise RuntimeError('private pg transport details')
    conn=Conn()
    owner=subject.Owner(conn,stop)
    with pytest.raises(subject.DiscoveryError,match='lock_lost'):
        owner.check()
    assert stop.is_set() and conn.calls==1
    with pytest.raises(subject.DiscoveryError,match='stop_requested'):
        owner.check()
    assert conn.calls==1


def test_symlinked_cache_is_rejected_before_database_or_source(tmp_path):
    folder=tmp_path/'mapping';folder.mkdir(mode=0o700)
    outside=tmp_path/'outside';outside.mkdir()
    (folder/'keyword-cache').symlink_to(outside,target_is_directory=True)
    with pytest.raises(subject.DiscoveryError,match='symlink'):
        subject.execute_mapping(plan(),folder,lambda:pytest.fail('No PG'),lambda:pytest.fail('No source'))


def test_cli_without_go_stays_offline_even_when_pg_config_is_missing(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('parallel_cli_test',ROOT/'scripts/run_prod2_parallel_discovery.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    with pytest.raises(subject.DiscoveryError,match='explicit_go'):
        cli.main(['run','--plan',str(tmp_path/'missing'),'--expected-sha256','0'*64,'--folder',str(tmp_path/'run')])
    assert not (tmp_path/'run').exists()


def test_source_hold_survives_error_while_building_final_report(prepared,monkeypatch):
    marker,value,folder=prepared
    class Gate:
        def metrics(self): return {'stop_reason':'source_403'}
    def broken(plan,folder,connect,gate_factory,**kwargs):
        gate_factory()
        raise RuntimeError('failure after source403 response')
    monkeypatch.setattr(subject,'execute_mapping',broken)
    with pytest.raises(subject.DiscoveryError,match='mapping_runtime_error'):
        subject.run(marker,digest(value),folder,ROOT,go=True,connect=object(),gate_factory=lambda **k:Gate())
    state=subject._read(folder/'state.private.json')
    assert state['status']=='attention_required' and state['stop_reason']=='source_403'
    with pytest.raises(subject.DiscoveryError,match='recovery_evidence'):
        subject.run(marker,digest(value),folder,ROOT,go=True,resume=True)


def test_recovery_claim_does_not_clear_prior_hold_before_successful_source_work(prepared,monkeypatch):
    marker,value,folder=prepared
    folder.mkdir(mode=0o700)
    _write(folder/'state.private.json',{'status':'attention_required','plan_sha256':digest(value),'stop_reason':'source_429'})
    def broken(*args,**kwargs): raise RuntimeError('schema/PG failure before HTTP')
    monkeypatch.setattr(subject,'execute_mapping',broken)
    with pytest.raises(subject.DiscoveryError):
        subject.run(marker,digest(value),folder,ROOT,go=True,resume=True,access_recovered=True,
            connect=object(),gate_factory=object())
    assert subject._read(folder/'state.private.json')['stop_reason']=='source_429'


def test_prior_source_hold_has_priority_over_later_pg_failure_without_completed_mapping():
    assert subject._stop_reason('coordinator_connection_lost','source_403')=='source_403'
    assert subject._stop_reason(None,'source_429')=='source_429'
    assert subject._stop_reason(None,'source_403',complete=True) is None
    assert subject._stop_reason('source_429','source_403',complete=True)=='source_429'


def test_abrupt_interruption_requires_access_review_even_if_no_final_hold_was_written(prepared):
    marker,value,folder=prepared
    folder.mkdir(mode=0o700)
    _write(folder/'state.private.json',{'status':'running','plan_sha256':digest(value)})
    with pytest.raises(subject.DiscoveryError,match='recovery_evidence'):
        subject.run(marker,digest(value),folder,ROOT,go=True,resume=True)


def test_apply_schema_without_go_never_reads_configuration_or_opens_database(tmp_path):
    spec=importlib.util.spec_from_file_location('parallel_cli_schema_test',ROOT/'scripts/run_prod2_parallel_discovery.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    with pytest.raises(subject.DiscoveryError,match='explicit_go'):
        cli.main(['apply-schema','--plan',str(tmp_path/'absent'),'--expected-sha256','0'*64])
