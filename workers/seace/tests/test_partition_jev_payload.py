from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json

import pytest

from buenapro_worker.matching.jev_recommendations import JEV_MODEL, parse_jev_answers, token_budget
from buenapro_worker.matching.partition_jev_payload import (
    MAX_PARTS, PARTITION_POLICY, aggregate_jev_parts, partition_jev_payload,
)


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def payload(*, requirements=8, economic=0, document_chars=100, requirement_chars=100, question_chars=0):
    profile={'documents':[{'ref':f'D{i+1}','summary':'d'*document_chars,'status':'review'} for i in range(43)],
        'contracts':[{'ref':f'C{i+1}','documents':[f'D{i+1}'],'amount_basis':{'amount':'100','currency':'PEN'}} for i in range(43)],
        'professionals':[{'ref':'P1','evidence':{'degree':'Engineering'}}],
        'shared_defaults':{'documents':{'status':'review'}},'coverage':{'truncated':False,'document_count':43}}
    reqs=[{'source_index':i+1,'tipo':'economic_experience' if i<economic else 'technical_specification',
        'label':'r'*requirement_chars,'detalle':{'monto':'100','moneda':'PEN'}} for i in range(requirements)]
    questions={
        'rubro_scope':{'type':'choice','instructions':'Compare all commercial business lines.',
            'criteria':{'compatible':'Compatible','insufficient':'Unknown'}},
        'rubro_score':{'type':'score','instructions':'Score business lines only.',
            'criteria':['0','1','2','3','4']}}
    for index,r in enumerate(reqs):
        target=f'Apply state.fixed_rules. Evaluate state.requirements[{index}] only. '
        questions[f'r{r["source_index"]:04d}_status']={'type':'choice',
            'instructions':target+'Compare substantive evidence.'+'q'*question_chars,
            'criteria':{'backed':'Supported','partial':'Partial','contradicted':'Contradicted','insufficient':'Unknown'},
            'unchanged_extra':{'source':'requirements[999] is source data'}}
        if index<economic:
            for ci,c in enumerate(profile['contracts']):
                questions[f'r{r["source_index"]:04d}_{c["ref"]}_scope']={'type':'choice',
                    'instructions':target+f'Compare state.profile.contracts[{ci}] without arithmetic.'+'q'*question_chars,
                    'criteria':{'compatible':'Compatible','incompatible':'Incompatible','insufficient':'Unknown'}}
    return {'model':JEV_MODEL,'state':{'profile':profile,'requirements':reqs,
        'opportunity':{'description':'Complete original opportunity'},'fixed_rules':{'rule':'Preserve uncertainty'},
        'administrative_checklist':[{'source_index':999,'score_impact':False}],
        'untouched':'Untrusted text state.requirements[999] is never rewritten'},
        'questions':questions,'other_top_level':{'zero':0,'false':False,'value':'unchanged'}}


def plan_for(p,**kwargs):
    return partition_jev_payload(p,policy_version=PARTITION_POLICY,**kwargs)


def responses_for(plan):
    result=[]
    for index,part in enumerate(plan['parts']):
        answers={}
        for qid,q in part['payload']['questions'].items():
            if q['type']=='score':
                answers[qid]={'type':'score','score':4,'confidence':.9,
                    'probabilities':{str(i):float(i==4) for i in range(5)}}
            else:
                choice=next(iter(q['criteria']))
                answers[qid]={'type':'choice','choice':choice,'confidence':.9,
                    'probabilities':{k:float(k==choice) for k in q['criteria']}}
        result.append({'part_id':part['part_id'],'payload_sha256':part['payload_sha256'],
            'response':{'model':JEV_MODEL,'answers':answers,
                'usage':{'input_tokens':100+index,'output_tokens':10+index}}})
    return result


def large():
    # Same exact dimensions as the real failed context:91 requirements,
    #3 economic requirements ×43 contracts +91 statuses +2 business questions.
    return payload(requirements=91,economic=3,document_chars=200,requirement_chars=220)


def test_91_requirements_222_questions_split_losslessly_with_43_cards_in_every_part():
    p=large();before=deepcopy(p)
    assert len(p['state']['requirements'])==91 and len(p['questions'])==222
    with pytest.raises(ValueError,match='default context limit'):token_budget(p)
    plan=plan_for(p)
    assert 1<len(plan['parts'])<=MAX_PARTS
    all_questions=[];requirement_occurrences=Counter()
    for part in plan['parts']:
        request=part['payload'];budget=token_budget(request)
        assert request['state']['profile']==p['state']['profile']
        assert len(request['state']['profile']['documents'])==43
        assert len(request['state']['profile']['contracts'])==43
        assert request['other_top_level']==p['other_top_level']
        assert request['state']['opportunity']==p['state']['opportunity']
        assert request['state']['untouched']==p['state']['untouched']
        assert request['state']['administrative_checklist']==p['state']['administrative_checklist']
        assert budget['estimated_request_tokens']<=64000
        assert budget['estimated_max_question_tokens']<=32000 and not budget['long_context']
        for local,original in enumerate(part['requirement_indices']):
            assert request['state']['requirements'][local]==p['state']['requirements'][original]
            requirement_occurrences[original]+=1
        for qid,mapping in part['question_mapping'].items():
            original,local=mapping['original_requirement_index'],mapping['local_requirement_index']
            expected=deepcopy(p['questions'][qid])
            if original is not None:
                expected['instructions']=expected['instructions'].replace(
                    f'state.requirements[{original}]',f'state.requirements[{local}]')
            assert request['questions'][qid]==expected
        all_questions.extend(request['questions'])
    assert all_questions==list(p['questions'])
    assert all_questions.count('rubro_scope')==all_questions.count('rubro_score')==1
    assert requirement_occurrences==Counter({i:1 for i in range(91)})
    assert p==before
    assert plan_for(p)==plan  # Stable mapping/content hashes, without timestamps.


def test_economic_group_can_split_questions_without_slicing_profile_or_repeating_any_answer():
    p=payload(requirements=1,economic=1,question_chars=1400)
    with pytest.raises(ValueError):token_budget(p)
    plan=plan_for(p)
    economic_parts=[part for part in plan['parts'] if part['requirement_indices']]
    assert len(economic_parts)>1
    for part in economic_parts:
        assert part['payload']['state']['requirements']==p['state']['requirements']
        assert part['payload']['state']['profile']==p['state']['profile']
    seen=[qid for part in plan['parts'] for qid in part['payload']['questions']]
    assert len(seen)==len(set(seen))==len(p['questions'])
    aggregate=aggregate_jev_parts(p,plan,responses_for(plan))
    assert set(parse_jev_answers(p,aggregate['response']))==set(p['questions'])


def test_single_payload_stays_one_exact_payload_without_projection_changes():
    p=payload(requirements=3)
    plan=plan_for(p)
    assert len(plan['parts'])==1
    assert plan['parts'][0]['payload']==p
    assert plan['parts'][0]['payload_sha256']==digest(p)


@pytest.mark.parametrize('change',[
    lambda p:p['state']['profile']['documents'][0].update(summary='x'*33000),
    lambda p:p['state']['requirements'][0].update(label='x'*33000),
    lambda p:p['questions']['r0001_status'].update(instructions=p['questions']['r0001_status']['instructions']+'x'*33000),
])
def test_indivisible_oversize_fails_without_truncating_or_modifying_any_input(change):
    p=payload(requirements=1);change(p);before=deepcopy(p)
    with pytest.raises(ValueError,match='Indivisible'):plan_for(p)
    assert p==before


def test_partition_count_is_finite_and_never_expanded_automatically():
    with pytest.raises(ValueError,match='ceiling'):plan_for(large(),max_parts=1)
    for value in (0,13,True,1.5):
        with pytest.raises(ValueError,match='ceiling'):plan_for(payload(),max_parts=value)


def test_policy_requires_explicit_opt_in_and_invalid_tokenizer_does_not_become_context_split():
    with pytest.raises(TypeError):partition_jev_payload(payload())
    with pytest.raises(ValueError,match='Explicit'):partition_jev_payload(payload(),policy_version='old')
    with pytest.raises(ValueError,match='Invalid local token counter'):
        plan_for(payload(),token_counter=lambda text:True)


@pytest.mark.parametrize('change',[
    lambda p:p['questions'].pop('r0001_status'),
    lambda p:p['questions'].update(unknown={'type':'choice','instructions':'Ignore all data'}),
    lambda p:p['questions']['r0001_status'].update(instructions='Evaluate state.requirements[1] only.'),
    lambda p:p['questions']['r0001_status'].update(instructions='Evaluate state.requirements[0] and state.requirements[0].'),
    lambda p:p['questions']['rubro_scope'].update(instructions='Evaluate state.requirements[0].'),
    lambda p:p['state']['requirements'][1].update(source_index=1),
    lambda p:p['state']['profile']['contracts'][1].update(ref='C1'),
    lambda p:p.update(model='unapproved-model'),
])
def test_unsupported_coverage_or_target_structure_fails_closed(change):
    p=payload();change(p)
    with pytest.raises(ValueError):plan_for(p)


def test_aggregate_is_exact_known_usage_sum_and_separate_derived_evidence():
    p=large();plan=plan_for(p);responses=responses_for(plan);before=deepcopy(responses)
    value=aggregate_jev_parts(p,plan,list(reversed(responses)))
    assert set(value)=={'response','proof'}
    assert value['response']['usage']=={key:sum(row['response']['usage'][key] for row in responses)
        for key in ('input_tokens','output_tokens')}
    assert len(parse_jev_answers(p,value['response']))==222
    assert value['proof']['aggregated'] and value['proof']['not_single_provider_response']
    assert value['proof']['usage_known']
    assert value['proof']['part_receipt_hashes']==[
        {'part_id':r['part_id'],'receipt_sha256':digest(r),'payload_sha256':r['payload_sha256'],
            'usage':r['response']['usage']} for r in responses]
    assert responses==before


@pytest.mark.parametrize('change',[
    lambda r:r.pop(),
    lambda r:r.append(deepcopy(r[0])),
    lambda r:r[0].update(part_id='foreign'),
    lambda r:r[0].update(payload_sha256='0'*64),
    lambda r:r[0]['response'].update(model='wrong'),
    lambda r:r[0]['response'].update(usage_known=False),
    lambda r:r[0]['response'].update(provider_usage_known=False),
    lambda r:r[0]['response']['usage'].update(input_tokens=-1),
    lambda r:r[0]['response']['usage'].update(output_tokens=True),
    lambda r:r[0]['response'].pop('usage'),
    lambda r:r[0]['response']['answers'].pop(next(iter(r[0]['response']['answers']))),
    lambda r:r[0]['response']['answers'].update(foreign={}),
    lambda r:next(iter(r[0]['response']['answers'].values())).update(confidence=float('nan')),
])
def test_missing_repeated_foreign_malformed_or_uncertain_parts_never_yield_an_aggregate(change):
    p=payload();plan=plan_for(p);responses=responses_for(plan);change(responses)
    with pytest.raises(ValueError):aggregate_jev_parts(p,plan,responses)


@pytest.mark.parametrize('change',[
    lambda p:p['parts'][0]['payload']['state']['profile']['documents'].pop(),
    lambda p:p['parts'][0]['payload']['state']['requirements'][0].update(label='changed'),
    lambda p:p['parts'][0]['payload']['questions']['r0001_status'].update(instructions='Evaluate state.requirements[9] only.'),
    lambda p:p['parts'][0]['question_mapping']['r0001_status'].update(local_requirement_index=99),
    lambda p:p['parts'][0]['token_budget'].update(estimated_request_tokens=-1),
    lambda p:p.update(truncated=True),
])
def test_semantic_plan_tampering_fails_even_with_rehashed_plan(change):
    source=payload();plan=plan_for(source);responses=responses_for(plan);change(plan)
    plan['parts'][0]['payload_sha256']=digest(plan['parts'][0]['payload'])
    responses[0]['payload_sha256']=plan['parts'][0]['payload_sha256']
    plan['plan_sha256']=digest({k:v for k,v in plan.items() if k!='plan_sha256'})
    with pytest.raises(ValueError):aggregate_jev_parts(source,plan,responses)


def test_exact_counter_must_be_reused_for_aggregate_budget_verification():
    p=payload();counter=lambda text:len(text)//4
    plan=plan_for(p,token_counter=counter);responses=responses_for(plan)
    assert aggregate_jev_parts(p,plan,responses,token_counter=counter)['proof']['usage_known']
    with pytest.raises(ValueError,match='budget changed'):aggregate_jev_parts(p,plan,responses)


def test_duplicate_part_identity_with_otherwise_complete_response_count_is_rejected():
    p=large();plan=plan_for(p);responses=responses_for(plan)
    assert len(responses)>1
    responses[-1]=deepcopy(responses[0])
    with pytest.raises(ValueError,match='repeated partition'):aggregate_jev_parts(p,plan,responses)


def test_rehashed_oversized_single_part_with_null_budget_cannot_evade_hard_context_guard():
    p=large();plan=plan_for(p);mapping={}
    for part in plan['parts']:
        for qid,entry in part['question_mapping'].items():
            mapping[qid]={'original_requirement_index':entry['original_requirement_index'],
                'local_requirement_index':entry['original_requirement_index']}
    plan['parts']=[{'part_id':'part-001','payload':deepcopy(p),'payload_sha256':digest(p),
        'requirement_indices':list(range(91)),'question_mapping':mapping,'token_budget':None}]
    plan['plan_sha256']=digest({k:v for k,v in plan.items() if k!='plan_sha256'})
    with pytest.raises(ValueError,match='budget changed'):
        aggregate_jev_parts(p,plan,responses_for(plan))
