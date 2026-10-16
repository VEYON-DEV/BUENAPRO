"""Opt-in cohort scope regression, with no HTTP, PG, SSH or model calls."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
import sys

import pytest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'scripts'))
import dinaut_batch_scope as scope
import run_dinaut_three_dot_matches as run
import run_dinaut_three_dot_gemma_batches as batches
import run_dinaut_two_dot_matches as entry
import dinaut_three_dot_persistence as persistence


def sample(source='seace_prod2',identity=19):
    return {'ready':True,'source_key':source,'source_id':identity,'opportunity_id':f'opp-{identity}',
        'fit_level':2,'code':'EXAMPLE','description':'Software','buyer_name':'Entidad',
        'extraction_id':27,'document_sha256':'a'*64,'is_current':True,'quality':'auto',
        'requires_human_review':True,'raw_extraction_json':{'read':True},'summary_json':{'object':'Software'},
        'existing_match_id':None,'prior_match_reservations':[],'prior_worker_jobs':[],'prior_provider_events':[],
        'facets':[{'extraction_id':27,'is_current':True,'facet':'specific_experience','label':'Software',
                   'required':True,'details_json':{}}]}


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    monkeypatch.setattr(run,'build_profile_cards',lambda p:{'company':{},'coverage':{'document_count':43}})
    monkeypatch.setattr(run.tiktoken,'get_encoding',lambda name:SimpleNamespace(encode=lambda text:[]))
    monkeypatch.setattr(run,'build_jev_payload',lambda cards,op,facets,**kw:{'model':run.JEV_MODEL,'state':{'requirements':facets},'questions':{}})
    monkeypatch.setattr(run,'token_budget',lambda *a,**kw:{'offline':True})
    def create(cases=None):
        receipt={'profile_id':run.PROFILE_ID,'tenant_id':run.TENANT_ID,'cutoff_utc':'2026-10-15T20:00:00+00:00'}
        inputs={'profile':{'razon_social':'DIN'},'business_lines':[],'cases':cases or [sample()],'receipt':dict(receipt)}
        path=tmp_path/'inputs.private.json';path.write_text(json.dumps(inputs))
        receipt['inputs_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        (tmp_path/'preflight.private.json').write_text(json.dumps(receipt))
        return tmp_path
    return create


def test_default_three_dot_scope_and_allowlist_are_restored_after_new_scope():
    before=(run.ACTIVE_SCOPE,run.GEMMA_MODEL,run.MODEL)
    with pytest.raises(RuntimeError):
        with run.execution_scope(scope.TWO_DOT_50_SCOPE):
            run.select_recommendation_model('gemini-3.1-flash-lite')
            assert run.scope_replacements()==frozenset()
            raise RuntimeError('stop')
    assert (run.ACTIVE_SCOPE,run.GEMMA_MODEL,run.MODEL)==before
    assert run.scope_replacements()==run.ALLOWED_REPLACEMENTS
    with pytest.raises(ValueError):scope.validate_scope(scope.BatchScope('other',2,'elsewhere','x','x','x'))


def test_preparation_scope2_is_new_only_and_has_explicit_binding(prepared):
    folder=prepared()
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):
        run.select_recommendation_model('gemini-3.1-flash-lite')
        _,_,cases,manifest=run.prepare(folder)
    assert manifest['fit_level']==2 and manifest['new_matches_only'] is True
    assert cases[0]['binding']['cohort']==scope.TWO_DOT_50_SCOPE.name
    assert cases[0]['binding']['source_summary_policy']=='canonical_requirements_for_scoring_full_source_summary_for_explanations'
    assert (folder/'execution-manifest-flash-lite.private.json').exists()
    assert not (folder/'jev-started.private.json').exists()


@pytest.mark.parametrize('change',[
    {'fit_level':3},{'existing_match_id':129},
    {'prior_match_reservations':[{'id':1,'status':'done'}]},
    {'prior_match_reservations':[{'id':1,'status':'failed'}]},
    {'prior_worker_jobs':[{'id':1,'status':'done'}]},
    {'prior_provider_events':[{'id':1,'status':'ok'}]},
    {'extraction_id':None},{'raw_extraction_json':{}},
])
def test_scope2_never_sends_existing_historical_other_level_or_unread_cases(prepared,change):
    folder=prepared([{**sample(),**change}])
    with run.execution_scope(scope.TWO_DOT_50_SCOPE),pytest.raises((ValueError,run.ProviderBlocked)):
        run.prepare(folder)
    assert not (folder/'execution-manifest-flash-lite.private.json').exists()


def test_scope2_keeps_a_whole_individual_case_with_more_than16requirements(prepared):
    row=sample();row['facets']=[{**row['facets'][0],'id':i} for i in range(23)]
    folder=prepared([row])
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):
        assert len(run.prepare(folder)[2][0]['jev_payload']['state']['requirements'])==23


def test_default_manifest_shape_and_pipeline_binding_remain_identical(prepared):
    folder=prepared([{**sample(),'fit_level':3}])
    _,_,cases,manifest=run.prepare(folder)
    assert set(manifest)=={'profile_id','tenant_id','inputs_sha256','policy','models','cases'}
    assert set(cases[0]['binding'])=={'source','profile','lines','jev_request','recommendation_model','recommendation_prompt','policy','as_of'}
    assert run.ACTIVE_SCOPE==scope.THREE_DOT_SCOPE


def test_scope2_rejects_more_than50_or_more_than25_of_one_source(prepared):
    folder=prepared([sample(identity=i+1) for i in range(26)])
    with run.execution_scope(scope.TWO_DOT_50_SCOPE),pytest.raises(ValueError,match='per-source'):
        run.prepare(folder)
    folder=prepared([sample('seace_prod2' if i<26 else 'seace_prod6',i+1) for i in range(51)])
    with run.execution_scope(scope.TWO_DOT_50_SCOPE),pytest.raises(ValueError,match='opportunity limit'):
        run.prepare(folder)


def test_scope2_sql_has_identical_frozen_publication_projection_and_fit_only_change():
    old=run.prod2_sql()
    assert scope.scoped_source_sql(old,scope.THREE_DOT_SCOPE)==old
    new=scope.scoped_source_sql(old,scope.TWO_DOT_50_SCOPE)
    assert 'WHERE fit.fit_level=2' in new and 'p.published_at AS published_at' in new
    minor=scope.scoped_source_sql(run.PROD6_SQL,scope.TWO_DOT_50_SCOPE)
    assert 'c.fec_publica AS published_at' in minor
    with pytest.raises(ValueError):scope.scoped_source_sql('SELECT 1',scope.TWO_DOT_50_SCOPE)


def test_scope2_iso_boundary_matches_frozen_datetime_uuid_decimal_without_default3_change():
    timestamp=datetime(2026,10,15,22,30,tzinfo=timezone.utc)
    identity=UUID('e11f2610-eb56-457a-97eb-bbd24dd6f1a1')
    live={'closes_at':timestamp,'id':identity,'amount':Decimal('100.50'),'nested':[timestamp]}
    frozen={'closes_at':timestamp.isoformat(),'id':str(identity),'amount':'100.50','nested':[timestamp.isoformat()]}
    assert scope.iso_snapshot(live)==frozen
    assert run.scope_snapshot(live) is live
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):assert run.scope_snapshot(live)==frozen
    with pytest.raises(ValueError):scope.iso_snapshot(datetime(2026,10,15))


def test_predispatch2_matches_actual_typed_pg_boundary_to_frozen_iso_snapshot(tmp_path,monkeypatch):
    timestamp=datetime(2026,10,15,22,30,tzinfo=timezone.utc)
    identifier=UUID('e11f2610-eb56-457a-97eb-bbd24dd6f1a1')
    live=sample();live.update(closes_at=timestamp,business_line_id=identifier,fit_points=Decimal('35.50'))
    raw_profile={'razon_social':'DIN','identity_json':{'when':timestamp}}
    raw_lines=[{'id':identifier,'created_at':timestamp,'monto_min':Decimal('100.50')}]
    frozen={'profile':scope.iso_snapshot(raw_profile),'business_lines':scope.iso_snapshot(raw_lines)}
    expected=scope.iso_snapshot(live)
    class Connection:
        def execute(self,sql,args=()):
            if sql.startswith('SELECT * FROM company_profiles'):self.value=raw_profile
            elif sql.startswith('SELECT * FROM business_lines'):self.value=raw_lines
            elif sql.startswith('SELECT * FROM ('):self.value=live
            elif 'FROM pipeline_events' in sql or 'FROM worker_jobs' in sql:self.value=[]
            else:pytest.fail('Unexpected external-style query in offline predispatch')
            return self
        def fetchone(self):return self.value
        def fetchall(self):return self.value
    monkeypatch.setattr(run,'current_match',lambda *args:{'previous_match':None,'previous_provenance':None})
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):
        normalized=run.source_reader(Connection(),expected)
        assert run.source_fingerprint(normalized)==run.source_fingerprint(expected)
        assert run.predispatch(Connection(),frozen,expected,tmp_path)=={'previous_match':None,'previous_provenance':None}


def test_fresh_foreign_ledger_blocks_even_if_complete_and_own_receipt_only_is_allowed(tmp_path,monkeypatch):
    monkeypatch.setattr(run,'get_cached_jev_receipt',lambda folder:{'request_sha256':'ours'})
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):
        run.guard_live_provider_events({'prior_provider_events':[{'stage':scope.TWO_DOT_50_SCOPE.event_prefix+'jev','provider_request_hash':'ours'}]},tmp_path)
        for event in [{'stage':'dinaut_three_dot_jev','provider_request_hash':'ours'},
                      {'stage':scope.TWO_DOT_50_SCOPE.event_prefix+'jev','provider_request_hash':'other'}]:
            with pytest.raises(run.PublicationConflict,match='provider ledger'):
                run.guard_live_provider_events({'prior_provider_events':[event]},tmp_path)


def test_entrypoint_delegates_offline_to_flash_schema_engine_and_jev_only_to_same_scope(tmp_path,monkeypatch):
    monkeypatch.setattr(run,'ROOT',tmp_path)
    calls=[]
    monkeypatch.setattr(batches,'main',lambda *a,**kw:calls.append(('batch',a,kw)))
    monkeypatch.setattr(run,'main',lambda *a,**kw:calls.append(('jev',a,kw)))
    entry.main([]);entry.main(['--jev-only','--limit','2'])
    assert calls[0][0]=='batch' and '--execute' not in calls[0][1][0]
    assert calls[0][2]['scope']==scope.TWO_DOT_50_SCOPE
    assert 'gemini-3.1-flash-lite' in calls[0][1][0]
    assert calls[1][0]=='jev' and '--jev-only' in calls[1][1][0]
    with pytest.raises(ValueError):entry.main(['--folder',str(tmp_path/'copy')])
    with pytest.raises(ValueError):entry.main(['--limit','51'])


def test_flash_pack_limit_applies_to_two_cases_but_not_one_complete_large_case(tmp_path,monkeypatch):
    monkeypatch.setattr(batches,'batch_request',lambda contexts:{'contents':'mock'})
    monkeypatch.setattr(batches,'count_input',lambda *a:batches.INPUT_BUDGET+1)
    context={'evaluation':{'requirements':[{}]*20}}
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):
        with pytest.raises(run.ProviderBlocked,match='sixteen'):
            batches.execute_batch(None,None,tmp_path/'two',['one','two'],{'one':context,'two':context},{})
        assert batches.execute_batch(None,None,tmp_path/'single',['one'],{'one':context},{'one':{'jev_payload':{}}})[0] is False


def test_two_dot_publisher_requires_opt_in_and_refuses_any_replacement():
    row=sample();row['source_key']='seace_prod6'
    persistence._ready(row,expected_fit_level=2)
    with pytest.raises(persistence.PublicationConflict):persistence._ready(row)
    with pytest.raises(persistence.PublicationConflict,match='only new matches'):
        persistence.publish_case(None,row,profile_id=run.PROFILE_ID,tenant_id=run.TENANT_ID,
            expected_profile_fingerprint='unused',score=75,verdict='gris',breakdown={},missing_actions=[],
            source_reader=lambda *a:None,backup_callback=lambda *a:None,
            expected_fit_level=2,event_type='dinaut_two_dot_50_analysis',
            allowed_replacements=frozenset({('seace_prod6',1)}))


def test_two_dot_runtime_guard_covers_entrypoint_canonical_sql_and_imported_matching_modules():
    assert run.scoped_runtime_paths()==[]
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):paths=run.scoped_runtime_paths()
    assert 'scripts/run_dinaut_two_dot_matches.py' in paths
    assert 'apps/web/server/services/keywordTextScoring.ts' in paths
    assert 'workers/seace/src/buenapro_worker/matching/jev_recommendations.py' in paths


def test_full_source_summary_restored_for_flash_only_without_mutating_frozen_jev(monkeypatch):
    source=sample();source['summary_json']={'participation':{'consorcio':{'conditions':['Full source condition']}},'dni':'private'}
    payload={'state':{'opportunity':{'descripcion':'Software'}}};before=deepcopy(payload)
    monkeypatch.setattr(run,'build_small_recommendation_context',lambda p,*args:{'evaluation':{'opportunity':deepcopy(p['state']['opportunity'])}})
    with run.execution_scope(scope.TWO_DOT_50_SCOPE):context=run.explanation_context(payload,{}, {},source)
    assert context['evaluation']['opportunity']['resumen']=={'participation':source['summary_json']['participation']}
    assert run.explanation_context(payload,{}, {},source,scope=scope.TWO_DOT_50_SCOPE)==context
    assert payload==before
