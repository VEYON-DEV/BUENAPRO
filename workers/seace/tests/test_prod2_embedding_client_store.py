"""Offline provider boundary and paid restart/cache contracts, never real API."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
from psycopg.pq import TransactionStatus
import pytest

from buenapro_worker.prod2 import embedding_client as client_module
from buenapro_worker.prod2 import embedding_store as subject


def vector(n=1536):
    return [1.0] + [0.0] * (n - 1)


def http_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def success_data(texts, *, known=True):
    return {"model": client_module.MODEL,
            "data": [{"index": i, "embedding": vector()} for i in range(len(texts))],
            "usage": {"prompt_tokens": sum(client_module.token_count(t) for t in texts),
                      "total_tokens": sum(client_module.token_count(t) for t in texts)} if known else {}}


@pytest.mark.parametrize('change', ['missing_index','duplicate','wrong_dimension','nan','model','usage','usage_bool'])
def test_client_reports_partial_paid_response_without_retry(change):
    requests = []
    data = success_data(['uno', 'dos'])
    if change == 'missing_index': data['data'].pop()
    if change == 'duplicate': data['data'].append(deepcopy(data['data'][0]))
    if change == 'wrong_dimension': data['data'][0]['embedding'] = [1]
    if change == 'nan': data['data'][0]['embedding'][0] = float('nan')
    if change == 'model': data['model'] = 'other'
    if change == 'usage': data['usage'] = {}
    if change == 'usage_bool': data['usage'] = {'prompt_tokens':True,'total_tokens':True}
    def post(request):
        requests.append(request)
        if change == 'nan':
            return httpx.Response(200,content=json.dumps(data).encode())
        return httpx.Response(200,json=data)
    client = client_module.EmbeddingClient(api_key='private-test', http_client=http_client(post))
    result = client.embed(['uno','dos'])
    assert len(requests) == 1
    if change in ('usage','usage_bool'):
        assert result['complete'] is True
        assert result['provider_usage_known'] is False and result['cost_usd'] is None
    else:
        assert result['complete'] is False
        assert result['provider_usage_known'] is True
        assert result['cost_usd'] > 0
    assert 'private-test' not in repr(result)


def test_client_order_dimensions_and_exact_usage():
    def handler(request):
        sent = json.loads(request.content)
        assert sent['input'] == ['hola', 'consorcio']
        assert sent['dimensions'] == 1536 and sent['encoding_format'] == 'float'
        data = success_data(sent['input'])
        data['data'].reverse()
        return httpx.Response(200, json=data)
    client = client_module.EmbeddingClient(api_key='private-test',http_client=http_client(handler))
    result = client.embed(['hola','consorcio'])
    assert set(result['vectors_by_index']) == {0,1}
    assert result['cost_usd'] == result['input_tokens'] * .02/1_000_000
    assert result['local_token_count'] == result['input_tokens']


@pytest.mark.parametrize('mode', ['timeout','http','bad_json'])
def test_client_after_dispatch_unknown_never_zero(mode):
    called = []
    def handler(request):
        called.append(1)
        if mode=='timeout': raise httpx.ReadTimeout('secret response location')
        if mode=='http': return httpx.Response(429,json={'error':{'message':'secret'}})
        return httpx.Response(200,content=b'notjson')
    client = client_module.EmbeddingClient(api_key='private-test',http_client=http_client(handler))
    with pytest.raises(client_module.EmbeddingRequestError) as error:
        client.embed(['contenido'])
    assert error.value.request_sent is True and len(called)==1
    assert 'secret' not in str(error.value)


def test_client_limits_and_budget_guard_are_proven_pre_send():
    calls=[]
    client=client_module.EmbeddingClient(api_key='private-test',http_client=http_client(lambda r:calls.append(r)))
    for texts in ([],['x']*65,['x '*9000],['x '*8000]*9):
        with pytest.raises(client_module.EmbeddingRequestError) as e: client.embed(texts)
        assert e.value.request_sent is False
    def refuse(): raise RuntimeError('limit secret')
    with pytest.raises(client_module.EmbeddingRequestError) as e: client.embed(['ok'],before_request=refuse)
    assert e.value.request_sent is False and not calls


class MemoryStore(subject.EmbeddingStore):
    """SQL-independent owner emulator for orchestration; actual SQL has scratch tests."""
    def __init__(self, state=None):
        self.state = state if state is not None else {'rows':{},'claims':{},'responses':{},'failed':{}}
        super().__init__(SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE)))
    def _rows(self,keys):
        return deepcopy({k:r for k,r in self.state['rows'].items() if k in keys})
    def _claim(self,texts):
        blocked=[r['attempt_id'] for t in texts if (r:=self.state['rows'].get(subject._text_sha(t))) and r['status'] not in ('done','failed_pre_send')]
        if blocked: raise subject.EmbeddingBlocked(blocked)
        texts=[t for t in texts if self.state['rows'].get(subject._text_sha(t),{}).get('status')!='done']
        if not texts: return None
        identity={'model':self.model,'dimensions':self.dimensions,'texts':texts,'text_sha256':[subject._text_sha(t) for t in texts]}
        claim={'attempt_id':str(uuid4()),'claim_token':str(uuid4()),'request_hash':subject._digest(identity),'input':identity,'version':'prod2_embedding_claim_v1'}
        self.state['claims'][claim['attempt_id']]=claim
        for t in texts:
            self.state['rows'][subject._text_sha(t)]={'exact_text':t,'status':'running','attempt_id':claim['attempt_id'],'vector':None}
        return claim
    def _complete(self,claim,response):
        assert self.state['claims'][claim['attempt_id']]==claim
        if claim['attempt_id'] in self.state['responses']:
            assert self.state['responses'][claim['attempt_id']]==response
            return
        self.state['responses'][claim['attempt_id']]=response
        for i,key in enumerate(claim['input']['text_sha256']):
            v=response['vectors_by_index'].get(i,response['vectors_by_index'].get(str(i)))
            self.state['rows'][key].update(status='done' if v else ('partial_known' if response['provider_usage_known'] else 'uncertain'),vector=v)
    def _failed(self,claim,error):
        self.state['failed'][claim['attempt_id']]={'sent':error.request_sent,'known':not error.request_sent,'cost':None if error.request_sent else 0}
        for key in claim['input']['text_sha256']:
            self.state['rows'][key]['status']='uncertain' if error.request_sent else 'failed_pre_send'


def counting_client(calls, **kwargs):
    def handler(request):
        data=json.loads(request.content)
        calls.append(data['input'])
        return httpx.Response(200,json=success_data(data['input'],**kwargs))
    return client_module.EmbeddingClient(api_key='private-test',http_client=http_client(handler))


def test_store_duplicate_texts_global_cache_and_different_folder(tmp_path):
    calls=[];store=MemoryStore();client=counting_client(calls)
    first=store.ensure_vectors(['uno','uno','dos'],client=client,folder=tmp_path/'first')
    assert calls==[['uno','dos']] and first['deduplicated_count']==1
    second=MemoryStore(store.state).ensure_vectors(['dos','uno'],client=client,folder=tmp_path/'second')
    assert len(calls)==1 and second['cached_count']==2 and second['known_cost_usd']==0
    assert second['vectors']==[vector(),vector()]
    assert first['known_cost_usd']>0


def test_store_claim_visible_before_http_and_factory_entered_before_claim(tmp_path):
    calls=[];store=MemoryStore();timeline=[]
    @contextmanager
    def factory(request_hash):
        assert len(request_hash)==64 and not store.state['claims']
        timeline.append('budget')
        def issue():
            assert store.state['claims'] and all(r['status']=='running' for r in store.state['rows'].values())
            timeline.append('sent')
        yield issue
        timeline.append('close')
    store.ensure_vectors(['consorcio'],client=counting_client(calls),folder=tmp_path,before_request_factory=factory)
    assert timeline==['budget','sent','close']
    timeline.clear()
    store.ensure_vectors(['consorcio'],client=counting_client(calls),folder=tmp_path,before_request_factory=factory)
    assert not timeline


def test_factory_exhausted_creates_no_claim_or_request(tmp_path):
    store=MemoryStore();calls=[]
    @contextmanager
    def exhausted(key):
        raise RuntimeError('no budget')
        yield
    with pytest.raises(RuntimeError):
        store.ensure_vectors(['x'],client=counting_client(calls),folder=tmp_path,before_request_factory=exhausted)
    assert not store.state['claims'] and not calls and not list(tmp_path.iterdir())


def test_restart_received_answer_reconciles_without_request(tmp_path,monkeypatch):
    store=MemoryStore();calls=[];client=counting_client(calls)
    monkeypatch.setattr(store,'_complete',lambda claim,response:(_ for _ in ()).throw(RuntimeError('crash after response')))
    with pytest.raises(RuntimeError): store.ensure_vectors(['dato'],client=client,folder=tmp_path)
    assert len(calls)==1
    restarted=MemoryStore(store.state)
    result=restarted.ensure_vectors(['dato'],client=client,folder=tmp_path)
    assert len(calls)==1 and result['cached_count']==1 and result['known_cost_usd']==0


def test_timeout_blocks_repartitioned_batches_and_retains_input(tmp_path):
    store=MemoryStore();calls=[]
    def timeout(request):
        calls.append(1)
        raise httpx.ReadTimeout('invisible secret')
    client=client_module.EmbeddingClient(api_key='private-test',http_client=http_client(timeout))
    with pytest.raises(client_module.EmbeddingRequestError): store.ensure_vectors(['uno','dos'],client=client,folder=tmp_path)
    with pytest.raises(subject.EmbeddingBlocked): MemoryStore(store.state).ensure_vectors(['dos'],client=client,folder=tmp_path/'other')
    assert len(calls)==1
    claim=json.loads(next(tmp_path.glob('*.claim.private.json')).read_text())
    assert claim['input']['texts']==['uno','dos']
    outcome=json.loads(next(tmp_path.glob('*.outcome.private.json')).read_text())
    assert outcome['cost_usd'] is None and outcome['usage_known'] is False


@pytest.mark.parametrize('status,provider_code', [
    (400, 'invalid_request_error'), (401, 'invalid_api_key'),
    (403, None), (429, 'insufficient_quota'),
    (500, 'server_error'), (503, 'server_is_overloaded'),
])
def test_http_failure_retains_safe_diagnostics_without_zero_cost_or_retry(tmp_path, status, provider_code):
    calls = []
    secret = 'do-not-retain-message-or-body'
    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={
            'error': {'code': provider_code, 'message': secret, 'type': secret, 'param': secret},
            'usage': {'prompt_tokens': 0, 'total_tokens': 0}, 'body': secret,
        })
    store = MemoryStore()
    client = client_module.EmbeddingClient(api_key='private-test', http_client=http_client(handler))
    with pytest.raises(client_module.EmbeddingRequestError) as caught:
        store.ensure_vectors(['contenido'], client=client, folder=tmp_path)
    error = caught.value
    assert error.code == 'embedding_http_error' and str(error) == 'embedding_http_error'
    assert error.http_status == status and error.provider_error_code == provider_code
    assert secret not in json.dumps(vars(error))
    raw = next(tmp_path.glob('*.outcome.private.json')).read_text()
    outcome = json.loads(raw)
    assert outcome['http_status'] == status and outcome['provider_error_code'] == provider_code
    assert outcome['status'] == 'uncertain' and outcome['request_sent'] is True
    assert outcome['usage_known'] is False and outcome['provider_usage_known'] is False
    assert outcome['input_tokens'] is None and outcome['cost_usd'] is None
    assert secret not in raw and 'private-test' not in raw
    assert 'contenido' not in raw and 'error_message' not in outcome and 'response' not in outcome
    assert not list(tmp_path.glob('*.response.private.json'))
    claim = json.loads(next(tmp_path.glob('*.claim.private.json')).read_text())
    started = json.loads(next(tmp_path.glob('*.started.private.json')).read_text())
    assert claim['version'] == 'prod2_embedding_claim_v1'
    assert claim['request_hash'] == started['request_hash'] == outcome['request_hash']
    assert claim['attempt_id'] == started['attempt_id'] == outcome['attempt_id']
    assert 'http_status' not in started and 'provider_error_code' not in started
    with pytest.raises(subject.EmbeddingBlocked):
        MemoryStore(store.state).ensure_vectors(['contenido'], client=client, folder=tmp_path/'restart')
    assert calls == [1]


@pytest.mark.parametrize('provider_code', sorted(client_module.SAFE_PROVIDER_ERROR_CODES))
def test_client_preserves_each_allowlisted_provider_code_only(provider_code):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={'error': {'code': provider_code, 'message': 'never-persist'}})
    client = client_module.EmbeddingClient(api_key='private-test', http_client=http_client(handler))
    with pytest.raises(client_module.EmbeddingRequestError) as caught:
        client.embed(['contenido'])
    assert caught.value.provider_error_code == provider_code
    assert set(client_module.safe_request_error_facts(caught.value)) == {'http_status', 'provider_error_code'}
    assert str(caught.value) == 'embedding_http_error' and calls == [1]


@pytest.mark.parametrize('provider_code', [
    'invalid_api_key\nsecret', ' insufficient_quota', 'RATE_LIMIT_EXCEEDED',
    'arbitrary-secret-code', {'secret': 'invalid_api_key'}, ['invalid_api_key'], True, 401, None,
])
def test_client_drops_arbitrary_provider_code_and_never_falls_back_to_message_or_type(provider_code):
    def handler(request):
        return httpx.Response(401, json={'error': {
            'code': provider_code, 'type': 'invalid_api_key', 'message': 'insufficient_quota',
        }})
    client = client_module.EmbeddingClient(api_key='private-test', http_client=http_client(handler))
    with pytest.raises(client_module.EmbeddingRequestError) as caught:
        client.embed(['contenido'])
    assert caught.value.provider_error_code is None and caught.value.http_status == 401
    assert vars(caught.value) == {'code': 'embedding_http_error', 'request_sent': True,
                                'http_status': 401, 'provider_error_code': None}


@pytest.mark.parametrize('status,payload,internal_code', [
    (429, b'not-json-secret-body', 'embedding_response_unreadable'),
    (503, b'<html>secret-body</html>', 'embedding_response_unreadable'),
    (400, b'["secret-body"]', 'embedding_http_error'),
    (200, b'null', 'embedding_http_error'),
])
def test_malformed_error_body_retains_only_http_status_and_uncertain_payment(tmp_path, status, payload, internal_code):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(status, content=payload)
    client = client_module.EmbeddingClient(api_key='private-test', http_client=http_client(handler))
    with pytest.raises(client_module.EmbeddingRequestError) as caught:
        MemoryStore().ensure_vectors(['contenido'], client=client, folder=tmp_path)
    assert caught.value.code == internal_code
    assert caught.value.http_status == status and caught.value.provider_error_code is None
    raw = next(tmp_path.glob('*.outcome.private.json')).read_text()
    outcome = json.loads(raw)
    assert outcome['http_status'] == status and outcome['provider_error_code'] is None
    assert outcome['status'] == 'uncertain' and outcome['request_sent'] is True
    assert outcome['cost_usd'] is None and outcome['input_tokens'] is None
    assert outcome['usage_known'] is False and 'secret-body' not in raw and calls == [1]


@pytest.mark.parametrize('status,provider_code', [
    (True, 'invalid_api_key\nsecret'), ('401-secret', {'secret': 'invalid_api_key'}),
    (600, ['invalid_api_key']), (99, 'arbitrary-secret-code'),
])
def test_terminal_resanitizes_mutated_diagnostics_from_injected_client(tmp_path, status, provider_code):
    error = client_module.EmbeddingRequestError('embedding_http_error', request_sent=True)
    error.http_status, error.provider_error_code = status, provider_code
    class FakeClient:
        model, dimensions, price_per_million = client_module.MODEL, client_module.DIMENSIONS, .02
        def embed(self, texts, *, before_request=None):
            if before_request: before_request()
            raise error
    with pytest.raises(client_module.EmbeddingRequestError):
        MemoryStore().ensure_vectors(['contenido'], client=FakeClient(), folder=tmp_path)
    raw = next(tmp_path.glob('*.outcome.private.json')).read_text()
    outcome = json.loads(raw)
    assert outcome['http_status'] is None and outcome['provider_error_code'] is None
    assert 'secret' not in raw and outcome['cost_usd'] is None


def test_legacy_exception_and_fake_client_without_new_fields_remain_compatible(tmp_path):
    error = client_module.EmbeddingRequestError('embedding_http_error', request_sent=True, http_status=403)
    del error.provider_error_code
    class FakeClient:
        model, dimensions, price_per_million = client_module.MODEL, client_module.DIMENSIONS, .02
        def embed(self, texts, *, before_request=None):
            raise error
    with pytest.raises(client_module.EmbeddingRequestError):
        MemoryStore().ensure_vectors(['contenido'], client=FakeClient(), folder=tmp_path)
    outcome = json.loads(next(tmp_path.glob('*.outcome.private.json')).read_text())
    assert outcome['http_status'] == 403 and outcome['provider_error_code'] is None
    assert outcome['cost_usd'] is None and outcome['status'] == 'uncertain'


def test_partial_known_preserves_good_vector_blocks_missing(tmp_path):
    calls=[]
    def partial(request):
        calls.append(1);data=success_data(['uno','dos']);data['data'].pop()
        return httpx.Response(200,json=data)
    client=client_module.EmbeddingClient(api_key='private-test',http_client=http_client(partial));store=MemoryStore()
    with pytest.raises(subject.EmbeddingIncomplete) as e: store.ensure_vectors(['uno','dos'],client=client,folder=tmp_path)
    assert e.value.receipt['cost_usd']>0
    good=store.ensure_vectors(['uno'],client=client,folder=tmp_path/'good')
    assert good['cached_count']==1
    with pytest.raises(subject.EmbeddingBlocked): store.ensure_vectors(['dos'],client=client,folder=tmp_path/'bad')
    assert len(calls)==1


def test_zero_pre_send_failure_can_retry_only_new_claim(tmp_path):
    store=MemoryStore();calls=[];client=counting_client(calls)
    def refuse(): raise ValueError('budget')
    with pytest.raises(client_module.EmbeddingRequestError): store.ensure_vectors(['dato'],client=client,folder=tmp_path,before_request=refuse)
    assert not calls
    assert next(iter(store.state['failed'].values()))=={'sent':False,'known':True,'cost':0}
    result=store.ensure_vectors(['dato'],client=client,folder=tmp_path)
    assert len(calls)==1 and len(store.state['claims'])==2 and result['known_cost_usd']>0


def test_unknown_usage_has_usable_vector_but_not_claimed_zero_payment(tmp_path):
    calls=[];store=MemoryStore();result=store.ensure_vectors(['dato'],client=counting_client(calls,known=False),folder=tmp_path)
    assert result['usage_unknown_attempts']==1
    outcome=json.loads(next(tmp_path.glob('*.outcome.private.json')).read_text())
    assert outcome['status']=='uncertain' and outcome['cost_usd'] is None
    store.ensure_vectors(['dato'],client=counting_client(calls),folder=tmp_path/'different')
    assert len(calls)==1


def test_store_batching64_without_reordering(tmp_path):
    calls=[];texts=[f'requisito particular {i}' for i in range(129)]
    result=MemoryStore().ensure_vectors(texts,client=counting_client(calls),folder=tmp_path)
    assert [len(batch) for batch in calls]==[64,64,1]
    assert [text for batch in calls for text in batch]==texts
    assert len(result['vectors'])==129 and len(result['attempt_ids'])==3


def test_idle_guard_and_corpus_invalid_hash_before_sql():
    conn=SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.INTRANS))
    with pytest.raises(subject.EmbeddingPersistenceError,match='IDLE'): subject.save_corpus(conn,{},convocation_id=1)
    with pytest.raises(subject.EmbeddingPersistenceError): subject._corpus_chunks({'corpus_hash':'a'*64,'chunks':[{'text':'exacto','text_sha256':'b'*64,'chunk_id':'c'*64}]})


def test_migration_no_extensions_binary_r2_and_identical_draft():
    from pathlib import Path
    root=Path(__file__).resolve().parents[3]
    migration=(root/'db/migrations/0043_prod2_embeddings.sql').read_text()
    assert migration==(root/'db/drafts/0043_prod2_embeddings.sql').read_text()
    assert 'PRIMARY KEY (text_sha256,model,dimensions)' in migration
    assert 'partial_known' in migration and 'failed_pre_send' in migration
    assert 'pgvector' in migration and 'CREATE EXTENSION' not in migration
    assert 'GENERATED ALWAYS AS IDENTITY' in migration


@pytest.mark.parametrize('mutation',['missing','text','vector','hash','dimensions','model'])
def test_done_vector_bound_to_immutable_paid_receipt(mutation):
    text='cláusula exacta';key=subject._text_sha(text)
    row={'text_sha256':key,'exact_text':text,'model':'text-embedding-3-small','dimensions':1536,
         'vector':vector(),'response_sha256':'b'*64,
         'attempt_input':{'texts':[text],'text_sha256':[key],'model':'text-embedding-3-small','dimensions':1536},
         'attempt_response':{'vectors_by_index':{'0':vector()},'model':'text-embedding-3-small','dimensions':1536,'response_sha256':'b'*64}}
    subject._verify_vector_receipt(row)
    if mutation=='missing': row['attempt_response']=None
    if mutation=='text': row['exact_text']='different'
    if mutation=='vector': row['vector'][1]=2.
    if mutation=='hash': row['response_sha256']='a'*64
    if mutation=='dimensions': row['attempt_input']['dimensions']=512
    if mutation=='model': row['attempt_response']['model']='different'
    with pytest.raises(subject.EmbeddingPersistenceError): subject._verify_vector_receipt(row)
