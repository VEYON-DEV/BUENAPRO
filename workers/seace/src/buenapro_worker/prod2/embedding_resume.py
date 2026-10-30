"""Explicit, one-shot queue fork when a frozen PROD2 reading policy changes.

Call only under the shared coordinator lock with the original main stopped.
Source Redis keys, outcomes and paid receipts remain unchanged. Every handled
identity, including rejections and uncertain/deferred reads, stays DONE.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from redis.exceptions import WatchError

from .batch_runner import private_write, read_private
from .staging import RedisStage

VERSION = "prod2_embedding_resume_fork_v1"


class EmbeddingResumeError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _file(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():
        raise EmbeddingResumeError("Resume source proof file missing or symlinked")
    raw=path.read_bytes()
    try:
        value=json.loads(raw)
    except (ValueError,UnicodeDecodeError):
        raise EmbeddingResumeError("Resume source JSON invalid") from None
    return value,sha256(raw).hexdigest()


def _id(value):
    if type(value) is int and value>0:
        return str(value)
    if isinstance(value,str) and re.fullmatch(r'[1-9][0-9]{0,18}',value):
        return value
    raise EmbeddingResumeError("Resume convocation identity invalid")


def _old_queue(manifest):
    if not manifest.get('keyword_tracking'):
        return manifest['run_id']
    payload={'sweep_id':manifest['keyword_tracking']['sweep_id'],
        'commercial_profiles':manifest['commercial_profiles'],'gemini_model':manifest['gemini_model'],
        'pipeline':manifest['pipeline'],'document_scope':manifest['document_scope'],
        'jev_threshold':manifest['jev_threshold'],'selector_high':manifest['selector_high'],
        'selector_low':manifest['selector_low']}
    if 'llm1_luna_fallback' in manifest:
        payload['llm1_luna_fallback']=manifest['llm1_luna_fallback']
    if 'llm1_min_affinity' in manifest:
        payload['llm1_min_affinity']=manifest['llm1_min_affinity']
    # This is the original queue formula. A new input policy intentionally has
    # a different target identity supplied by the runner, never guessed here.
    return 'kw-'+_digest(payload)


def _latest_report(folder):
    reports=[]
    for path in folder.glob('report-*.private.json'):
        match=re.fullmatch(r'report-([0-9]+)\.private\.json',path.name)
        if match:
            reports.append((int(match.group(1)),path))
    if not reports:
        raise EmbeddingResumeError("Resume latest source report missing")
    return max(reports)[1]


def load_resume_binding(folder)->dict:
    folder=Path(folder).absolute()
    if folder.is_symlink() or not folder.is_dir():
        raise EmbeddingResumeError("Resume source directory missing or symlinked")
    manifest,manifest_sha=_file(folder/'manifest.private.json')
    report_path=_latest_report(folder)
    report,report_sha=_file(report_path)
    if report.get('run_id')!=manifest.get('run_id') or report.get('source_coverage_complete') is not True:
        raise EmbeddingResumeError("Resume source query coverage incomplete")
    coverage=report.get('coverage')
    if (not isinstance(coverage,list) or not coverage or report.get('queries_total')!=len(coverage)
            or report.get('queries_complete')!=len(coverage)
            or any(row.get('coverage_complete') is not True or row.get('status')!='complete' for row in coverage)):
        raise EmbeddingResumeError("Resume source query proofs incomplete")
    # Verified empty scopes omit query_hash but retain their exact query and
    # scope proof; derive a local identity without changing either source.
    if any(not isinstance(row.get('query'),dict) for row in coverage):
        raise EmbeddingResumeError('Resume exact query objects missing')
    keywords=set(manifest.get('keywords',[]));objects=set(manifest.get('objects',[]))
    years={int(year) for year in manifest.get('resolved_years',[])}
    if not keywords or not objects or not years:
        raise EmbeddingResumeError('Resume frozen query scope missing')
    for row in coverage:
        query=row['query']
        if (query.get('keyword') not in keywords or query.get('object_type') not in objects
                or type(query.get('year')) is not int or query['year'] not in years):
            raise EmbeddingResumeError('Resume query outside frozen keyword/object/year scope')
    query_hashes=[_digest(row['query']) for row in coverage]
    if len(set(query_hashes))!=len(query_hashes) or any(row.get('query_hash') is not None and
            (not isinstance(row['query_hash'],str) or not re.fullmatch('[0-9a-f]{64}',row['query_hash'])) for row in coverage):
        raise EmbeddingResumeError("Resume source query identities invalid")
    scope_proofs=report.get('scope_proofs',[])
    if not isinstance(scope_proofs,list):
        raise EmbeddingResumeError('Resume scope proofs invalid')
    scopes={proof.get('proof_hash'):proof for proof in scope_proofs if isinstance(proof,dict)}
    for row in coverage:
        if row.get('coverage_kind')=='verified_empty_scope_observed':
            proof=scopes.get(row.get('scope_proof_hash'))
            if (not proof or proof.get('scope_zero_verified') is not True
                    or proof.get('sessions_independent') is not True or proof.get('total_results')!=0
                    or proof.get('errors')):
                raise EmbeddingResumeError('Resume empty query scope proof missing or unverified')
    records=report.get('records')
    if not isinstance(records,list) or report.get('processed')!=len(records):
        raise EmbeddingResumeError("Resume processed outcomes differ from report")
    refs,handled={},set()
    for outcome in records:
        identity=_id(outcome.get('convocation_id'))
        path=folder/identity/'outcome.private.json'
        actual,digest=_file(path)
        if actual!=outcome or identity in handled:
            raise EmbeddingResumeError("Resume outcome changed or duplicated")
        handled.add(identity)
        refs[str(path)]=digest
    carry_path=folder/'done-carryover-verified.private.json'
    carry_ids=set()
    if carry_path.exists():
        carry,carry_sha=_file(carry_path)
        refs[str(carry_path)]=carry_sha
        if (carry.get('target_queue')!=_old_queue(manifest) or carry.get('old_queue_unchanged') is not True
                or carry.get('source_calls')!=0 or carry.get('provider_calls')!=0):
            raise EmbeddingResumeError("Resume historical carryover contract invalid")
        rows=carry.get('source_outcomes')
        if not isinstance(rows,list):
            raise EmbeddingResumeError("Resume historical source outcomes missing")
        for item in rows:
            identity=_id(item.get('convocation_id'))
            actual,digest=_file(item.get('outcome_path',''))
            if (digest!=item.get('outcome_sha256') or _id(actual.get('convocation_id'))!=identity
                    or actual.get('status')!=item.get('status') or actual.get('accepted')!=item.get('accepted')
                    or identity in carry_ids or identity in handled):
                raise EmbeddingResumeError("Resume historical outcome SHA/identity changed")
            carry_ids.add(identity)
            refs[str(Path(item['outcome_path']).absolute())]=digest
        if carry_ids!={_id(i) for i in carry.get('old_done_ids',[])} or carry.get('redis_done_ids_added')!=len(carry_ids):
            raise EmbeddingResumeError("Resume carryover ID count differs")
        handled|=carry_ids
    redis=report.get('redis')
    if (not isinstance(redis,dict) or any(type(redis.get(k)) is not int or redis[k]<0 for k in ('queued','pending','payloads','seen'))
            or redis['pending']!=0 or redis['payloads']!=redis['queued']
            or redis['seen']!=len(handled)+redis['queued']):
        raise EmbeddingResumeError("Resume latest source report queue inconsistent")
    binding={'version':VERSION,'source_folder':str(folder),'manifest_sha256':manifest_sha,
             'report_path':str(report_path),'report_sha256':report_sha,'source_queue':_old_queue(manifest),
             'handled_ids':sorted(handled,key=int),'handled_count':len(handled),'processed_count':len(records),
             'carryover_count':len(carry_ids),'redis':redis,'coverage':coverage,
             'scope_proofs':report.get('scope_proofs',[]),'source_manifest':manifest,
             'outcome_refs':[{'path':path,'sha256':digest} for path,digest in sorted(refs.items())]}
    binding['binding_hash']=_digest(binding)
    return binding


def _binding_unchanged(binding):
    current=load_resume_binding(binding['source_folder'])
    if current!=binding:
        raise EmbeddingResumeError("Resume source manifest/report/outcomes changed")


def _same_scope(source,new):
    fields=('commercial_profiles','gemini_model','profile_id','pipeline','inventory_hash',
            'keywords','configured_keywords','lanes','years','resolved_years','objects','from_date','to_date',
            'published_from','published_to','document_scope','jev_threshold','llm1_min_affinity','selector_high',
            'selector_low','selector_policy_version')
    if any(source.get(key)!=new.get(key) for key in fields):
        raise EmbeddingResumeError("Embedding fork changed frozen source or commercial scope")
    if source.get('keyword_tracking',{}).get('sweep_id')!=new.get('keyword_tracking',{}).get('sweep_id'):
        raise EmbeddingResumeError("Embedding fork changed keyword sweep identity")


def _decode(value):
    if isinstance(value,bytes):
        return value.decode('utf-8')
    return value


def _snapshot(pipe,keys):
    kinds=[_decode(pipe.type(key)) for key in keys]
    if any(kind not in ('none','hash' if index<2 else 'list') for index,kind in enumerate(kinds)):
        raise EmbeddingResumeError("Resume Redis key type incompatible")
    dedup={_decode(k):_decode(v) for k,v in pipe.hgetall(keys[0]).items()}
    payloads={_decode(k):_decode(v) for k,v in pipe.hgetall(keys[1]).items()}
    queue=[_decode(x) for x in pipe.lrange(keys[2],0,-1)]
    pending=[_decode(x) for x in pipe.lrange(keys[3],0,-1)]
    return {'dedup':dedup,'payloads':payloads,'queue':queue,'pending':pending}


def _validate_source(snapshot,binding):
    dedup,payloads,queue,pending=(snapshot[k] for k in ('dedup','payloads','queue','pending'))
    if pending or len(queue)!=len(set(queue)) or any(value not in ('done','queued') for value in dedup.values()):
        raise EmbeddingResumeError("Resume original queue has pending or invalid states")
    done={key for key,value in dedup.items() if value=='done'}
    queued={key for key,value in dedup.items() if value=='queued'}
    if (done!=set(binding['handled_ids']) or queued!=set(queue) or set(payloads)!=queued
            or len(dedup)!=binding['redis']['seen'] or len(queue)!=binding['redis']['queued']):
        raise EmbeddingResumeError("Resume original queue differs from frozen source report")
    for identity,encoded in payloads.items():
        _id(identity)
        try:
            parsed=json.loads(encoded,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        except (ValueError,TypeError):
            raise EmbeddingResumeError("Resume staged payload invalid") from None
        if not isinstance(parsed,dict):
            raise EmbeddingResumeError("Resume staged payload must be an object")
        row=parsed.get('row')
        if not isinstance(row,dict) or _id(row.get('convocation_id'))!=identity:
            raise EmbeddingResumeError('Resume staged payload row.convocation_id differs')
    return _digest(snapshot)


def _empty(snapshot):
    return not any(snapshot.values())


def seed_resume(client,target_stage,binding,new_manifest,folder)->dict:
    """Atomic fork. A lost EXEC answer is retained for manual reconciliation."""
    _binding_unchanged(binding)
    _same_scope(binding['source_manifest'],new_manifest)
    if not isinstance(target_stage,RedisStage) or target_stage.client is not client:
        raise EmbeddingResumeError("Resume target must use the supplied Redis client")
    source=RedisStage(client,binding['source_queue'])
    if source.keys==target_stage.keys:
        raise EmbeddingResumeError("Embedding fork requires a new queue identity")
    folder=Path(folder).absolute();folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    marker_key=target_stage.namespace+':embedding-resume'
    proof_path=folder/'embedding-resume-proof.private.json'
    intent_path=folder/'embedding-resume-intent.private.json'
    started_path=folder/'embedding-resume-exec-started.private.json'
    target_manifest_hash=_digest(new_manifest)
    with client.pipeline() as pipe:
        try:
            pipe.watch(*source.keys,*target_stage.keys,marker_key)
            source_snapshot=_snapshot(pipe,source.keys)
            source_sha=_validate_source(source_snapshot,binding)
            marker=_decode(pipe.get(marker_key))
            target_snapshot=_snapshot(pipe,target_stage.keys)
            immutable={'version':VERSION,'binding_hash':binding['binding_hash'],
                       'source_queue':binding['source_queue'],'target_queue':target_stage.namespace.removeprefix('buenapro:prod2:accepted:'),
                       'source_snapshot_sha256':source_sha,'new_manifest_hash':target_manifest_hash,
                       'source_manifest_sha256':binding['manifest_sha256'],'source_report_sha256':binding['report_sha256'],
                       'handled_count':binding['handled_count'],'queued_count':len(source_snapshot['queue']),
                       'seen_count':len(source_snapshot['dedup'])}
            if marker is not None:
                if marker!=_json(immutable) or not proof_path.exists():
                    raise EmbeddingResumeError("Existing embedding fork requires its exact committed proof")
                proof,_=_file(proof_path)
                if any(proof.get(key)!=value for key,value in immutable.items()) or proof.get('redis_committed') is not True:
                    raise EmbeddingResumeError("Embedding fork proof differs from existing marker")
                queued_ids=set(target_snapshot['queue']);pending_ids=set(target_snapshot['pending'])
                if (len(queued_ids)!=len(target_snapshot['queue']) or len(pending_ids)!=len(target_snapshot['pending'])
                        or queued_ids&pending_ids
                        or set(target_snapshot['payloads'])!=queued_ids|pending_ids
                        or any(state not in ('done','queued','pending') for state in target_snapshot['dedup'].values())
                        or {key for key,state in target_snapshot['dedup'].items() if state=='queued'}!=queued_ids
                        or {key for key,state in target_snapshot['dedup'].items() if state=='pending'}!=pending_ids):
                    raise EmbeddingResumeError('Embedding fork target progress inconsistent')
                # Processing may advance, but handled identities must never
                # disappear or regain a payload/queued state.
                if (set(target_snapshot['dedup'])!=set(source_snapshot['dedup'])
                        or any(target_snapshot['dedup'].get(identity)!='done' or identity in target_snapshot['payloads']
                               or identity in target_snapshot['queue'] or identity in target_snapshot['pending']
                               for identity in binding['handled_ids'])):
                    raise EmbeddingResumeError("Embedding fork progress damaged handled identities")
                return {**proof,'reused':True,'target_progress_reset':False}
            if not _empty(target_snapshot):
                raise EmbeddingResumeError("Embedding fork target already nonempty without marker")
            if intent_path.exists() or started_path.exists() or proof_path.exists():
                raise EmbeddingResumeError("Embedding fork previous attempt requires manual reconciliation")
            _binding_unchanged(binding)
            private_write(intent_path,{**immutable,'created_at':datetime.now(timezone.utc).isoformat(),
                'source_keys_preserved':True,'provider_calls':0,'source_calls':0,'business_database_writes':0})
            pipe.multi()
            if source_snapshot['dedup']:
                pipe.hset(target_stage.keys[0],mapping=source_snapshot['dedup'])
            if source_snapshot['payloads']:
                pipe.hset(target_stage.keys[1],mapping=source_snapshot['payloads'])
            if source_snapshot['queue']:
                pipe.rpush(target_stage.keys[2],*source_snapshot['queue'])
            pipe.set(marker_key,_json(immutable),nx=True)
            for key in (*target_stage.keys,marker_key):
                pipe.persist(key)
            private_write(started_path,{'version':VERSION,'binding_hash':binding['binding_hash'],
                                      'dispatch_started_at':datetime.now(timezone.utc).isoformat()})
            results=pipe.execute()
            # No caller retries this transaction if the response was lost.
            marker_index=int(bool(source_snapshot['dedup']))+int(bool(source_snapshot['payloads']))+int(bool(source_snapshot['queue']))
            if results[marker_index] is not True:
                raise EmbeddingResumeError("Embedding fork marker commit not confirmed")
        except WatchError:
            raise EmbeddingResumeError("Embedding fork source/target changed before commit; no automatic retry") from None
        except EmbeddingResumeError:
            raise
        except Exception:
            raise EmbeddingResumeError("Embedding fork outcome uncertain; preserve inputs and do not automatically repeat") from None
    # Confirm actual committed keys atomically. Never infer success from an
    # exception, an empty target, or a local file written before EXEC.
    with client.pipeline(transaction=True) as check:
        for stage in (source,target_stage):
            check.hgetall(stage.keys[0]);check.hgetall(stage.keys[1]);check.lrange(stage.keys[2],0,-1);check.lrange(stage.keys[3],0,-1)
        check.get(marker_key)
        values=check.execute()
    def parsed(values):
        return {'dedup':{_decode(k):_decode(v) for k,v in values[0].items()},
                'payloads':{_decode(k):_decode(v) for k,v in values[1].items()},
                'queue':[_decode(v) for v in values[2]],'pending':[_decode(v) for v in values[3]]}
    if parsed(values[:4])!=source_snapshot or parsed(values[4:8])!=source_snapshot or _decode(values[8])!=_json(immutable):
        raise EmbeddingResumeError("Embedding fork committed readback mismatch; do not automatically repeat")
    _binding_unchanged(binding)
    proof={**immutable,'redis_committed':True,'source_keys_preserved':True,'target_payloads_exact':True,
           'handled_done_preserved':True,'target_progress_reset':False,'provider_calls':0,'source_calls':0,
           'business_database_writes':0,'verified_at':datetime.now(timezone.utc).isoformat()}
    private_write(proof_path,proof)
    return {**proof,'reused':False}
