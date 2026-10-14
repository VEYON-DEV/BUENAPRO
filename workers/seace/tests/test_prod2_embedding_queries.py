"""Prewarm once, share ten query vectors, and stop before document generation."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import embedding_queries as subject
from buenapro_worker.prod2.embedding_client import EmbeddingRequestError
from buenapro_worker.prod2.embedding_store import EmbeddingBlocked,EmbeddingIncomplete
from buenapro_worker.prod2.selected_control import LimitReached


def config():
    return {'schema_version':subject.RUNTIME,'embedding':{'model':'text-embedding-3-small','dimensions':1536,
        'tokenizer':'cl100k_base','price_per_million_usd':.02},
        'retrieval':{'queries':[{'id':'q'+str(i),'query':'requisito exacto '+str(i)} for i in range(10)]}}


class Limits:
    def __init__(self):self.reserved=[];self.issued=0;self.closed=0;self.slot=0;self.deny=False
    def reserve_request(self,provider):
        if self.deny:raise LimitReached('Budget exhausted')
        self.reserved.append(provider);owner=self
        class Permit:
            def issue(self):owner.issued+=1
            def close(self):owner.closed+=1
        return Permit()
    @contextmanager
    def model(self,key):self.slot+=1;yield


@pytest.fixture
def mocks(monkeypatch):
    calls=[];state={'cached':False,'stop':False,'error':None,'unknown':0,'pg_error':None,'after_cache':False};clients=[]
    class Client:
        def __init__(self,**kwargs):self.closed=False;clients.append(self)
        def close(self):self.closed=True
    class Store:
        def __init__(self,conn,**kwargs):calls.append(('store',kwargs))
        def ensure_vectors(self,texts,*,client,folder,before_request_factory):
            calls.append(('ensure',list(texts),folder))
            if state['error']:raise state['error']
            if not state['cached']:
                with before_request_factory('a'*64) as issue:issue()
            if state['after_cache']:state['stop']=True
            return {'vectors':[[1.]*1536]*10,'cached_count':10 if state['cached'] else 0,
                    'known_cost_usd':0 if state['cached'] else .00001,
                    'new_token_count':0 if state['cached'] else 500,
                    'attempt_ids':[] if state['cached'] else ['new-attempt'],'receipts':[],
                    'pg_verified':True,'model':'text-embedding-3-small','dimensions':1536,
                    'vector_text_sha256':[subject._text_hash(t) for t in texts],
                    'usage_unknown_attempts':state['unknown']}
    def payments(conn,hashes,model,dimensions):
        if state['pg_error']:raise state['pg_error']
        assert len(hashes)==10 and model=='text-embedding-3-small' and dimensions==1536
        return ['cache-attempt']
    monkeypatch.setattr(subject,'EmbeddingClient',Client)
    monkeypatch.setattr(subject,'EmbeddingStore',Store)
    monkeypatch.setattr(subject,'_verify_query_payments',payments)
    return calls,state,clients


def run(tmp_path,mocks,limits=None,cfg=None):
    return subject.prepare_query_vectors(cfg or config(),SimpleNamespace(openai_api_key='private-test'),
        object(),tmp_path,limits or Limits(),lambda:mocks[1]['stop'])


def test_new_queries_one_embedding_budget_no_documents_compactproof(tmp_path,mocks):
    limits=Limits();proof=run(tmp_path,mocks,limits)
    assert limits.reserved==['embedding'] and limits.issued==1 and limits.closed==1
    assert proof['query_count']==10 and proof['new_token_count']==500 and proof['known_cost_usd']==.00001
    assert proof['pg_verified'] and proof['document_provider_calls']==0 and proof['full_pdf_fallback'] is False
    assert 'vectors' not in proof and 'private-test' not in repr(proof) and 'query' not in proof
    assert mocks[2][0].closed
    assert list((tmp_path/'embedding-queries').glob('proof-*.private.json'))


def test_all_cached_zero_budget_even_limit_exhausted_and_safe_replays(tmp_path,mocks):
    mocks[1]['cached']=True;limits=Limits();limits.deny=True
    first=run(tmp_path,mocks,limits);second=run(tmp_path,mocks,limits)
    assert not limits.reserved and not limits.issued
    assert first['cached_count']==10 and first['known_cost_usd']==0 and not first['attempt_ids']
    assert first['query_set_hash']==second['query_set_hash']
    assert first['proof_path']!=second['proof_path']
    assert len(list((tmp_path/'embedding-queries').glob('proof-*.private.json')))==2


@pytest.mark.parametrize('error',[EmbeddingBlocked(['unknown']),EmbeddingIncomplete({'cost_usd':.1}),
    EmbeddingRequestError('timeout',request_sent=True),EmbeddingRequestError('guard',request_sent=False)])
def test_unknown_partial_and_request_failure_never_dispatch_document_or_proof(tmp_path,mocks,error):
    mocks[1]['error']=error;limits=Limits()
    with pytest.raises(subject.EmbeddingQueryReview,match='review'):
        run(tmp_path,mocks,limits)
    assert not limits.reserved and not list((tmp_path/'embedding-queries').glob('proof-*.private.json'))
    assert mocks[2][0].closed


def test_usage_unknown_with_vectors_still_blocks_main(tmp_path,mocks):
    mocks[1]['unknown']=1
    with pytest.raises(subject.EmbeddingQueryReview,match='usage'):
        run(tmp_path,mocks)
    assert not list((tmp_path/'embedding-queries').glob('proof-*.private.json'))


def test_cache_contains_older_unknown_payment_blocks_even_no_budget(tmp_path,mocks):
    mocks[1]['cached']=True;mocks[1]['pg_error']=subject.EmbeddingQueryReview('Older ownership uncertain')
    limits=Limits()
    with pytest.raises(subject.EmbeddingQueryReview):run(tmp_path,mocks,limits)
    assert not limits.reserved and mocks[2][0].closed


@pytest.mark.parametrize('when',['initial','after_cache'])
def test_stop_does_not_continue_from_cache_or_charge_documents(tmp_path,mocks,when):
    limits=Limits();mocks[1]['cached']=True
    if when=='initial':mocks[1]['stop']=True
    else:mocks[1]['after_cache']=True
    with pytest.raises(LimitReached):run(tmp_path,mocks,limits)
    assert not limits.reserved and not list(tmp_path.rglob('proof-*.private.json'))


def test_new_binding_queries_changed_rejected_before_store(tmp_path,mocks):
    run(tmp_path,mocks)
    changed=config();changed['retrieval']['queries'][0]['query']='nuevo texto'
    calls_before=len(mocks[0])
    with pytest.raises(ValueError,match='binding changed'):run(tmp_path,mocks,cfg=changed)
    assert len(mocks[0])==calls_before


def test_budget_denial_before_claim_does_not_become_unknown(tmp_path,mocks):
    limits=Limits();limits.deny=True
    with pytest.raises(LimitReached):run(tmp_path,mocks,limits)
    assert not limits.issued and not list((tmp_path/'embedding-queries').glob('proof-*.private.json'))
    assert mocks[2][0].closed


@pytest.mark.parametrize('change',['count','ids','model','dims','price','schema'])
def test_bad_configuration_before_clients_or_store(tmp_path,mocks,change):
    cfg=config()
    if change=='count':cfg['retrieval']['queries'].pop()
    if change=='ids':cfg['retrieval']['queries'][1]['id']='q0'
    if change=='model':cfg['embedding']['model']='other'
    if change=='dims':cfg['embedding']['dimensions']=256
    if change=='price':cfg['embedding']['price_per_million_usd']=1
    if change=='schema':cfg['schema_version']='design'
    with pytest.raises(ValueError):run(tmp_path,mocks,cfg=cfg)
    assert not mocks[0] and not mocks[2]


def test_stop_at_issue_boundary_closes_reserved_budget_without_issue(tmp_path,mocks):
    class StopLimits(Limits):
        def reserve_request(self,provider):
            permit=super().reserve_request(provider)
            mocks[1]['stop']=True
            return permit
    limits=StopLimits()
    with pytest.raises(LimitReached):run(tmp_path,mocks,limits)
    assert limits.reserved==['embedding'] and limits.issued==0 and limits.closed==1
    assert mocks[2][0].closed and not list((tmp_path/'embedding-queries').glob('proof-*.private.json'))


@pytest.mark.parametrize('status',['done','uncertain','partial_known','missing'])
def test_actual_payment_readback_contract_without_sql_service(status):
    from psycopg.pq import TransactionStatus
    rows=[{'text_sha256':'a'*64,'vector_status':'done','attempt_id':'owner','attempt_status':status,
           'provider_usage_known':True,'input_tokens':10,'cost_usd':.0000002}]
    if status=='missing':rows=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,query,params):
            assert 'prod2_embedding_vectors' in query and 'prod2_embedding_attempts' in query
            assert params==(['a'*64],'text-embedding-3-small',1536)
        def fetchall(self):return rows
    class Connection:
        info=SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        @contextmanager
        def transaction(self):yield
        def cursor(self,**kwargs):return Cursor()
    if status=='done':
        assert subject._verify_query_payments(Connection(),['a'*64],'text-embedding-3-small',1536)==['owner']
    else:
        with pytest.raises(subject.EmbeddingQueryReview):subject._verify_query_payments(Connection(),['a'*64],'text-embedding-3-small',1536)
