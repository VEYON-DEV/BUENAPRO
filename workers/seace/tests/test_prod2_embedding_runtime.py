"""Runner contract, paid boundaries and actual durable reduced-input smoke."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from buenapro_worker.prod2 import embedding_runtime as runtime
from buenapro_worker.prod2.input_policy import prepare_input, read_verified_retrieved_text
from buenapro_worker.prod2.selected_control import RunLimits, LimitReached

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('embedding_runtime_runner', ROOT / 'scripts/run_prod2_selected.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def options(folder, *values):
    return runner.parser().parse_args(['--folder',str(folder),'--llm1-input','embeddings',*values])


def test_explicit_route_and_config_frozen_old_identity_preserved(tmp_path):
    args=options(tmp_path)
    requested=runner.plan(args)
    assert requested['llm1_input_policy']==runtime.INPUT_POLICY
    assert runtime.validate_frozen_config(requested)['embedding']['model']=='text-embedding-3-small'
    manifest={**requested,'requested':requested,'run_id':'synthetic','commercial_profiles':[],
              'gemini_model':'gemini-3.1-flash-lite','keyword_tracking':{'sweep_id':'synthetic'}}
    modern=runner.staging_identity(manifest)
    old={k:v for k,v in manifest.items() if k not in ('embedding_reading','llm1_input_policy')}
    old_identity=runner.staging_identity(old)
    assert modern!=old_identity
    assert old_identity==runner.staging_identity({**old,'llm1_input_policy':'prod2_native_text_or_pdf_v1'})
    runner.private_write(tmp_path/'manifest.private.json',manifest)
    assert runner.plan(args)==requested
    args.llm1_input='native'
    with pytest.raises(ValueError,match='new folder'):
        runner.plan(args)


def test_changed_frozen_config_blocks_before_providers(tmp_path):
    requested=runner.plan(options(tmp_path))
    mutated=deepcopy(requested)
    mutated['embedding_reading']['config']['retrieval']['neighbor_pages']=0
    with pytest.raises(ValueError,match='Frozen embedding'):
        runtime.validate_frozen_config(mutated)
    with pytest.raises(ValueError,match='qualification'):
        runner.plan(options(tmp_path,'--llm1-scope','legacy'))


def test_new_v4_wire_policy_is_frozen_without_reinterpreting_v3(tmp_path):
    args=options(tmp_path,'--embedding-config',str(ROOT/'docs/sources/prod2-embedding-reading.runtime.v4.json'))
    request=runner.plan(args)
    assert request['llm1_retrieved_transport_policy']=='prod2_retrieved_compact_metadata_v4'
    assert runtime.validate_frozen_config(request)['corpus']['ocr_textual_scan_policy'].endswith('v3')
    missing=deepcopy(request)
    missing.pop('llm1_retrieved_transport_policy')
    with pytest.raises(ValueError,match='transport'):
        runtime.validate_frozen_config(missing)
    old=runner.plan(options(tmp_path,'--embedding-config',str(ROOT/'docs/sources/prod2-embedding-reading.runtime.v3.json')))
    assert 'llm1_retrieved_transport_policy' not in old
    runtime.validate_frozen_config(old)


def test_embedding_budget_reserves_before_issue_and_stop_costs_zero():
    limits=RunLimits(max_accepted=10,max_candidates=100,max_jev_calls=120,max_llm1_calls=10,max_embedding_calls=1)
    with runtime.embedding_permission(limits) as issue:
        with pytest.raises(LimitReached):
            limits.reserve_request('embedding')
        # Cached decisions close an unused permit without a paid call.
    assert limits.calls['embedding']==0 and limits.reserved['embedding']==0
    with runtime.embedding_permission(limits,stop_requested=lambda:True) as issue:
        with pytest.raises(LimitReached,match='stopped'):
            issue()
    assert limits.calls['embedding']==0
    with runtime.embedding_permission(limits) as issue:
        issue()
    assert limits.calls['embedding']==1
    with pytest.raises(LimitReached):
        limits.reserve_request('embedding')


def test_resume_source_scope_cannot_silently_change(tmp_path,monkeypatch):
    from buenapro_worker.prod2 import embedding_resume
    requested=runner.plan(options(tmp_path,'--embedding-resume-from',str(tmp_path/'old')))
    previous=deepcopy(requested)
    fake={'source_manifest':previous,'handled_count':1}
    monkeypatch.setattr(embedding_resume,'load_resume_binding',lambda _:fake)
    assert runner.embedding_resume_binding(requested,tmp_path)==fake
    previous['keywords']=['different']
    with pytest.raises(ValueError,match='keywords'):
        runner.embedding_resume_binding(requested,tmp_path)


def test_selected_reading_memoizes_reference_expansion(tmp_path,monkeypatch):
    # The memo keeps context and hash (test_prod2_retrieval_expansion_recovery); a 249-page corpus took 403 s without it.
    from datetime import datetime,timezone
    code='00000000-0000-4000-8000-000000000001'
    detail={'documents':[{'uuid':code,'filename':'Bases.pdf','type':'Bases','stage':'Convocatoria',
        'published_at':'06/10/2026 10:00'}],'schedule':[],'item_states':[],'document_inventory_complete':True,
        'description':'SOFTWARE'}
    monkeypatch.setattr(runner,'persist_detail',lambda *a,**kw:{'detail_hash':'a'*64})
    monkeypatch.setattr(runner,'decide',lambda *a:{'state':'unknown'})
    monkeypatch.setattr(runner,'save_participation_assessment',lambda *a,**kw:None)
    monkeypatch.setattr(runner,'download',lambda *a:(tmp_path/'Bases.pdf',{'sha256':'b'*64}))
    monkeypatch.setattr(runner,'paid_document_selection',lambda *a,**kw:({'selected_ids':['loose:'+code],
        'pending_ids':[],'excluded_ids':[],'cost_usd':0,'raw_response':{},'decisions':[],'thresholds':{},
        'requires_human_review':False},True))
    monkeypatch.setattr(runner,'prepare_selected_document_set',lambda *a,**kw:{})
    seen={}
    class Reached(Exception):
        pass
    def retrieved(*a,**kw):
        seen.update(kw)
        raise Reached
    monkeypatch.setattr(runtime,'prepare_retrieved_input',retrieved)
    manifest=runner.plan(options(tmp_path))|{'profile_id':'00000000-0000-4000-8000-000000000002'}
    item={'row':{'convocation_id':1,'description':'SOFTWARE'},'query':{'object_type':'service'}}
    capture={'observed_at':datetime.now(timezone.utc).isoformat(),'detail':detail}
    limits=RunLimits(max_accepted=1,max_candidates=1,max_jev_calls=1,max_llm1_calls=1)
    with pytest.raises(Reached):
        runner.enrich(None,SimpleNamespace(gemini_model='gemini-3.1-flash-lite'),tmp_path,item,manifest,runner.SharedPacer(2),limits,'k',
                      capture=capture)
    assert seen['memoize_expansion'] is True


# The fixture uses a committed private schema and holds the global executor.
# The only HTTP transport is synthetic; no real embedding or generation fees.
from test_prod2_embeddings_postgres_integration import scratch, success
import os


@pytest.mark.skipif(os.environ.get('BUENAPRO_PG_SCRATCH_TEST')!='1',reason='Explicit isolated PostgreSQL opt-in')
def test_actual_durable_runtime_text_transport_and_free_replay(scratch,tmp_path,monkeypatch):
    import hashlib
    import httpx
    from buenapro_worker.prod2 import embedding_client, embedding_store
    from test_prod2_embedding_corpus_retrieval import pdf_bytes
    conn,dsn,schema=scratch
    content=pdf_bytes(['Objeto: desarrollo de software. Requisitos de calificacion. Experiencia del postor 100000 soles.',
                      'Personal clave: dos ingenieros de sistemas con tres anos de experiencia. Consorcio permitido.',
                      'Subcontratacion prohibida. Entregables: aplicacion web en 90 dias. Conformidad del area usuaria.'])
    pdf=tmp_path/'analysis.pdf';pdf.write_bytes(content)
    prepared={'pdf_path':pdf,'analysis_sha256':hashlib.sha256(content).hexdigest(),'analysis_bytes':len(content),
        'page_count':3,'source_document_manifest':[{'candidate_id':'base-1','document_uuid':'official-uuid-1',
        'original_sha256':'a'*64,'analysis_sha256':'b'*64,'filename':'bases.pdf','start_page':1,'end_page':3,'page_offset':0}],
        'selection_context':{'llm1_input_policy':runtime.INPUT_POLICY,'llm1_scope_policy':'prod2_qualification_focus_v1'}}
    config=runtime.load_config(ROOT/'docs/sources/prod2-embedding-reading.runtime.v1.json')
    limits=RunLimits(max_accepted=10,max_candidates=100,max_jev_calls=120,max_llm1_calls=10,max_embedding_calls=2)
    actual_client=embedding_client.EmbeddingClient
    requests=[]
    def http(req):
        requests.append(req)
        return success(req)
    def client(**kwargs):
        return actual_client(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(http)))
    monkeypatch.setattr(embedding_client,'EmbeddingClient',client)
    settings=SimpleNamespace(openai_api_key='synthetic-only')
    result=runtime.prepare_retrieved_input(conn,settings,prepared,tmp_path,limits,config,convocation_id=1)
    transport,local=prepare_input(result,tmp_path)
    text=read_verified_retrieved_text(local,transport)
    proof=transport['llm1_input']
    assert proof['indexed_complete'] and 'Experiencia' in text
    assert len(requests)==limits.calls['embedding']==1 and limits.calls['llm1']==0
    assert embedding_store.verified_corpus_retrieval(conn,proof['corpus_hash'],proof['retrieval_hash'],expected_input=proof)['pg_verified']
    replay=runtime.prepare_retrieved_input(conn,settings,prepared,tmp_path,limits,config,convocation_id=1)
    replay,_=prepare_input(replay,tmp_path)
    assert replay['llm1_input']==proof and len(requests)==1
    conn.execute("UPDATE prod2_embedding_retrievals SET retrieval_json=jsonb_set(retrieval_json,'{context_text}', '\"altered\"'::jsonb) WHERE retrieval_hash=%s",(proof['retrieval_hash'],))
    with pytest.raises(embedding_store.EmbeddingPersistenceError):
        embedding_store.verified_corpus_retrieval(conn,proof['corpus_hash'],proof['retrieval_hash'],expected_input=proof)
