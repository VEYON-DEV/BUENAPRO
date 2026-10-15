"""Opt-in DIN three-dot batch: JEV comparisons/rules, Gemma recommendations.

Default is offline preparation from an immutable read-only snapshot. --execute
is the explicit production operation. No ingestion, PDFs, notifications,
automatic paid retries, migrations or worker restarts are performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'workers/seace/src'))
sys.path.insert(0, str(ROOT/'scripts'))

import psycopg
from psycopg.rows import dict_row
from dotenv import dotenv_values
import tiktoken

from buenapro_worker.matching.jev_recommendations import (
    POLICY_VERSION, JEV_MODEL, RECOMMENDATION_PROMPT, build_profile_cards,
    build_jev_payload, score_match, build_recommendation_context,
    validate_recommendations, token_budget, build_small_recommendation_context,
)
from buenapro_worker.extraction.gemini import parse_json_response
from buenapro_worker.matching.profile_evidence import guard_participation_actions
from buenapro_worker.matching.readiness import require_match_ready
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2.repository import complete_evaluation
from dinaut_three_dot_selector import PROFILE_ID, TENANT_ID, PARAMS, prod2_sql, PROD6_SQL
from dinaut_three_dot_persistence import (
    PublicationConflict, profile_fingerprint, source_fingerprint,
    create_evaluation_before_dispatch, publish_case,
)
from match_provider_journal import ProviderBlocked, canonical, fingerprint, private_json, dispatch_once

GEMMA_MODEL = 'gemma-4-31b-it'
MODEL = JEV_MODEL + '+' + GEMMA_MODEL
ALLOWED_REPLACEMENTS = frozenset({('seace_prod2',129),('seace_prod2',130),('seace_prod6',911)})


def select_recommendation_model(model):
    global GEMMA_MODEL,MODEL
    if model not in {'gemma-4-31b-it','gemini-3.1-flash-lite'}:
        raise ValueError('Unsupported explicitly selected recommendation model')
    GEMMA_MODEL=model
    MODEL=JEV_MODEL+'+'+model


def emit(value):
    print(json.dumps(value,ensure_ascii=False,default=str),flush=True)


def once_json(path, value):
    if path.exists():
        if fingerprint(json.loads(path.read_text())) != fingerprint(value):
            raise PublicationConflict('Immutable local snapshot changed')
    else:
        private_json(path,value,exclusive=True)


def prepare(folder):
    inputs_path=folder/'inputs.private.json'
    frozen=json.loads(inputs_path.read_text())
    receipt=json.loads((folder/'preflight.private.json').read_text())
    if hashlib.sha256(inputs_path.read_bytes()).hexdigest()!=receipt['inputs_sha256']:
        raise ValueError('Frozen inputs hash mismatch')
    if (receipt['profile_id'],receipt['tenant_id'])!=(PROFILE_ID,TENANT_ID):
        raise ValueError('Batch outside the authorized DIN tenant/profile')
    selected=[c for c in frozen['cases'] if c['ready']]
    if not selected or any(c['fit_level']!=3 for c in selected):
        raise ValueError('Only ready three-dot opportunities are authorized')
    if len(selected)!=len({(c['source_key'],c['source_id']) for c in selected}):
        raise ValueError('Duplicate batch identity')
    cards=build_profile_cards(frozen['profile'])
    cards['company']['business_lines']=[{k:line[k] for k in
        ('nombre','keyword_phrases','keyword_terms','cubso_segmentos')} for line in frozen['business_lines']]
    encoder=tiktoken.get_encoding('o200k_base')
    counter=lambda text:len(encoder.encode(text))
    cases=[]
    for case in selected:
        require_match_ready(case,case.get('facets') or [],provisional_source_review=True)
        if any(r['status'] in {'running','uncertain'} for r in case.get('prior_match_reservations') or []):
            raise ProviderBlocked('Historical running/uncertain matching attempt in selected case')
        if case.get('prior_worker_jobs'):
            raise ProviderBlocked('Historical worker job needs reconciliation before dispatch')
        if case.get('existing_match_id') and (case['source_key'],case['existing_match_id']) not in ALLOWED_REPLACEMENTS:
            raise PublicationConflict('Existing match outside the authorized replacement set')
        opportunity={'codigo':case['code'],'descripcion':case['description'],
            'entidad':case['buyer_name'],'resumen':case['summary_json'],
            'requires_human_review':case['requires_human_review'],
            'source_key':case['source_key'],'source_id':case['source_id']}
        payload=build_jev_payload(cards,opportunity,case['facets'],token_counter=counter)
        binding={'source':source_fingerprint(case),'profile':profile_fingerprint(frozen['profile']),
            'lines':fingerprint(sorted(frozen['business_lines'],key=lambda l:str(l['id']))),
            'jev_request':fingerprint(payload),'recommendation_model':GEMMA_MODEL,
            'recommendation_prompt':fingerprint(RECOMMENDATION_PROMPT),'policy':POLICY_VERSION,
            'as_of':receipt['cutoff_utc']}
        cases.append({'case':case,'jev_payload':payload,'binding':binding,
                      'pipeline_request_hash':fingerprint(binding),
                      'token_budget':token_budget(payload,token_counter=counter)})
    manifest={'profile_id':PROFILE_ID,'tenant_id':TENANT_ID,'inputs_sha256':receipt['inputs_sha256'],
        'policy':POLICY_VERSION,'models':MODEL,'cases':[{k:x[k] for k in
            ('binding','pipeline_request_hash','token_budget')} for x in cases]}
    manifest_name=('execution-manifest.private.json' if GEMMA_MODEL=='gemma-4-31b-it'
                   else 'execution-manifest-flash-lite.private.json')
    once_json(folder/manifest_name,manifest)
    return frozen,cards,cases,manifest


def source_reader(conn,case):
    sql=prod2_sql() if case['source_key']=='seace_prod2' else PROD6_SQL
    return conn.execute('SELECT * FROM ('+sql+') selected WHERE source_id=%(source_id)s',
                        {**PARAMS,'source_id':case['source_id']}).fetchone()


def current_match(conn,case):
    prod2=case['source_key']=='seace_prod2'
    table,identity,value=('opportunity_matches','opportunity_id',case['opportunity_id']) if prod2 else ('matches','id_contrato',case['source_id'])
    row=conn.execute(f'SELECT * FROM {table} WHERE profile_id=%s AND {identity}=%s',(PROFILE_ID,value)).fetchone()
    provenance=conn.execute('SELECT * FROM prod2_match_provenance WHERE match_id=%s',(row['id'],)).fetchone() if prod2 and row else None
    return {'previous_match':row,'previous_provenance':provenance}


def publication_source_reader(conn,case,frozen):
    lines=conn.execute('SELECT * FROM business_lines WHERE profile_id=%s AND is_active FOR SHARE',(PROFILE_ID,)).fetchall()
    if fingerprint(sorted(lines,key=lambda l:str(l['id'])))!=fingerprint(sorted(frozen['business_lines'],key=lambda l:str(l['id']))):
        raise PublicationConflict('Business lines changed after provider evaluation')
    return source_reader(conn,case)


def predispatch(conn,frozen,case,folder):
    profile=conn.execute('SELECT * FROM company_profiles WHERE id=%s AND tenant_id=%s AND is_active',(PROFILE_ID,TENANT_ID)).fetchone()
    if not profile or profile_fingerprint(profile)!=profile_fingerprint(frozen['profile']):
        raise PublicationConflict('DIN profile changed since the frozen selection')
    lines=conn.execute('SELECT * FROM business_lines WHERE profile_id=%s AND is_active',(PROFILE_ID,)).fetchall()
    if fingerprint(sorted(lines,key=lambda l:str(l['id'])))!=fingerprint(sorted(frozen['business_lines'],key=lambda l:str(l['id']))):
        raise PublicationConflict('DIN business lines changed since selection')
    live=source_reader(conn,case)
    if not live or source_fingerprint(live)!=source_fingerprint(case):
        raise PublicationConflict('Source, requirements or three-dot affinity changed')
    require_match_ready(live,live.get('facets') or [],provisional_source_review=True)
    owned_claim=json.loads((folder/'pg-claim.private.json').read_text()) if (folder/'pg-claim.private.json').exists() else None
    if any(r['status'] in {'running','uncertain'} and r['id']!=(owned_claim or {}).get('id')
           for r in live.get('prior_match_reservations') or []):
        raise PublicationConflict('Another unresolved provider attempt owns this source/profile')
    previous=current_match(conn,case)
    if (previous['previous_match'] or {}).get('id')!=case.get('existing_match_id'):
        raise PublicationConflict('Another analysis was published since selection')
    if fingerprint((previous['previous_match'] or {}).get('breakdown_json',{}).get('meta'))!=fingerprint(case.get('existing_match_meta')):
        raise PublicationConflict('The existing analysis changed since the read-only selection')
    if live.get('prior_worker_jobs'):
        raise PublicationConflict('Another matching worker attempted this case; reconcile before dispatch')
    once_json(folder/'baseline-match.private.json',previous)
    return previous


def reserved_dispatch(conn,case,case_folder,stage,payload,**kwargs):
    """A PG stage ledger also blocks replay from another local receipt folder."""
    request_hash=fingerprint(payload)
    metadata={'profile_id':PROFILE_ID,'source_key':case['source_key'],'source_id':case['source_id'],
              'provider_request_hash':request_hash,'policy':POLICY_VERSION,
              'model':JEV_MODEL if stage=='jev' else GEMMA_MODEL,'no_automatic_retry':True}
    stage_name='dinaut_three_dot_'+stage
    prior=conn.execute("""SELECT id,status FROM pipeline_events WHERE stage=%s
        AND metadata_json->>'profile_id'=%s AND metadata_json->>'provider_request_hash'=%s
        ORDER BY id DESC LIMIT 1""",(stage_name,PROFILE_ID,request_hash)).fetchone()
    if prior and not (case_folder/f'{stage}-response.private.json').exists():
        raise ProviderBlocked('A durable provider attempt exists without the local response; do not repay')
    if prior:
        event_id=prior['id']
    else:
        event_id=conn.execute("""INSERT INTO pipeline_events(id_contrato,stage,status,metadata_json)
            VALUES(%s,%s,'started',%s::jsonb) RETURNING id""",
            (case['source_id'] if case['source_key']=='seace_prod6' else None,stage_name,canonical(metadata))).fetchone()['id']
    try:
        receipt=dispatch_once(case_folder,stage,payload,**kwargs)
        conn.execute("UPDATE pipeline_events SET status='ok',metadata_json=metadata_json || %s::jsonb WHERE id=%s",
            (canonical({'response_sha256':fingerprint(receipt),'http_status':receipt['http_status']}),event_id))
        return receipt
    except Exception as exc:
        conn.execute("UPDATE pipeline_events SET status='failed',error=%s,metadata_json=metadata_json || %s::jsonb WHERE id=%s",
            (type(exc).__name__,canonical({'response_preserved':(case_folder/f'{stage}-response.private.json').exists(),
                'unresolved_sent_request':(case_folder/f'{stage}-started.private.json').exists()}),event_id))
        raise


def verify_owned_claim(conn,claim,case,item,case_folder,stage=None):
    if not claim:
        return
    row=conn.execute('SELECT *,lease_until>now() AS lease_live FROM prod2_evaluations WHERE id=%s',
                     (claim['id'],)).fetchone()
    expected=(case['source_id'],PROFILE_ID,'gemini_match',MODEL,POLICY_VERSION,item['pipeline_request_hash'])
    actual=(row['id_convocatoria'],str(row['profile_id']),row['kind'],row['model'],
            row['policy_version'],row['request_hash']) if row else None
    if actual!=expected or str(row.get('claim_token'))!=claim.get('claim_token'):
        raise ProviderBlocked('Stored PG claim ownership or frozen request binding changed')
    receipts_ready=all((case_folder/f'{stage}-response.private.json').exists() and
        json.loads((case_folder/f'{stage}-response.private.json').read_text()).get('http_status')==200
        for stage in ('jev','gemma'))
    if row['status']=='running' and row['lease_live'] is True:
        return
    if row['status']=='uncertain' and stage is not None:
        response_path=case_folder/f'{stage}-response.private.json'
        if response_path.exists() and json.loads(response_path.read_text()).get('http_status')==200:
            return  # This exact stage is cached; journal cannot send it again.
    if row['status'] in {'done','uncertain'} and receipts_ready:
        return  # Only replay saved responses; dispatch_once will make no HTTP call.
    raise ProviderBlocked('PG reservation is not live; no new inference is authorized by an old claim')


def gemma_request(payload,scoring,cards=None):
    context=(build_small_recommendation_context(payload,scoring,cards) if cards is not None
             else build_recommendation_context(payload,scoring))
    return {'systemInstruction':{'parts':[{'text':RECOMMENDATION_PROMPT}]},
        'contents':[{'role':'user','parts':[{'text':canonical(context)}]}],
        'generationConfig':{'temperature':0,'maxOutputTokens':8192,
                            'thinkingConfig':{'thinkingLevel':'minimal'}}}


def parse_gemma(body,payload):
    candidates=body.get('candidates') or []
    if len(candidates)!=1 or candidates[0].get('finishReason')!='STOP':
        raise ProviderBlocked('Gemma did not return a complete single response; receipt retained')
    text=''.join(p.get('text','') for p in candidates[0].get('content',{}).get('parts',[]) if not p.get('thought'))
    result=validate_recommendations(parse_json_response(text),payload)
    usage=body.get('usageMetadata') or {}
    if any(type(usage.get(key)) is not int or usage[key]<0 for key in ('promptTokenCount','candidatesTokenCount')):
        raise ProviderBlocked('Gemma usage missing; preserve response before further calls')
    return result,usage


def saved_batch_recommendation(case_folder,item):
    binding=json.loads((case_folder/'gemma-batch-binding.private.json').read_text())
    if binding['case_key']!=f"{item['case']['source_key']}-{item['case']['source_id']}" or binding['jev_request_sha256']!=fingerprint(item['jev_payload']):
        raise PublicationConflict('Shared Gemma recommendation belongs to another JEV input')
    batch_folder=(case_folder.parent/binding['batch_folder']).resolve()
    if not batch_folder.is_relative_to((case_folder.parent/'gemma-batches').resolve()):
        raise PublicationConflict('Unexpected shared Gemma receipt path')
    request=json.loads((batch_folder/'gemma-request.private.json').read_text())
    receipt=json.loads((batch_folder/'gemma-response.private.json').read_text())
    if (receipt['http_status']!=200 or fingerprint(request)!=receipt['request_sha256']
            or receipt['request_sha256']!=binding['batch_request_sha256']
            or fingerprint(receipt)!=binding['batch_response_sha256']):
        raise PublicationConflict('Shared Gemma receipt hash mismatch')
    candidates=receipt['body'].get('candidates') or []
    if len(candidates)!=1 or candidates[0].get('finishReason')!='STOP':
        raise ProviderBlocked('Incomplete shared Gemma response')
    text=''.join(p.get('text','') for p in candidates[0]['content']['parts'] if not p.get('thought'))
    result=parse_json_response(text)
    matches=[r for r in result['evaluations'] if r['case_key']==binding['case_key']]
    if len(matches)!=1:
        raise PublicationConflict('Shared Gemma output coverage changed')
    recommendation=validate_recommendations({k:v for k,v in matches[0].items() if k!='case_key'},item['jev_payload'])
    return recommendation,binding['allocated_usage']


def transition_rejected_gemma_claim(conn,case,item,case_folder,claim):
    """One explicitly authorized model change; the original paid JEV is reused."""
    if GEMMA_MODEL!='gemini-3.1-flash-lite' or not claim:
        return claim
    row=conn.execute('SELECT * FROM prod2_evaluations WHERE id=%s',(claim['id'],)).fetchone()
    if row and row['model']==MODEL:return claim
    old_rejection=json.loads((case_folder/'gemma-response.private.json').read_text())
    jev=json.loads((case_folder/'jev-response.private.json').read_text())
    old_request=json.loads((case_folder/'jev-request.private.json').read_text())
    rejected_request=json.loads((case_folder/'gemma-request.private.json').read_text())
    old_binding={**item['binding'],'recommendation_model':'gemma-4-31b-it'}
    old_pipeline_hash=fingerprint(old_binding)
    if (case['source_key']!='seace_prod2' or case['source_id']!=1046782 or claim['id']!=2142
            or not row or row['id_convocatoria']!=case['source_id'] or str(row['profile_id'])!=PROFILE_ID
            or row['model']!=JEV_MODEL+'+gemma-4-31b-it' or row['status']!='uncertain'
            or row['kind']!='gemini_match' or row['policy_version']!=POLICY_VERSION
            or row['request_hash']!=old_pipeline_hash
            or str(row['claim_token'])!=claim.get('claim_token')
            or old_rejection['http_status']!=429 or jev['http_status']!=200
            or jev.get('request_sha256')!=fingerprint(old_request)
            or jev['body'].get('model')!=JEV_MODEL
            or old_rejection.get('request_sha256')!=fingerprint(rejected_request)
            or fingerprint(old_request)!=fingerprint(item['jev_payload'])):
        raise ProviderBlocked('Old provider chain cannot be closed without verified rejection and the paid JEV receipt')
    violations=[v for detail in old_rejection['body'].get('error',{}).get('details',[])
                for v in detail.get('violations',[])]
    if not any(v.get('quotaValue')=='16000' and 'InputTokensPerModelPerMinute' in v.get('quotaId','') for v in violations):
        raise ProviderBlocked('Prior error is not the diagnosed Gemma input-token quota')
    usage=jev['body']['usage']
    proof={'reason':'Human selected Gemini 3.1 Flash-Lite after the verified Gemma429 quota rejection',
        'old_evaluation_id':claim['id'],'reused_jev_request_sha256':fingerprint(old_request),
        'old_gemma_response_sha256':fingerprint(old_rejection),'new_model':MODEL,
        'jev_cost_usd':str(Decimal(usage['input_tokens'])*Decimal('.042')/Decimal(1_000_000)),
        'old_gemma_generation_returned':False,'new_jev_inference_authorized':False}
    once_json(case_folder/'provider-transition.private.json',proof)
    once_json(case_folder/'pg-claim-gemma-original.private.json',claim)
    with conn.transaction():
        changed=conn.execute("""UPDATE prod2_evaluations SET status='failed',result_json=%s::jsonb,
            input_tokens=%s,output_tokens=%s,cost_usd=%s,completed_at=now(),updated_at=now()
            WHERE id=%s AND claim_token=%s AND status='uncertain'
              AND id_convocatoria=%s AND profile_id=%s AND kind='gemini_match'
              AND model=%s AND policy_version=%s AND request_hash=%s RETURNING id""",
            (canonical(proof),usage['input_tokens'],usage['output_tokens'],proof['jev_cost_usd'],claim['id'],claim['claim_token'],
             case['source_id'],PROFILE_ID,JEV_MODEL+'+gemma-4-31b-it',POLICY_VERSION,old_pipeline_hash)).fetchone()
        if not changed:raise PublicationConflict('Original rejected provider claim changed')
    new_claim=create_evaluation_before_dispatch(conn,case,profile_id=PROFILE_ID,
        request_hash=item['pipeline_request_hash'],model=MODEL,policy_version=POLICY_VERSION)
    if not new_claim or new_claim['status']!='claimed':
        raise ProviderBlocked('New explicitly selected model reservation needs reconciliation')
    private_json(case_folder/'pg-claim.private.json',new_claim)
    return new_claim


def compose(case,payload,scoring,recommendations,cards,item,usage):
    explained={r['source_index']:r for r in recommendations['requisitos']}
    rows=[]
    refs=cards['reference_map']
    for original in scoring['requisitos']:
        explanation=explained[original['source_index']]
        row={**original,'evidence_explanation':explanation['evidencia'],
            'gap':explanation['brecha'] or None,'accion':explanation['accion'] or None,
            'evidence_refs':list(dict.fromkeys([refs[ref]['document_id'] for ref in
                [*original.get('evidence_refs',[]),*explanation['evidence_refs']]]))}
        if row['estado']=='cumple':
            row['gap']=None
            row['accion']=None
        rows.append(row)
    actions=guard_participation_actions(rows,recommendations['acciones_recomendadas'],case['summary_json'])
    summary=recommendations['resumen']
    if scoring['coverage']['partial_score']:
        summary='Puntaje provisional parcial: hay criterios sustantivos aún sin cuantificar. '+summary
    breakdown={'resumen':summary,'requisitos':rows,'acciones_recomendadas':actions,
        'administrative_checklist':scoring['administrative_checklist'],
        'capacity_scoring':scoring,'fortalezas':[r['requisito'] for r in rows if r['estado']=='cumple'][:3],
        'riesgo_principal':next((r['gap'] or r['requisito'] for r in rows if r['estado']!='cumple'),None),
        'meta':{'model':MODEL,'score_model':JEV_MODEL,'recommendation_model':GEMMA_MODEL,
            'provider':'TypeSafe + Google Gemini API','policy_version':POLICY_VERSION,
            'prompt_version':POLICY_VERSION,'context_version':POLICY_VERSION,
            'score_validated':False,'semantic_validated':False,
            'source_review_required':case['requires_human_review'],
            'company_evidence_review_required':scoring['source_review_required'],
            'score_basis':'substantive_capacity_weighted_rules_not_win_probability',
            'administrative_score_impact':False,'request_hash':item['pipeline_request_hash'],
            'execution':'dinaut_three_dot_explicit_batch','fit_level':3,
            'profile_snapshot_hash':item['binding']['profile'],
            'input_tokens':usage['input_tokens'],'output_tokens':usage['output_tokens'],
            'cost_usd':usage['cost_usd'],'provider_usage':usage['providers'],
            'analyzed_at':datetime.now(timezone.utc).isoformat()}}
    missing=[{'facet':r['categoria'],'label':r['requisito'],'estado':r['estado'],
              'accion':r['accion'],'gap':r['gap'],'critico':r['critico']}
             for r in rows if r['estado']!='cumple']
    return breakdown,missing


def run_case(conn,settings,frozen,cards,item,folder,jev_key,*,jev_only=False):
    case=item['case'];case_folder=folder/f"{case['source_key']}-{case['source_id']}"
    result_path=case_folder/'published.private.json'
    if result_path.exists():
        saved=json.loads(result_path.read_text());current=current_match(conn,case)['previous_match']
        if not current or current['id']!=saved['match_id'] or (current['breakdown_json'].get('meta') or {}).get('request_hash')!=item['pipeline_request_hash']:
            raise PublicationConflict('A previously published result changed; no repeat inference')
        return {**saved,'reused':True}
    previous=predispatch(conn,frozen,case,case_folder)
    claim_path=case_folder/'pg-claim.private.json'
    if claim_path.exists():
        claim=json.loads(claim_path.read_text())
        claim=transition_rejected_gemma_claim(conn,case,item,case_folder,claim)
    else:
        claim=create_evaluation_before_dispatch(conn,case,profile_id=PROFILE_ID,
            request_hash=item['pipeline_request_hash'],model=MODEL,policy_version=POLICY_VERSION)
        if claim and claim['status']!='claimed':
            raise ProviderBlocked('Existing PROD2 reservation needs reconciliation; no repeat inference')
        private_json(claim_path,claim,exclusive=True)
    try:
        verify_owned_claim(conn,claim,case,item,case_folder,stage='jev')
        jev=reserved_dispatch(conn,case,case_folder,'jev',item['jev_payload'],url='https://api.typesafe.ai/v1/systemone',
            api_key=jev_key,header_name='Authorization')['body']
        scoring=score_match(item['jev_payload'],jev,at=datetime.fromisoformat(frozen['receipt']['cutoff_utc']),profile_cards=cards)
        once_json(case_folder/'scoring.private.json',scoring)
        if scoring['score'] is None:
            raise ProviderBlocked('JEV produced no quantified capacity; retain the response for review')
        if jev_only:
            return {'source_key':case['source_key'],'source_id':case['source_id'],
                'status':'jev_ready','score':scoring['score'],
                'cost_usd':str(Decimal(jev['usage']['input_tokens'])*Decimal('.042')/Decimal(1_000_000))}
        if (case_folder/'gemma-batch-binding.private.json').exists():
            recommendations,gusage=saved_batch_recommendation(case_folder,item)
        else:
            request=gemma_request(item['jev_payload'],scoring,cards)
            verify_owned_claim(conn,claim,case,item,case_folder)
            gemma=reserved_dispatch(conn,case,case_folder,'gemma',request,
                url=f'https://generativelanguage.googleapis.com/v1beta/models/{GEMMA_MODEL}:generateContent',
                api_key=settings.gemini_api_key,header_name='x-goog-api-key')['body']
            recommendations,gusage=parse_gemma(gemma,item['jev_payload'])
        once_json(case_folder/'recommendations.private.json',recommendations)
        ju=jev['usage'];jev_cost=Decimal(ju['input_tokens'])*Decimal('.042')/Decimal(1_000_000)
        output=gusage['candidatesTokenCount']+gusage.get('thoughtsTokenCount',0)
        explanation_cost=(Decimal(gusage['promptTokenCount'])*Decimal('.25')+Decimal(output)*Decimal('1.50'))/Decimal(1_000_000) if GEMMA_MODEL=='gemini-3.1-flash-lite' else Decimal(0)
        reused_paid=(case_folder/'provider-transition.private.json').exists()
        cost=str(explanation_cost+(Decimal(0) if reused_paid else jev_cost))
        usage={'input_tokens':ju['input_tokens']+gusage['promptTokenCount'],
            'output_tokens':ju['output_tokens']+gusage['candidatesTokenCount']+gusage.get('thoughtsTokenCount',0),
            'cost_usd':cost,'providers':{'jev':{'model':JEV_MODEL,**ju,'cost_usd':str(jev_cost),
                'reused_from_prior_provider_chain':reused_paid,'incurred_in_this_chain_usd':'0' if reused_paid else str(jev_cost)},
                'recommendations':{'model':GEMMA_MODEL,**gusage,'published_rate_cost_usd':str(explanation_cost),
                    'rate_source':'https://ai.google.dev/gemini-api/docs/pricing','provider_bill_verified':False}}}
        composed_path=case_folder/'composed.private.json'
        if composed_path.exists():
            composed=json.loads(composed_path.read_text())
            if (composed['score']!=scoring['score'] or composed['usage']!=usage
                    or composed['breakdown']['meta']['request_hash']!=item['pipeline_request_hash']):
                raise PublicationConflict('Stored composition differs from the paid responses')
            breakdown,missing=composed['breakdown'],composed['missing_actions']
        else:
            breakdown,missing=compose(case,item['jev_payload'],scoring,recommendations,cards,item,usage)
            once_json(composed_path,{'score':scoring['score'],'breakdown':breakdown,'missing_actions':missing,'usage':usage})
        if claim:
            reservation=conn.execute('SELECT status FROM prod2_evaluations WHERE id=%s',(claim['id'],)).fetchone()
            if reservation['status']!='done':
                complete_evaluation(conn,claim['id'],claim['claim_token'],
                    {'extraction_id':case['extraction_id'],'profile_hash':profile_fingerprint(frozen['profile']),
                     'request_hash':item['pipeline_request_hash'],'provider_usage':usage['providers'],
                     'score':scoring['score'],'score_validated':False},
                    input_tokens=usage['input_tokens'],output_tokens=usage['output_tokens'],cost_usd=usage['cost_usd'])
        result=publish_case(conn,case,profile_id=PROFILE_ID,tenant_id=TENANT_ID,
            expected_profile_fingerprint=profile_fingerprint(frozen['profile']),score=scoring['score'],
            verdict='gris',breakdown=breakdown,missing_actions=missing,
            source_reader=lambda connection,row:publication_source_reader(connection,row,frozen),
            backup_callback=lambda value:once_json(case_folder/'publication-backup.private.json',value),
            evaluation_id=claim['id'] if claim else None,
            expected_previous=previous['previous_match'],expected_provenance=previous['previous_provenance'],
            allowed_replacements=ALLOWED_REPLACEMENTS)
        result.update(cost_usd=cost,providers=usage['providers'],pipeline_request_hash=item['pipeline_request_hash'])
        private_json(result_path,result,exclusive=True)
        return result
    except Exception:
        if claim:
            conn.execute("UPDATE prod2_evaluations SET status='uncertain',updated_at=now() WHERE id=%s AND claim_token=%s AND status='running'",(claim['id'],claim['claim_token']))
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--jev-only',action='store_true',help='Prepare only the reserved scoring stage for shared Gemma batches')
    parser.add_argument('--recommendation-model',choices=['gemma-4-31b-it','gemini-3.1-flash-lite'],default='gemma-4-31b-it')
    args=parser.parse_args();folder=args.folder.resolve()
    select_recommendation_model(args.recommendation_model)
    if folder!=(ROOT/'tmp/dinaut-three-dot-20261015').resolve():
        raise ValueError('Use the canonical DIN three-dot folder; never copy a paid batch to a new folder')
    frozen,cards,cases,manifest=prepare(folder)
    emit({'status':'prepared','ready_three_dots':len(cases),'sources':dict(Counter(x['case']['source_key'] for x in cases)),
          'excluded_por_leer':sum(not c['ready'] for c in frozen['cases']),
          'documents':cards['coverage']['document_count'],'inference_calls':0})
    if not args.execute:return
    if args.limit is not None and args.limit<1:raise ValueError('Positive explicit limit required')
    jev_key=dotenv_values(ROOT/'.env.jev.local').get('TYPESAFE_API_KEY')
    if not jev_key:raise ValueError('JEV credential unavailable')
    # Record the Git code identity. The private inputs/receipts never enter Git.
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    tracked=['scripts/run_dinaut_three_dot_matches.py','scripts/dinaut_three_dot_selector.py',
        'scripts/dinaut_three_dot_persistence.py','scripts/match_provider_journal.py',
        'workers/seace/src/buenapro_worker/matching/jev_recommendations.py']
    if subprocess.run(['git','diff','--quiet','HEAD','--',*tracked],cwd=ROOT).returncode:
        raise ValueError('Commit the verified operational code before production execution')
    for path in tracked:
        if subprocess.run(['git','ls-files','--error-unmatch',path],cwd=ROOT,capture_output=True).returncode:
            raise ValueError('Uncommitted operational code is not executable in production')
    with (folder/'coordinator.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with production_settings(ssh_host='ubuntu@168.129.177.199',ssh_key=ROOT/'credentials/ssh-key-2026-07-14.key') as settings:
            with psycopg.connect(settings.database_url,row_factory=dict_row,autocommit=True) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(hashtext('dinaut_three_dot_llm2'),hashtext(%s)) AS locked",(PROFILE_ID,)).fetchone()['locked']:
                    raise ProviderBlocked('Another DIN matching coordinator owns the profile')
                private_json(folder/'run-code.private.json',{'commit':commit,'image':'existing production services unchanged','started_utc':datetime.now(timezone.utc).isoformat()})
                results=[]
                for item in cases[:args.limit]:
                    conn.execute('SELECT 1').fetchone()  # A dead lock-owner connection stops further dispatches.
                    result=run_case(conn,settings,frozen,cards,item,folder,jev_key,jev_only=args.jev_only)
                    results.append(result)
                    emit({'status':'jev_ready' if args.jev_only else 'published','source':result['source_key'],'source_id':result['source_id'],
                          'match_id':result.get('match_id'),'score':result['score'],'provisional':True,
                          'completed_this_run':len(results),'known_cost_usd':result['cost_usd']})
                    private_json(folder/('jev-progress.private.json' if args.jev_only else 'progress.private.json'),
                                 {'commit':commit,'results':results,'complete':not args.jev_only and len(results)==len(cases)})
                emit({'status':'run_finished','completed_this_run':len(results),'authorized_ready':len(cases),
                      'complete':not args.jev_only and len(results)==len(cases)})


if __name__=='__main__':
    try:main()
    except Exception as exc:
        emit({'status':'blocked','error_class':type(exc).__name__,
              'reason':str(exc) if isinstance(exc,(ValueError,ProviderBlocked)) else 'Sanitized operational failure; inspect private evidence',
              'automatic_retry':False})
        raise SystemExit(2)
