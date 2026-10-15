"""Complete the same frozen DIN batch with shared recommendation context.

Gemma's observed account quota is 16k input tokens/minute. Count tokens before
inference, pack up to five opportunities, and pace independent requests. A saved
HTTP rejection is never retried. Original JEV responses and quota receipts stay.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers/seace/src'))
sys.path.insert(0,str(ROOT/'scripts'))
import psycopg
from psycopg.rows import dict_row
import requests
from dotenv import dotenv_values

import run_dinaut_three_dot_matches as run
from buenapro_worker.matching.jev_recommendations import (
    build_small_recommendation_context,build_recommendation_batch,
    RECOMMENDATION_BATCH_PROMPT,validate_recommendation_batch,
)

FOLDER=ROOT/'tmp/dinaut-three-dot-20261015'
INPUT_BUDGET=14500


def key(item):
    return f"{item['case']['source_key']}-{item['case']['source_id']}"


def batch_request(contexts):
    result={'systemInstruction':{'parts':[{'text':RECOMMENDATION_BATCH_PROMPT}]},
        'contents':[{'role':'user','parts':[{'text':run.canonical(build_recommendation_batch(contexts))}]}],
        'generationConfig':{'temperature':0,'maxOutputTokens':16384,
                            'thinkingConfig':{'thinkingLevel':'minimal'}}}
    if run.GEMMA_MODEL=='gemini-3.1-flash-lite':
        result['generationConfig']['responseMimeType']='application/json'
    return result


def count_input(settings,request,folder):
    path=folder/'token-count.private.json'
    digest=run.fingerprint(request)
    if path.exists():
        saved=json.loads(path.read_text())
        if saved['request_sha256']!=digest:raise run.PublicationConflict('Count belongs to another Gemma request')
        return saved['total_tokens']
    result=requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{run.GEMMA_MODEL}:countTokens',
        headers={'x-goog-api-key':settings.gemini_api_key},
        json={'generateContentRequest':{'model':'models/'+run.GEMMA_MODEL,**request}},
        timeout=(15,45),allow_redirects=False)
    if result.status_code!=200:raise run.ProviderBlocked(f'Free token count HTTP {result.status_code}; no generation attempted')
    tokens=result.json().get('totalTokens')
    if type(tokens)is not int or tokens<1:raise run.ProviderBlocked('Invalid free Gemma token count')
    run.private_json(path,{'request_sha256':digest,'total_tokens':tokens,'metadata_only':True,
        'inference_calls':0,'http_status':200,'cutoff_utc':datetime.now(timezone.utc).isoformat()},exclusive=True)
    return tokens


def paced_wait(folder,next_tokens):
    if run.GEMMA_MODEL!='gemma-4-31b-it':return
    while True:
        now=datetime.now(timezone.utc).timestamp();recent=[]
        for p in (folder/'gemma-batches').glob('*/gemma-started.private.json'):
            response=p.with_name('gemma-response.private.json')
            if response.exists() and json.loads(response.read_text()).get('http_status')!=200:
                continue
            started=datetime.fromisoformat(json.loads(p.read_text())['started_utc']).timestamp()
            if now-started<62:
                count=json.loads(p.with_name('token-count.private.json').read_text())['total_tokens']
                recent.append((started,count))
        if sum(n for _,n in recent)+next_tokens<=INPUT_BUDGET:return
        wait=max(0,min(t for t,_ in recent)+62-now)
        run.emit({'status':'waiting_for_observed_gemma_token_quota','seconds':round(wait,1)})
        deadline=time.monotonic()+wait
        while time.monotonic()<deadline:time.sleep(min(5,max(0,deadline-time.monotonic())))


def recommendations_from_receipt(receipt,payloads):
    candidates=receipt['body'].get('candidates') or []
    if len(candidates)!=1 or candidates[0].get('finishReason')!='STOP':
        raise run.ProviderBlocked('Incomplete Gemma batch response; no retry')
    text=''.join(p.get('text','') for p in candidates[0]['content']['parts'] if not p.get('thought'))
    result=validate_recommendation_batch(run.parse_json_response(text),payloads)
    usage=receipt['body'].get('usageMetadata') or {}
    if any(type(usage.get(k))is not int or usage[k]<0 for k in ('promptTokenCount','candidatesTokenCount')):
        raise run.ProviderBlocked('Gemma batch usage missing; preserve response')
    return result,usage


def bind_saved_batch(batch_folder,items):
    plan=json.loads((batch_folder/'plan.private.json').read_text())
    request=json.loads((batch_folder/'gemma-request.private.json').read_text())
    receipt=json.loads((batch_folder/'gemma-response.private.json').read_text())
    if receipt['http_status']!=200 or receipt['request_sha256']!=run.fingerprint(request) or plan['request_sha256']!=receipt['request_sha256']:
        raise run.ProviderBlocked('Saved Gemma batch rejected or mismatched; no automatic retry')
    keys=plan['case_keys']
    if not 1<=len(keys)<=5 or len(keys)!=len(set(keys)) or not set(keys)<=set(items):
        raise run.PublicationConflict('Saved batch case coverage changed')
    if plan.get('jev_requests')!={k:run.fingerprint(items[k]['jev_payload']) for k in keys}:
        raise run.PublicationConflict('Saved batch belongs to a different frozen JEV snapshot')
    payloads={k:items[k]['jev_payload'] for k in plan['case_keys']}
    _,usage=recommendations_from_receipt(receipt,payloads)
    total=len(plan['case_keys'])
    for index,k in enumerate(plan['case_keys']):
        allocated={name:usage.get(name,0)//total+(index<usage.get(name,0)%total)
                   for name in ('promptTokenCount','candidatesTokenCount','thoughtsTokenCount')}
        allocated.update(allocation='even_split_for_display_not_measured_per_case',
            shared_batch_request_sha256=receipt['request_sha256'],shared_batch_usage=usage)
        binding={'case_key':k,'jev_request_sha256':run.fingerprint(items[k]['jev_payload']),
            'batch_folder':str(batch_folder.relative_to(FOLDER)),
            'batch_request_sha256':receipt['request_sha256'],'batch_response_sha256':run.fingerprint(receipt),
            'context_version':'document_summary_recommendations_v2','allocated_usage':allocated}
        run.once_json(FOLDER/k/'gemma-batch-binding.private.json',binding)
    return usage


def execute_batch(conn,settings,batch_folder,group,contexts,items):
    selected={k:contexts[k] for k in group}
    request=batch_request(selected)
    plan={'case_keys':group,'request_sha256':run.fingerprint(request),
          'jev_requests':{k:run.fingerprint(items[k]['jev_payload']) for k in group},
          'recommendation_model':run.GEMMA_MODEL,
          'recovery_basis':'Human selected recommendation model; exact shared summary context counted; saved JEV reused',
          'headroom_budget':INPUT_BUDGET}
    run.once_json(batch_folder/'plan.private.json',plan)
    count=count_input(settings,request,batch_folder)
    if count>INPUT_BUDGET:return False,count
    # Bind every opportunity to its still-current source before shared generation.
    for k in group:
        case_folder=FOLDER/k
        run.predispatch(conn,frozen_global,items[k]['case'],case_folder)
        claim=json.loads((case_folder/'pg-claim.private.json').read_text())
        run.verify_owned_claim(conn,claim,items[k]['case'],items[k],case_folder)
    paced_wait(FOLDER,count)
    receipt=run.reserved_dispatch(conn,items[group[0]]['case'],batch_folder,'gemma',request,
        url=f'https://generativelanguage.googleapis.com/v1beta/models/{run.GEMMA_MODEL}:generateContent',
        api_key=settings.gemini_api_key,header_name='x-goog-api-key')
    usage=bind_saved_batch(batch_folder,items)
    run.emit({'status':'gemma_batch_ready','opportunities':len(group),'input_tokens':usage['promptTokenCount'],
              'output_tokens':usage['candidatesTokenCount'],'batch':batch_folder.name})
    return True,count


frozen_global=None


def main():
    global frozen_global
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--recommendation-model',choices=['gemma-4-31b-it','gemini-3.1-flash-lite'],default='gemma-4-31b-it')
    args=parser.parse_args()
    if args.limit is not None and args.limit<1:raise ValueError('Positive explicit limit required')
    global INPUT_BUDGET
    run.select_recommendation_model(args.recommendation_model)
    if run.GEMMA_MODEL=='gemini-3.1-flash-lite':INPUT_BUDGET=30000
    frozen,cards,prepared,_=run.prepare(FOLDER)
    frozen_global=frozen
    cases=prepared[:args.limit]
    if not args.execute:
        run.emit({'status':'prepared_shared_batches','ready':len(cases),'inference_calls':0});return
    for path in ('scripts/run_dinaut_three_dot_gemma_batches.py','scripts/run_dinaut_three_dot_matches.py',
                 'scripts/dinaut_three_dot_persistence.py','scripts/dinaut_three_dot_selector.py','scripts/match_provider_journal.py',
                 'workers/seace/src/buenapro_worker/matching/jev_recommendations.py'):
        if subprocess.run(['git','diff','--quiet','HEAD','--',path],cwd=ROOT).returncode or subprocess.run(
                ['git','ls-files','--error-unmatch',path],cwd=ROOT,capture_output=True).returncode:
            raise ValueError('Commit the verified recovery before production execution')
    jev_key=dotenv_values(ROOT/'.env.jev.local').get('TYPESAFE_API_KEY')
    if not jev_key:raise ValueError('JEV credential unavailable')
    with (FOLDER/'coordinator.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with run.production_settings(ssh_host='ubuntu@168.129.177.199',ssh_key=ROOT/'credentials/ssh-key-2026-07-14.key') as settings:
            with psycopg.connect(settings.database_url,row_factory=dict_row,autocommit=True) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(hashtext('dinaut_three_dot_llm2'),hashtext(%s)) AS locked",(run.PROFILE_ID,)).fetchone()['locked']:
                    raise run.ProviderBlocked('Another matching coordinator owns DIN')
                run.private_json(FOLDER/'flash-lite-run-code.private.json',{
                    'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    'image':'existing production services unchanged','recommendation_model':run.GEMMA_MODEL,
                    'started_utc':datetime.now(timezone.utc).isoformat()})
                items={key(i):i for i in cases}
                # Recover successful shared responses before packing any remaining cases.
                for p in sorted((FOLDER/'gemma-batches').glob('*/gemma-response.private.json')):
                    plan=json.loads(p.with_name('plan.private.json').read_text())
                    if set(plan['case_keys'])<=set(items):bind_saved_batch(p.parent,items)
                contexts={};results=[]
                for item in cases:
                    k=key(item);case_folder=FOLDER/k
                    if (case_folder/'published.private.json').exists() or (case_folder/'gemma-batch-binding.private.json').exists():
                        result=run.run_case(conn,settings,frozen,cards,item,FOLDER,jev_key)
                        results.append(result);continue
                    run.run_case(conn,settings,frozen,cards,item,FOLDER,jev_key,jev_only=True)
                    scoring=json.loads((case_folder/'scoring.private.json').read_text())
                    contexts[k]=build_small_recommendation_context(item['jev_payload'],scoring,cards)
                    run.emit({'status':'jev_ready','source_id':item['case']['source_id'],'completed_scoring':len(contexts),
                              'score':scoring['score']})
                pending=list(contexts)
                while pending:
                    size=min(5,len(pending))
                    while True:
                        group=pending[:size]
                        digest=run.fingerprint(batch_request({k:contexts[k] for k in group}))
                        batch_folder=FOLDER/'gemma-batches'/('batch-'+digest[:16])
                        success,count=execute_batch(conn,settings,batch_folder,group,contexts,items)
                        if success:break
                        if size==1:raise run.ProviderBlocked('One complete summary context still exceeds the observed Gemma quota; no generation sent')
                        size-=1
                    for k in group:
                        result=run.run_case(conn,settings,frozen,cards,items[k],FOLDER,jev_key)
                        results.append(result)
                        run.emit({'status':'published','source':result['source_key'],'source_id':result['source_id'],
                                  'match_id':result['match_id'],'score':result['score'],'completed':len(results),'target':len(cases)})
                        run.private_json(FOLDER/'progress.private.json',{'results':results,'complete':len(results)==len(prepared)})
                    pending=pending[size:]
                run.emit({'status':'complete' if len(results)==len(prepared) else 'pilot_complete','published':len(results),
                          'target':len(prepared)})


if __name__=='__main__':
    try:main()
    except Exception as exc:
        run.emit({'status':'blocked','error_class':type(exc).__name__,
            'reason':str(exc) if isinstance(exc,(ValueError,run.ProviderBlocked)) else 'Inspect private receipts; no automatic retry',
            'automatic_retry':False})
        raise SystemExit(2)
