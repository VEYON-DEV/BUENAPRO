"""Prewarm the ten qualification queries once before concurrent consumers/OCR.

Cache identity remains exact UTF-8 text/model/dimensions. This stage never
passes documents to a provider and never falls back to a full-PDF generation.
Unknown or partial payments stop preparation without automatic repayment.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from .batch_runner import private_write, read_private
from .embedding_client import EmbeddingClient, EmbeddingRequestError
from .embedding_store import EmbeddingStore, EmbeddingBlocked, EmbeddingIncomplete
from .selected_control import LimitReached

VERSION = 'prod2_embedding_query_preparation_v1'
RUNTIME = 'prod2_embedding_reading_runtime_v1'


class EmbeddingQueryReview(RuntimeError):
    """Paid receipts stay available; main must not dispatch document reading."""


def _json(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def _hash(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _text_hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def _query_binding(config):
    if not isinstance(config,dict) or config.get('schema_version')!=RUNTIME:
        raise ValueError('Query preparation requires the explicit runtime configuration')
    embedding=config.get('embedding',{})
    if (embedding.get('model')!='text-embedding-3-small' or embedding.get('dimensions')!=1536
            or embedding.get('tokenizer')!='cl100k_base'
            or embedding.get('price_per_million_usd')!=.02):
        raise ValueError('Query embedding model, dimensions or price contract invalid')
    queries=config.get('retrieval',{}).get('queries')
    if not isinstance(queries,list) or len(queries)!=10:
        raise ValueError('Qualification query preparation requires ten queries')
    if any(not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id']
           or not isinstance(row.get('query'),str) or not row['query'].strip() for row in queries):
        raise ValueError('Qualification query identities/text invalid')
    ids=[row['id'] for row in queries]
    if len(set(ids))!=len(ids):
        raise ValueError('Qualification query identities must be distinct')
    texts=[row['query'] for row in queries]
    identity={'model':embedding['model'],'dimensions':embedding['dimensions'],
              'tokenizer':embedding['tokenizer'],
              'queries':[{'id':row['id'],'text_sha256':_text_hash(row['query'])} for row in queries]}
    return embedding,queries,texts,{'version':VERSION,'model':embedding['model'],
        'dimensions':embedding['dimensions'],'query_ids':ids,
        'query_text_sha256':[_text_hash(text) for text in texts],
        'query_set_hash':_hash(identity),'config_sha256':_hash(config)}


def _verify_query_payments(conn,hashes,model,dimensions):
    """Cached query vectors cannot hide an older uncertain/partial attempt."""
    if conn.info.transaction_status!=TransactionStatus.IDLE:
        raise EmbeddingQueryReview('Query vector readback requires an IDLE connection')
    keys=set(hashes)
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT v.text_sha256,v.status AS vector_status,a.attempt_id,
            a.status AS attempt_status,a.provider_usage_known,a.input_tokens,a.cost_usd
            FROM prod2_embedding_vectors v JOIN prod2_embedding_attempts a USING(attempt_id)
            WHERE v.text_sha256=ANY(%s) AND v.model=%s AND v.dimensions=%s''',
            (sorted(keys),model,dimensions))
        rows=cur.fetchall()
    if ({row['text_sha256'] for row in rows}!=keys or any(
            row['vector_status']!='done' or row['attempt_status']!='done'
            or row['provider_usage_known'] is not True or row['input_tokens'] is None or row['cost_usd'] is None
            for row in rows)):
        raise EmbeddingQueryReview('Query vectors have pending or uncertain paid ownership')
    return sorted({str(row['attempt_id']) for row in rows})


def _stop(stop_requested):
    if stop_requested():
        raise LimitReached('Coordinator stopped during query embedding preparation')


def prepare_query_vectors(config,settings,conn,folder,limits,stop_requested=lambda:False):
    """Return compact proof, never vectors or credentials in printable results."""
    _stop(stop_requested)
    embedding,queries,texts,binding=_query_binding(config)
    folder=Path(folder).absolute()
    if folder.is_symlink() or any(parent.is_symlink() for parent in folder.parents):
        raise ValueError('Query preparation needs a literal private folder')
    key=getattr(settings,'openai_api_key','')
    if not isinstance(key,str) or not key.strip():
        raise ValueError('Query embedding credential unavailable')
    private=folder/'embedding-queries'
    if private.is_symlink():
        raise ValueError('Query preparation private path cannot be symlinked')
    binding_path=private/'binding.private.json'
    if binding_path.exists():
        if read_private(binding_path)!=binding:
            raise ValueError('Frozen qualification query binding changed')
    else:
        private_write(binding_path,binding)
    store=EmbeddingStore(conn,model=embedding['model'],dimensions=embedding['dimensions'],
                         price_per_million=embedding['price_per_million_usd'])
    @contextmanager
    def permission(request_hash):
        # The store calls this before claiming a batch, and not for cache hits.
        _stop(stop_requested)
        permit=limits.reserve_request('embedding')
        try:
            def issue():
                _stop(stop_requested)
                permit.issue()
            yield issue
        finally:
            permit.close()
    _stop(stop_requested)
    client=EmbeddingClient(api_key=key,model=embedding['model'],dimensions=embedding['dimensions'],
                           price_per_million=embedding['price_per_million_usd'])
    try:
        with limits.model('embedding-queries:'+binding['query_set_hash']):
            result=store.ensure_vectors(texts,client=client,folder=private,
                                       before_request_factory=permission)
        _stop(stop_requested)
        if (result.get('pg_verified') is not True or len(result.get('vectors',[]))!=len(texts)
                or result.get('model')!=embedding['model'] or result.get('dimensions')!=embedding['dimensions']
                or result.get('vector_text_sha256')!=binding['query_text_sha256']
                or result.get('usage_unknown_attempts',0)!=0):
            raise EmbeddingQueryReview('Qualification query vectors or usage not fully confirmed')
        all_attempts=_verify_query_payments(conn,binding['query_text_sha256'],embedding['model'],embedding['dimensions'])
        _stop(stop_requested)
    except LimitReached:
        raise
    except EmbeddingRequestError as error:
        if not error.request_sent and stop_requested():
            raise LimitReached('Coordinator stopped before query embedding request') from None
        raise EmbeddingQueryReview('Query embedding request needs review; no automatic retry or document fallback') from None
    except (EmbeddingBlocked,EmbeddingIncomplete):
        raise EmbeddingQueryReview('Query embedding ownership or response needs review; no automatic retry') from None
    except EmbeddingQueryReview:
        raise
    except Exception:
        raise EmbeddingQueryReview('Query embedding preparation failed; preserve receipts and stop document dispatch') from None
    finally:
        client.close()
    proof={**binding,'query_count':len(queries),'cached_count':result['cached_count'],
           'new_token_count':result['new_token_count'],'known_cost_usd':result['known_cost_usd'],
           'attempt_ids':result['attempt_ids'],'all_cached_attempt_ids':all_attempts,
           'receipts':result.get('receipts',[]),'usage_unknown_attempts':0,'pg_verified':True,
           'document_provider_calls':0,'full_pdf_fallback':False,
           'verified_at':datetime.now(timezone.utc).isoformat()}
    path=private/('proof-'+str(time.time_ns())+'.private.json')
    private_write(path,proof)
    return {**proof,'proof_path':str(path)}
