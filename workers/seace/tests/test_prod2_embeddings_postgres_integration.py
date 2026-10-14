"""Opt-in committed scratch schema: real ownership/restart/provenance contracts.

Only an ephemeral schema is written. No source/provider/R2 calls, public rows or
application migration ledger writes; the schema is dropped even after failure.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2.embedding_client import EmbeddingClient, token_count, EmbeddingRequestError
from buenapro_worker.prod2 import embedding_store as subject
from buenapro_worker.prod2.runtime import production_settings

pytestmark = pytest.mark.skipif(os.environ.get('BUENAPRO_PG_SCRATCH_TEST') != '1',
                                reason='Explicit PostgreSQL isolated scratch opt-in required')


def vec(): return [1.] + [0.] * 1535


@pytest.fixture
def scratch():
    root=Path(__file__).resolve().parents[3]
    schema='prod2_embeddings_scratch_'+uuid4().hex
    with production_settings(ssh_host='ubuntu@168.129.177.199',ssh_key=root/'credentials/ssh-key-2026-07-14.key') as settings:
        with psycopg.connect(settings.database_url,row_factory=dict_row,autocommit=True,connect_timeout=15) as conn:
            locked=conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()['locked']
            if not locked:
                pytest.fail('Another PROD2 coordinator owns the executor',pytrace=False)
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            try:
                subject.prepare_schema(conn,root/'db/migrations'/subject.MIGRATION)
                yield conn, settings.database_url, schema
            finally:
                conn.rollback()
                conn.execute('SET search_path TO public')
                conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                assert conn.execute('SELECT to_regnamespace(%s) AS ns',(schema,)).fetchone()['ns'] is None
                conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")


def client_for(handler):
    return EmbeddingClient(api_key='synthetic-only',http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def success(request):
    texts=json.loads(request.content)['input'];count=sum(token_count(t) for t in texts)
    return httpx.Response(200,json={'model':'text-embedding-3-small',
        'data':[{'index':i,'embedding':vec()} for i in range(len(texts))],
        'usage':{'prompt_tokens':count,'total_tokens':count}})


def corpus_for(texts):
    chunks=[{'chunk_id':sha256(('chunk'+t).encode()).hexdigest(),'text':t,'text_sha256':sha256(t.encode()).hexdigest(),
             'token_count':token_count(t),'block_ids':['b'+str(i)],'global_pages':[i+1],'section_id':'s'} for i,t in enumerate(texts)]
    return {'corpus_hash':sha256(json.dumps(chunks).encode()).hexdigest(),'policy_version':'prod2_complete_embedding_corpus_v1',
            'chunks':chunks,'blocks':[{'id':'b'+str(i),'text':t,'global_page':i+1,'locations':[{'page':i+1}]} for i,t in enumerate(texts)],
            'coverage_complete':True,'unresolved_pages':[]}


def retrieval_for(corpus):
    context='\n'.join(chunk['text'] for chunk in corpus['chunks'])
    return {'retrieval_hash':sha256(('retrieval'+context).encode()).hexdigest(),'context_text':context,
            'context_sha256':sha256(context.encode()).hexdigest(),'selected_chunk_ids':[chunk['chunk_id'] for chunk in corpus['chunks']],
            'citations':[{'block_id':block['id'],'page':block['global_page']} for block in corpus['blocks']],
            'needs_review':False}


def test_durable_before_dispatch_global_duplicate_and_readback_tamper(scratch,tmp_path):
    conn,dsn,schema=scratch;store=subject.EmbeddingStore(conn);calls=[]
    def request(req):
        with psycopg.connect(dsn,row_factory=dict_row,autocommit=True) as observer:
            observer.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            rows=observer.execute("SELECT * FROM prod2_embedding_vectors WHERE status='running'").fetchall()
            assert len(rows)==2 and {r['exact_text'] for r in rows}=={'consorcio permitido','experiencia diez años'}
        calls.append(1);return success(req)
    texts=['consorcio permitido','experiencia diez años']
    result=store.ensure_vectors(texts+[texts[0]],client=client_for(request),folder=tmp_path)
    assert len(calls)==1 and result['pg_verified'] and result['deduplicated_count']==1
    subject.EmbeddingStore(conn).ensure_vectors(list(reversed(texts)),client=client_for(request),folder=tmp_path/'different')
    assert len(calls)==1
    corpus=corpus_for(texts);receipt=subject.save_corpus(conn,corpus,convocation_id=1)
    assert receipt['corpus_id']>0
    assert subject.save_corpus(conn,{**corpus,'cache_hit':True},convocation_id=1)['corpus_id']==receipt['corpus_id']
    retrieval=retrieval_for(corpus)
    proof=subject.save_retrieval(conn,corpus['corpus_hash'],retrieval,convocation_id=1)
    assert proof['vectors_persisted'] and proof['blocks_persisted'] and proof['retrieval_id']>0
    with conn.transaction():
        assert subject.verified_corpus_retrieval(conn,corpus['corpus_hash'],retrieval['retrieval_hash'],allow_transaction=True)['retrieval_id']==proof['retrieval_id']
    with pytest.raises(subject.EmbeddingPersistenceError,match='immutable'):
        subject.save_corpus(conn,{**corpus,'coverage_complete':False},convocation_id=1)
    conn.execute("UPDATE prod2_embedding_chunks SET exact_text='altered' WHERE corpus_hash=%s AND ordinal=0",(corpus['corpus_hash'],))
    with pytest.raises(subject.EmbeddingPersistenceError,match='changed'):
        subject.verified_corpus_retrieval(conn,corpus['corpus_hash'],retrieval['retrieval_hash'])


def test_two_workers_same_vector_one_call_other_blocked(scratch,tmp_path):
    conn,dsn,schema=scratch;entered,release=Event(),Event();calls=[]
    def run_one():
        with psycopg.connect(dsn,row_factory=dict_row,autocommit=True) as owned:
            owned.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            def handler(req):
                calls.append(1);entered.set()
                assert release.wait(15)
                return success(req)
            return subject.EmbeddingStore(owned).ensure_vectors(['same exact fragment'],client=client_for(handler),folder=tmp_path/'one')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run_one)
        assert entered.wait(15)
        try:
            with pytest.raises(subject.EmbeddingBlocked):
                subject.EmbeddingStore(conn).ensure_vectors(['same exact fragment'],client=client_for(success),folder=tmp_path/'two')
        finally:
            release.set()
        assert future.result(timeout=15)['pg_verified']
    assert len(calls)==1
    assert subject.EmbeddingStore(conn).ensure_vectors(['same exact fragment'],client=client_for(success),folder=tmp_path/'three')['cached_count']==1


def test_unknown_blocks_repartition_partial_keeps_good_and_restart_no_repay(scratch,tmp_path,monkeypatch):
    conn,dsn,schema=scratch;store=subject.EmbeddingStore(conn);calls=[]
    def timeout(req): calls.append(1);raise httpx.ReadTimeout('synthetic')
    with pytest.raises(EmbeddingRequestError):
        store.ensure_vectors(['unknown1','unknown2'],client=client_for(timeout),folder=tmp_path/'unknown')
    with pytest.raises(subject.EmbeddingBlocked):
        store.ensure_vectors(['unknown2'],client=client_for(success),folder=tmp_path/'other')
    unknown=conn.execute("SELECT * FROM prod2_embedding_attempts WHERE status='uncertain'").fetchone()
    assert unknown['cost_usd'] is None and not unknown['provider_usage_known']
    def partial(req):
        calls.append(1);response=success(req).json();response['data'].pop()
        return httpx.Response(200,json=response)
    with pytest.raises(subject.EmbeddingIncomplete):
        store.ensure_vectors(['good','missing'],client=client_for(partial),folder=tmp_path/'partial')
    assert store.ensure_vectors(['good'],client=client_for(success),folder=tmp_path/'good')['cached_count']==1
    with pytest.raises(subject.EmbeddingBlocked):
        store.ensure_vectors(['missing'],client=client_for(success),folder=tmp_path/'missing')
    assert len(calls)==2
    original=store._complete
    monkeypatch.setattr(store,'_complete',lambda *a:(_ for _ in ()).throw(RuntimeError('after response')))
    with pytest.raises(RuntimeError):
        store.ensure_vectors(['crash persisted response'],client=client_for(success),folder=tmp_path/'restart')
    monkeypatch.setattr(store,'_complete',original)
    assert store.ensure_vectors(['crash persisted response'],client=client_for(timeout),folder=tmp_path/'restart')['cached_count']==1
    assert len(calls)==2


def test_pre_send_zero_can_new_claim_migration_idempotent(scratch,tmp_path):
    conn,_,_=scratch;store=subject.EmbeddingStore(conn)
    def refuse(): raise RuntimeError('before send')
    with pytest.raises(EmbeddingRequestError):
        store.ensure_vectors(['guarded'],client=client_for(success),folder=tmp_path,before_request=refuse)
    row=conn.execute("SELECT * FROM prod2_embedding_attempts WHERE status='failed_pre_send'").fetchone()
    assert row['input_tokens']==0 and row['cost_usd']==0 and row['provider_usage_known'] and not row['request_sent']
    result=store.ensure_vectors(['guarded'],client=client_for(success),folder=tmp_path)
    assert result['new_token_count']>0
    migration=Path(__file__).resolve().parents[3]/'db/migrations'/subject.MIGRATION
    ledger=conn.execute('SELECT * FROM schema_migrations').fetchall()
    subject.prepare_schema(conn,migration)
    assert conn.execute('SELECT * FROM schema_migrations').fetchall()==ledger
