"""Queue migration must preserve paid/declined/uncertain identities exactly."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest
from redis.exceptions import WatchError

from buenapro_worker.prod2 import embedding_resume as subject
from buenapro_worker.prod2.staging import RedisStage


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True))
    return sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path):
    folder=tmp_path/'source';legacy=tmp_path/'legacy';folder.mkdir()
    manifest={'run_id':'old','keyword_tracking':{'sweep_id':'initial'},'commercial_profiles':[{'id':'company'}],
              'gemini_model':'gemini-2.5-flash-lite','pipeline':'prod2','document_scope':'selected','jev_threshold':60,
              'selector_high':80,'selector_low':20,'keywords':['software'],'configured_keywords':['software'],
              'objects':['good','service'],'years':['2026'],'resolved_years':[2026],
              'published_from':'2026-04-06','published_to':'2026-10-06'}
    write(folder/'manifest.private.json',manifest)
    records=[{'convocation_id':1,'accepted':False,'status':'rejected_below_60'},
             {'convocation_id':2,'accepted':True,'status':'needs_review','provider_usage_known':False}]
    for row in records: write(folder/str(row['convocation_id'])/'outcome.private.json',row)
    carry=[]
    for identity,status in ((3,'needs_review'),(4,'llm1_complete')):
        row={'convocation_id':identity,'accepted':True,'status':status};path=legacy/str(identity)/'outcome.private.json'
        digest=write(path,row)
        carry.append({**row,'outcome_path':str(path),'outcome_sha256':digest})
    carryvalue={'target_queue':subject._old_queue(manifest),'old_queue_unchanged':True,'source_calls':0,'provider_calls':0,
                'source_outcomes':carry,'old_done_ids':[3,4],'redis_done_ids_added':2}
    write(folder/'done-carryover-verified.private.json',carryvalue)
    coverage=[{'query':{'keyword':'software','year':2026,'object_type':'good'},'status':'complete','coverage_complete':True}]
    report={'run_id':'old','source_coverage_complete':True,'queries_total':1,'queries_complete':1,
            'coverage':coverage,'records':records,'processed':2,'scope_proofs':[],
            'redis':{'queued':2,'payloads':2,'pending':0,'seen':6}}
    write(folder/'report-10.private.json',report)
    newmanifest={**manifest,'run_id':'new','llm1_input_policy':'embeddings','embedding_config_hash':'a'*64}
    return folder,manifest,report,newmanifest


class FakeRedis:
    def __init__(self):
        self.data={};self.versions={};self.writes=[];self.before_exec=None;self.lose_response=False
    def pipeline(self,transaction=True): return FakePipe(self,transaction)
    def mutate(self,key,value): self.data[key]=deepcopy(value);self.versions[key]=self.versions.get(key,0)+1
    def command(self,name,*args,**kwargs):
        key=args[0]
        if name=='type':
            val=self.data.get(key)
            return 'none' if val is None else 'hash' if isinstance(val,dict) else 'list' if isinstance(val,list) else 'string'
        if name=='hgetall': return deepcopy(self.data.get(key,{}))
        if name=='lrange': return deepcopy(self.data.get(key,[]))
        if name=='get': return self.data.get(key)
        self.writes.append((name,key))
        if name=='hset': self.mutate(key,{**self.data.get(key,{}),**kwargs['mapping']});return len(kwargs['mapping'])
        if name=='rpush': self.mutate(key,self.data.get(key,[])+list(args[1:]));return len(self.data[key])
        if name=='set':
            if kwargs.get('nx') and key in self.data:return None
            self.mutate(key,args[1]);return True
        if name=='persist': return key in self.data
        raise AssertionError(name)


class FakePipe:
    def __init__(self,client,transaction):self.client=client;self.queue=[];self.watched={};self.queuing=transaction
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def watch(self,*keys):self.watched={key:self.client.versions.get(key,0) for key in keys};self.queuing=False
    def multi(self):self.queuing=True
    def __getattr__(self,name):
        def command(*args,**kwargs):
            if self.queuing:self.queue.append((name,args,kwargs));return self
            return self.client.command(name,*args,**kwargs)
        return command
    def execute(self):
        if self.watched and self.client.before_exec:
            callback=self.client.before_exec;self.client.before_exec=None;callback()
        if any(self.client.versions.get(key,0)!=version for key,version in self.watched.items()):raise WatchError()
        result=[self.client.command(name,*args,**kwargs) for name,args,kwargs in self.queue]
        self.queue=[]
        if self.watched and self.client.lose_response:
            self.client.lose_response=False;raise TimeoutError('synthetic lost EXEC')
        return result


def setup_queue(binding):
    client=FakeRedis();stage=RedisStage(client,binding['source_queue'])
    client.mutate(stage.keys[0],{'1':'done','2':'done','3':'done','4':'done','5':'queued','6':'queued'})
    client.mutate(stage.keys[1],{'5':'{"row":{"convocation_id":5,"text":"exact"}}','6':'{"row":{"convocation_id":6}}'})
    client.mutate(stage.keys[2],['5','6'])
    return client,stage,RedisStage(client,'new-embedding-queue')


def test_load_binding_old_paid_rejected_uncertain_and_carryover(source):
    folder,_,_,_=source;binding=subject.load_resume_binding(folder)
    assert binding['handled_ids']==['1','2','3','4'] and binding['handled_count']==4
    assert binding['processed_count']==2 and binding['carryover_count']==2
    assert len(binding['outcome_refs'])==5


def test_seed_atomic_exact_source_unchanged_and_reuse_progress(source,tmp_path):
    folder,_,_,new=source;binding=subject.load_resume_binding(folder);client,source_stage,target=setup_queue(binding)
    before=deepcopy(client.data)
    proof=subject.seed_resume(client,target,binding,new,tmp_path/'new')
    assert proof['redis_committed'] and proof['handled_count']==4 and proof['queued_count']==2
    for key in source_stage.keys:assert client.data.get(key)==before.get(key)
    assert client.data[target.keys[0]]==before[source_stage.keys[0]]
    assert client.data[target.keys[1]]==before[source_stage.keys[1]]
    assert client.data[target.keys[2]]==['5','6']
    assert all(key in set(target.keys)|{target.namespace+':embedding-resume'} for _,key in client.writes)
    client.mutate(target.keys[0],{**client.data[target.keys[0]],'5':'done'})
    client.mutate(target.keys[1],{'6':client.data[target.keys[1]]['6']})
    client.mutate(target.keys[2],['6']);count=len(client.writes)
    reused=subject.seed_resume(client,target,binding,new,tmp_path/'new')
    assert reused['reused'] and reused['target_progress_reset'] is False and len(client.writes)==count


@pytest.mark.parametrize('case',['scope','target_nonempty','source_pending','source_done_missing','payload_mismatch','report_changed','outcome_changed','carry_sha','payload_id','query_scope'])
def test_invalid_resume_zero_redis_writes(source,tmp_path,case):
    folder,_,report,new=source;binding=subject.load_resume_binding(folder);client,stage,target=setup_queue(binding)
    if case=='scope':new['commercial_profiles']=[{'id':'different'}]
    if case=='target_nonempty':client.mutate(target.keys[0],{'9':'queued'})
    if case=='source_pending':client.mutate(stage.keys[3],['5'])
    if case=='source_done_missing':client.mutate(stage.keys[0],{k:v for k,v in client.data[stage.keys[0]].items() if k!='2'})
    if case=='payload_mismatch':client.mutate(stage.keys[1],{'5':'{}'})
    if case=='report_changed':write(folder/'report-11.private.json',report)
    if case=='outcome_changed':write(folder/'2/outcome.private.json',{'convocation_id':2,'status':'altered'})
    if case=='carry_sha':write(tmp_path/'legacy/3/outcome.private.json',{'convocation_id':3,'accepted':True,'status':'different'})
    if case=='payload_id':client.mutate(stage.keys[1],{'5':'{"row":{"convocation_id":999}}','6':'{"row":{"convocation_id":6}}'})
    if case=='query_scope':
        report['coverage'][0]['query']['keyword']='other';write(folder/'report-10.private.json',report)
    with pytest.raises(subject.EmbeddingResumeError):subject.seed_resume(client,target,binding,new,tmp_path/'new')
    assert not client.writes


def test_source_race_abort_retains_intent_blocks_repeat(source,tmp_path):
    folder,_,_,new=source;binding=subject.load_resume_binding(folder);client,stage,target=setup_queue(binding)
    client.before_exec=lambda:client.mutate(stage.keys[0],{**client.data[stage.keys[0]],'9':'queued'})
    with pytest.raises(subject.EmbeddingResumeError,match='changed before commit'):
        subject.seed_resume(client,target,binding,new,tmp_path/'new')
    assert not client.writes and (tmp_path/'new/embedding-resume-intent.private.json').exists()
    client.mutate(stage.keys[0],{'1':'done','2':'done','3':'done','4':'done','5':'queued','6':'queued'})
    with pytest.raises(subject.EmbeddingResumeError,match='previous attempt'):
        subject.seed_resume(client,target,binding,new,tmp_path/'new')


def test_lost_exec_reply_marker_without_proof_never_repeats(source,tmp_path):
    folder,_,_,new=source;binding=subject.load_resume_binding(folder);client,stage,target=setup_queue(binding)
    client.lose_response=True
    with pytest.raises(subject.EmbeddingResumeError,match='uncertain'):
        subject.seed_resume(client,target,binding,new,tmp_path/'new')
    assert client.data[target.keys[0]]==client.data[stage.keys[0]]
    count=len(client.writes)
    with pytest.raises(subject.EmbeddingResumeError,match='exact committed proof'):
        subject.seed_resume(client,target,binding,new,tmp_path/'new')
    assert len(client.writes)==count


def test_scope_zero_requires_actual_proof(source):
    folder,_,report,_=source
    report['coverage'][0]['coverage_kind']='verified_empty_scope_observed';report['coverage'][0]['scope_proof_hash']='b'*64
    write(folder/'report-10.private.json',report)
    with pytest.raises(subject.EmbeddingResumeError,match='scope proof'):
        subject.load_resume_binding(folder)
    report['scope_proofs']=[{'proof_hash':'b'*64,'scope_zero_verified':True,'sessions_independent':True,'total_results':0,'errors':[]}]
    write(folder/'report-10.private.json',report)
    assert subject.load_resume_binding(folder)['handled_count']==4


def test_preserved_handled_cannot_reappear_after_fork(source,tmp_path):
    folder,_,_,new=source;binding=subject.load_resume_binding(folder);client,stage,target=setup_queue(binding)
    subject.seed_resume(client,target,binding,new,tmp_path/'new')
    client.mutate(target.keys[0],{**client.data[target.keys[0]],'2':'queued'})
    with pytest.raises(subject.EmbeddingResumeError,match='handled identities|progress inconsistent'):
        subject.seed_resume(client,target,binding,new,tmp_path/'new')


@pytest.mark.skipif(__import__('os').environ.get('BUENAPRO_REDIS_SCRATCH_TEST')!='1',reason='Explicit isolated Redis scratch opt-in required')
def test_real_redis_fork_keeps_source_exact_and_reuses_progress(source,tmp_path):
    from uuid import uuid4
    import psycopg
    import redis
    from psycopg.rows import dict_row
    from buenapro_worker.prod2.runtime import production_staging_settings
    folder,manifest,report,new=source
    manifest['commercial_profiles']=[{'synthetic_scratch':uuid4().hex}]
    write(folder/'manifest.private.json',manifest)
    carry_path=folder/'done-carryover-verified.private.json';carry=json.loads(carry_path.read_text())
    carry['target_queue']=subject._old_queue(manifest);write(carry_path,carry)
    new={**new,'commercial_profiles':manifest['commercial_profiles']}
    binding=subject.load_resume_binding(folder)
    root=Path(__file__).resolve().parents[3]
    with production_staging_settings(ssh_host='ubuntu@168.129.177.199',ssh_key=root/'credentials/ssh-key-2026-07-14.key') as (settings,redis_url):
        with psycopg.connect(settings.database_url,autocommit=True,row_factory=dict_row) as conn:
            locked=conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()['locked']
            assert locked,'Another PROD2 coordinator owns the executor'
            client=redis.Redis.from_url(redis_url,decode_responses=True)
            old=RedisStage(client,binding['source_queue']);target=RedisStage(client,'embedding-scratch-'+uuid4().hex)
            marker=target.namespace+':embedding-resume';keys=(*old.keys,*target.keys,marker)
            try:
                assert not client.exists(*keys),'Scratch keys must be absent'
                client.hset(old.keys[0],mapping={'1':'done','2':'done','3':'done','4':'done','5':'queued','6':'queued'})
                client.hset(old.keys[1],mapping={'5':'{"row":{"convocation_id":5,"text":"exact"}}','6':'{"row":{"convocation_id":6}}'})
                client.rpush(old.keys[2],'5','6')
                before={key:client.dump(key) for key in old.keys}
                proof=subject.seed_resume(client,target,binding,new,tmp_path/'real')
                assert proof['redis_committed'] and proof['handled_count']==4
                assert all(client.dump(key)==before[key] for key in old.keys)
                assert all(client.pttl(key)==-1 for key in (*target.keys[:3],marker))
                reserved=target.next();assert reserved[0]=='5'
                reused=subject.seed_resume(client,target,binding,new,tmp_path/'real')
                assert reused['reused'] and target.stats()['pending']==1
                target.ack('5')
                assert subject.seed_resume(client,target,binding,new,tmp_path/'real')['reused']
                assert all(client.dump(key)==before[key] for key in old.keys)
            finally:
                client.delete(*keys)
                assert client.exists(*keys)==0
                client.close()
                conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")


def test_source_file_mutates_during_exec_no_committed_proof(source,tmp_path):
    folder,_,report,new=source;binding=subject.load_resume_binding(folder);client,stage,target=setup_queue(binding)
    client.before_exec=lambda:write(folder/'report-11.private.json',report)
    with pytest.raises(subject.EmbeddingResumeError,match='changed'):
        subject.seed_resume(client,target,binding,new,tmp_path/'new')
    assert client.data.get(target.namespace+':embedding-resume')
    assert not (tmp_path/'new/embedding-resume-proof.private.json').exists()
