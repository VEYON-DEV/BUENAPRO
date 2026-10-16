"""The additional thirty-case DIN scope is SEACE-only, isolated and offline."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'scripts'))
import dinaut_batch_scope as scopes
import dinaut_three_dot_persistence as persistence
import run_dinaut_three_dot_matches as run
import run_dinaut_three_dot_gemma_batches as batches
import run_dinaut_two_dot_seace_matches as entry

SCOPE=scopes.TWO_DOT_SEACE_30_SCOPE


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(batches.requests,'post',lambda *a,**k:pytest.fail('No HTTP'))
    monkeypatch.setattr(run.psycopg,'connect',lambda *a,**k:pytest.fail('No PG'))
    monkeypatch.setattr(run,'production_settings',lambda *a,**k:pytest.fail('No SSH'))


def sample(identity=1,source='seace_prod2',**changes):
    return {'ready':True,'source_key':source,'source_id':identity,'opportunity_id':f'opp-{identity}',
        'fit_level':2,'code':'SEACE','description':'Software','buyer_name':'Entidad',
        'extraction_id':27,'document_sha256':'a'*64,'is_current':True,'quality':'auto',
        'requires_human_review':True,'raw_extraction_json':{'read':True},'summary_json':{'object':'Software'},
        'existing_match_id':None,'prior_match_reservations':[],'prior_worker_jobs':[],'prior_provider_events':[],
        'facets':[{'extraction_id':27,'is_current':True,'facet':'specific_experience','label':'Software',
                   'required':True,'details_json':{}}],**changes}


@pytest.fixture
def frozen(tmp_path,monkeypatch):
    monkeypatch.setattr(run,'build_profile_cards',lambda p:{'company':{},'coverage':{'document_count':43}})
    monkeypatch.setattr(run.tiktoken,'get_encoding',lambda name:SimpleNamespace(encode=lambda text:[]))
    monkeypatch.setattr(run,'build_jev_payload',lambda cards,op,facets,**kw:{'model':run.JEV_MODEL,
        'state':{'opportunity':deepcopy(op),'requirements':deepcopy(facets)},'questions':{}})
    monkeypatch.setattr(run,'token_budget',lambda *a,**k:{'offline':True})
    def create(cases,selector_scope=SCOPE):
        receipt={'profile_id':run.PROFILE_ID,'tenant_id':run.TENANT_ID,'cutoff_utc':'2026-10-16T16:00:00+00:00',
            'selector_sql_sha256':{source:hashlib.sha256(scopes.scoped_source_sql(
                run.prod2_sql() if source=='seace_prod2' else run.PROD6_SQL,selector_scope).encode()).hexdigest()
                for source in selector_scope.allowed_sources}}
        inputs={'profile':{'razon_social':'DIN'},'business_lines':[],'cases':cases,'receipt':deepcopy(receipt)}
        path=tmp_path/'inputs.private.json';path.write_text(json.dumps(inputs))
        receipt['inputs_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        (tmp_path/'preflight.private.json').write_text(json.dumps(receipt))
        return tmp_path
    return create


def test_seace_scope_is_explicit_immutable_and_cannot_widen_the_authorization():
    assert scopes.validate_scope(SCOPE) is SCOPE
    assert SCOPE.allowed_sources==('seace_prod2',) and SCOPE.maximum_cases==30
    assert SCOPE.new_only and SCOPE.fit_level==2
    for changed in (replace(SCOPE,maximum_cases=31),replace(SCOPE,allowed_sources=('seace_prod2','seace_prod6')),
                    replace(SCOPE,folder_name=scopes.TWO_DOT_50_SCOPE.folder_name)):
        with pytest.raises(ValueError):scopes.validate_scope(changed)
    assert scopes.TWO_DOT_50_SCOPE.allowed_sources==('seace_prod2','seace_prod6')
    assert scopes.THREE_DOT_SCOPE.allowed_sources==('seace_prod2','seace_prod6')


def test_sql_hash_is_the_established_prod2_selector_and_minor_selector_is_rejected():
    prod2=run.prod2_sql()
    assert scopes.scoped_source_sql(prod2,SCOPE)==scopes.scoped_source_sql(prod2,scopes.TWO_DOT_50_SCOPE)
    assert scopes.scoped_source_sql(prod2,scopes.THREE_DOT_SCOPE)==prod2
    with pytest.raises(ValueError,match='Source outside'):scopes.scoped_source_sql(run.PROD6_SQL,SCOPE)


def test_thirty_seace_cases_prepare_with_new_binding_and_intact_canonical_requirements(frozen):
    folder=frozen([sample(i+1) for i in range(30)])
    original=(folder/'inputs.private.json').read_bytes()
    with run.execution_scope(SCOPE):
        _,_,cases,manifest=run.prepare(folder)
    assert len(cases)==30 and manifest['cohort']==SCOPE.name
    assert manifest['new_matches_only'] is True and manifest['fit_level']==2
    assert cases[0]['binding']['cohort']==SCOPE.name
    assert cases[0]['binding']['source_summary_policy']=='canonical_requirements_for_scoring_full_source_summary_for_explanations'
    assert cases[0]['jev_payload']['state']['requirements']==sample()['facets']
    assert 'resumen' not in cases[0]['jev_payload']['state']['opportunity']
    assert (folder/'inputs.private.json').read_bytes()==original


@pytest.mark.parametrize('rows',[[sample(i+1) for i in range(31)],
    [sample(),sample(2,'seace_prod6')],[sample(),sample(2,'seace_prod6',ready=False)],
    [sample(source='other')]])
def test_over_limit_or_foreign_sources_block_before_profile_budget_or_manifest(frozen,monkeypatch,rows):
    folder=frozen(rows)
    monkeypatch.setattr(run,'build_profile_cards',lambda *_:pytest.fail('Source/count validation must precede profile construction'))
    with run.execution_scope(SCOPE),pytest.raises(ValueError):run.prepare(folder)
    assert not (folder/'execution-manifest-flash-lite.private.json').exists()


@pytest.mark.parametrize('change',[{'fit_level':3},{'existing_match_id':999},
    {'prior_match_reservations':[{'status':'done'}]},{'prior_worker_jobs':[{'status':'failed'}]},
    {'prior_provider_events':[{'status':'ok'}]},{'raw_extraction_json':{}}])
def test_same_new_only_readiness_and_history_guards_are_preserved(frozen,change):
    folder=frozen([sample(**change)])
    with run.execution_scope(SCOPE),pytest.raises((ValueError,run.ProviderBlocked)):run.prepare(folder)
    assert not (folder/'execution-manifest-flash-lite.private.json').exists()


def test_freeze_from_fifty_cannot_silently_supply_a_different_selector_receipt(frozen):
    folder=frozen([sample()],selector_scope=scopes.TWO_DOT_50_SCOPE)
    with run.execution_scope(SCOPE),pytest.raises(run.PublicationConflict,match='selector'):
        run.prepare(folder)


def test_wrapper_pins_folder_flash_model_scope_and_maximum_before_delegating(tmp_path,monkeypatch):
    monkeypatch.setattr(run,'ROOT',tmp_path)
    calls=[]
    monkeypatch.setattr(batches,'main',lambda *a,**kw:calls.append(('batch',a,kw)))
    monkeypatch.setattr(run,'main',lambda *a,**kw:calls.append(('jev',a,kw)))
    entry.main([]);entry.main(['--jev-only','--limit','30'])
    assert calls[0][2]['scope']==SCOPE and calls[0][2]['folder']==tmp_path/'tmp'/SCOPE.folder_name
    assert calls[0][1][0]==['--recommendation-model','gemini-3.1-flash-lite']
    assert calls[1][2]['scope']==SCOPE and '--jev-only' in calls[1][1][0]
    for args in (['--limit','31'],['--limit','0'],['--folder',str(tmp_path/'tmp'/scopes.TWO_DOT_50_SCOPE.folder_name)],
                 ['--folder',str(tmp_path/'tmp'/scopes.THREE_DOT_SCOPE.folder_name)]):
        with pytest.raises(ValueError):entry.main(args)
    assert len(calls)==2


def test_engine_also_rejects_other_canonical_folders_before_preparation(tmp_path,monkeypatch):
    monkeypatch.setattr(batches,'ROOT',tmp_path)
    monkeypatch.setattr(run,'ROOT',tmp_path)
    monkeypatch.setattr(run,'prepare',lambda *_:pytest.fail('Do not open another cohort'))
    for previous in (scopes.THREE_DOT_SCOPE,scopes.TWO_DOT_50_SCOPE):
        folder=tmp_path/'tmp'/previous.folder_name
        with pytest.raises(ValueError,match='canonical'):batches.main([],scope=SCOPE,folder=folder)
        with pytest.raises(ValueError,match='canonical'):
            run.main(['--folder',str(folder),'--recommendation-model','gemini-3.1-flash-lite'],scope=SCOPE)


@pytest.mark.parametrize('hook',[lambda row,p:run.source_reader(None,row),
    lambda row,p:run.current_match(None,row),lambda row,p:run.publication_source_reader(None,row,{}),
    lambda row,p:run.predispatch(None,{},row,p),
    lambda row,p:run.reserved_dispatch(None,row,p,'jev',{}),
    lambda row,p:run.saved_batch_recommendation(p,{'case':row}),
    lambda row,p:run.cached_scoring_context({}, {}, {'case':row},p),
    lambda row,p:run.run_case(None,None,{}, {}, {'case':row},p,'not-used')])
def test_forbidden_source_blocks_every_dispatch_and_publication_boundary_before_io(tmp_path,hook):
    with run.execution_scope(SCOPE),pytest.raises(ValueError,match='Source outside'):
        hook(sample(source='seace_prod6'),tmp_path)
    assert list(tmp_path.iterdir())==[]


def test_shared_generation_rejects_minor_source_before_request_construction_or_count(tmp_path,monkeypatch):
    monkeypatch.setattr(batches,'batch_request',lambda *_:pytest.fail('Source check must run before request/count'))
    with run.execution_scope(SCOPE),pytest.raises(ValueError,match='Source outside'):
        batches.execute_batch(None,None,tmp_path,['case'],{}, {'case':{'case':sample(source='seace_prod6')}})


def test_publisher_event_whitelist_keeps_new_event_seace_only_without_changing_old_scope(monkeypatch):
    class PassedScopeGuard(Exception):pass
    def reached(_):raise PassedScopeGuard
    monkeypatch.setattr(persistence,'_require_idle',reached)
    kwargs={'profile_id':run.PROFILE_ID,'tenant_id':run.TENANT_ID,'expected_profile_fingerprint':'unused',
        'score':50,'verdict':'gris','breakdown':{},'missing_actions':[],
        'source_reader':lambda *a:None,'backup_callback':lambda *a:None,'expected_fit_level':2}
    with pytest.raises(persistence.PublicationConflict,match='PROD2'):
        persistence.publish_case(None,sample(source='seace_prod6'),event_type=SCOPE.publication_event,**kwargs)
    with pytest.raises(PassedScopeGuard):
        persistence.publish_case(None,sample(),event_type=SCOPE.publication_event,**kwargs)
    with pytest.raises(PassedScopeGuard):
        persistence.publish_case(None,sample(source='seace_prod6'),event_type=scopes.TWO_DOT_50_SCOPE.publication_event,**kwargs)
    with pytest.raises(persistence.PublicationConflict,match='only new matches'):
        persistence.publish_case(None,sample(existing_match_id=9),event_type=SCOPE.publication_event,**kwargs)


def test_scope_restores_old_models_and_guards_after_exception_and_includes_new_wrapper():
    before=(run.ACTIVE_SCOPE,run.MODEL,run.GEMMA_MODEL)
    with pytest.raises(RuntimeError):
        with run.execution_scope(SCOPE):
            assert run.scope_persistence_kwargs()=={'expected_fit_level':2}
            assert run.scope_replacements()==frozenset()
            assert 'scripts/run_dinaut_two_dot_seace_matches.py' in run.scoped_runtime_paths()
            assert 'scripts/dinaut_source_citation_refs.py' in run.scoped_runtime_paths()
            with pytest.raises(ValueError):run.select_recommendation_model('gemma-4-31b-it')
            raise RuntimeError('test scope restoration')
    assert (run.ACTIVE_SCOPE,run.MODEL,run.GEMMA_MODEL)==before


def test_iso_and_source_summary_routing_reuse_the_established_two_dot_protocol(monkeypatch):
    live={'when':datetime(2026,10,16,16,0,tzinfo=timezone.utc)}
    payload={'state':{'opportunity':{'descripcion':'Software'}}};before=deepcopy(payload)
    monkeypatch.setattr(run,'build_small_recommendation_context',lambda p,*a:{'evaluation':{'opportunity':deepcopy(p['state']['opportunity'])}})
    source=sample(summary_json={'object':'Full frozen source summary','dni':'private'})
    with run.execution_scope(SCOPE):
        assert run.scope_snapshot(live)==scopes.iso_snapshot(live)
        context=run.explanation_context(payload,{}, {},source)
    assert context==run.explanation_context(payload,{}, {},source,scope=scopes.TWO_DOT_50_SCOPE)
    assert context['evaluation']['opportunity']['resumen']=={'object':'Full frozen source summary'}
    assert payload==before


def test_cached_scoring_fastpath_is_enabled_for_new_scope_without_pg(tmp_path,monkeypatch):
    monkeypatch.setattr(run,'get_cached_jev_receipt',lambda *a:{'body':{'paid':True}})
    monkeypatch.setattr(run,'score_match',lambda *a,**k:{'score':61})
    monkeypatch.setattr(run,'explanation_context',lambda *a,**k:{'summary':'cached'})
    frozen={'receipt':{'cutoff_utc':'2026-10-16T16:00:00+00:00'}}
    with run.execution_scope(SCOPE):
        assert run.cached_scoring_context(frozen,{}, {'case':sample(),'jev_payload':{}},tmp_path)=={'summary':'cached'}
    assert json.loads((tmp_path/'seace_prod2-1/scoring.private.json').read_text())=={'score':61}


def test_exact_source_citation_normalizer_is_available_in_both_seace_decoders(tmp_path,monkeypatch):
    monkeypatch.setattr(batches,'FOLDER',tmp_path)
    key='seace_prod2-1';row=sample();citation='Pág. 10, numeral 9'
    payload={'state':{'requirements':[{'source_index':1,'detalle':{'evidencia':citation}}],
        'profile':{'documents':[{'ref':'D1'}],'professionals':[{'ref':'P1'}]}}}
    answer={'case_key':key,'resumen':'Contrastar autorización.','requisitos':[{'source_index':1,
        'evidencia':citation,'brecha':'Autorización pendiente.','accion':'Verificarla.',
        'evidence_refs':['D1',citation,'P1']}],'acciones_recomendadas':['Verificar autorización.']}
    request={'contents':'Frozen'};request_sha=run.fingerprint(request)
    receipt={'http_status':200,'request_sha256':request_sha,'body':{
        'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps({'evaluations':[answer]})}]}}],
        'usageMetadata':{'promptTokenCount':99,'candidatesTokenCount':9}}}
    plan={'case_keys':[key],'request_sha256':request_sha,'jev_requests':{key:run.fingerprint(payload)}}
    folder=tmp_path/'gemma-batches/batch-one'
    for name,value in [('plan.private.json',plan),('gemma-request.private.json',request),('gemma-response.private.json',receipt)]:
        run.private_json(folder/name,value)
    items={key:{'case':row,'jev_payload':payload}}
    with run.execution_scope(SCOPE):
        expected,usage=batches.recommendations_from_receipt(receipt,{key:payload})
        assert expected[key]['requisitos'][0]['evidence_refs']==['D1','P1']
        batches.bind_saved_batch(folder,items)
        result,allocated=run.saved_batch_recommendation(tmp_path/key,items[key])
        assert result==expected[key] and allocated['promptTokenCount']==usage['promptTokenCount']
