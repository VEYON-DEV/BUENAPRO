"""Durable Jev decisions before canonical promotion. No provider/network calls.

Commit claims before paid I/O. The exact request hash includes description,
commercial context, model and policy. Multiple convocations can reuse that
semantic response; only accepted candidates get canonical metadata/evaluations.
Membership proves a pilot selection, never completion of a portal query/page.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row

from buenapro_worker.prod2.analysis import jev_request_hash
from buenapro_worker.prod2.batch_repository import _object
from buenapro_worker.prod2.discovery import _hash, _identity, _json, validate_capture, LIMA, ROW_KEYS
from buenapro_worker.prod2.repository import _id, _sha, _uuid, _text, _aware, hash_request

ACCEPTANCE_THRESHOLD = 60.0


class AcceptedPersistenceError(ValueError):
    """Safe input/ownership/provenance failure without response bodies or secrets."""


def _one(cursor, statement, params=()):
    cursor.execute(statement, params)
    return cursor.fetchone()


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", value):
        raise AcceptedPersistenceError("Invalid selection run identifier")
    return value


def _result(value, *, model, policy_version, request_hash):
    result = _object(value)
    affinity = result.get("affinity_index")
    if (isinstance(affinity, bool) or not isinstance(affinity, (float, int))
            or not math.isfinite(affinity) or not 0<=affinity<=100):
        raise AcceptedPersistenceError("Jev affinity must be a finite index from zero to one hundred")
    if result.get("model")!=model or result.get("policy_version")!=policy_version:
        raise AcceptedPersistenceError("Jev result model/policy differs from its durable request")
    expected = "prod2:jev:" + request_hash
    if result.get("cache_key", expected)!=expected or result.get("request_hash",request_hash)!=request_hash:
        raise AcceptedPersistenceError("Jev result does not match its exact request hash")
    if "profile_id" in result:
        result["profile_id"]=_uuid(result["profile_id"])
    context=result.get("request_context")
    if context is not None:
        if not isinstance(context,dict) or set(context)!={"description","commercial_profiles"}:
            raise AcceptedPersistenceError("Invalid Jev request context")
        if jev_request_hash(context["description"],commercial_profiles=context["commercial_profiles"],model=model)!=request_hash:
            raise AcceptedPersistenceError("Jev context does not hash to the durable request")
    for key in ("input_tokens","output_tokens"):
        if key in result and (type(result[key]) is not int or not 0<=result[key]<=2**31-1):
            raise AcceptedPersistenceError("Invalid Jev token usage")
    for key in ("cost_usd","incurred_cost_usd"):
        if key in result and (isinstance(result[key],bool) or not isinstance(result[key],(float,int))
                or not math.isfinite(result[key]) or not 0<=result[key]<10000):
            raise AcceptedPersistenceError("Invalid Jev recorded cost")
    result.update(request_hash=request_hash)
    return result


def _same_result(left, right):
    """Context/profile metadata may be attached to a verified legacy response."""
    extra={"profile_id","request_context"}
    if {k:v for k,v in left.items() if k not in extra}!={k:v for k,v in right.items() if k not in extra}:
        return False
    return all(left[k]==right[k] for k in extra if k in left and k in right)


def claim_jev(conn: Any, *, convocation_id: int, request_hash: str, model: str, policy_version: str) -> dict:
    """Claim a previously unissued request; blocked states never create a payment."""
    conv, digest = _id(convocation_id),_sha(request_hash)
    model,policy = _text(model,limit=200),_text(policy_version,limit=200)
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_jev_decision_cache'),hashtext(%s))",(digest,))
        cached=_one(cursor,"SELECT *, lease_until<=clock_timestamp() AS expired FROM prod2_jev_decision_cache WHERE cache_key=%s FOR UPDATE",(digest,))
        if cached:
            if (cached["model"],cached["policy_version"])!=(model,policy):
                raise AcceptedPersistenceError("Request hash already binds another model/policy")
            if cached["status"]=="done":
                result=_result(cached["result_json"],model=model,policy_version=policy,request_hash=digest)
                return {"state":"cached","status":"cached","request_hash":digest,"result":result}
            if cached["status"]=="running" and cached["expired"]:
                cursor.execute("""UPDATE prod2_jev_decision_cache SET status='uncertain',
                    blocked_reason='expired_provider_claim',updated_at=now() WHERE cache_key=%s""",(digest,))
            return {"state":"blocked","status":"blocked","request_hash":digest,
                    "reason":"provider_running" if cached["status"]=="running" and not cached["expired"] else "provider_uncertain"}
        # Preserve prior billing truth from the original canonical ledger.
        cursor.execute("""SELECT * FROM prod2_evaluations WHERE kind='jev_affinity' AND request_hash=%s
            AND model=%s AND policy_version=%s ORDER BY (status='done') DESC,id DESC FOR UPDATE""",(digest,model,policy))
        prior=cursor.fetchall()
        for ev in prior:
            if ev["status"]=="done":
                result=_result(ev["result_json"],model=model,policy_version=policy,request_hash=digest)
                cursor.execute("""INSERT INTO prod2_jev_decision_cache(cache_key,convocation_id,model,policy_version,
                    status,completed_at,result_json,reused_evaluation_id) VALUES (%s,%s,%s,%s,'done',%s,%s::jsonb,%s)""",
                    (digest,conv,model,policy,ev["completed_at"],_json(result),ev["id"]))
                return {"state":"cached","status":"cached","request_hash":digest,"result":result,"reused_evaluation_id":ev["id"]}
        if prior:
            cursor.execute("""INSERT INTO prod2_jev_decision_cache(cache_key,convocation_id,model,policy_version,
                status,blocked_reason,reused_evaluation_id) VALUES (%s,%s,%s,%s,'uncertain','legacy_request_not_done',%s)""",
                (digest,conv,model,policy,prior[0]["id"]))
            return {"state":"blocked","status":"blocked","request_hash":digest,"reason":"legacy_request_not_done"}
        token=str(uuid4())
        cursor.execute("""INSERT INTO prod2_jev_decision_cache(cache_key,convocation_id,model,policy_version,
            status,claim_token,claimed_at,lease_until) VALUES (%s,%s,%s,%s,'running',%s,
            clock_timestamp(),clock_timestamp()+interval '10 minutes')""",(digest,conv,model,policy,token))
    return {"state":"claimed","status":"claimed","request_hash":digest,"claim_token":token}


def complete_jev(conn: Any, *, request_hash: str, claim_token: str, result: dict) -> dict:
    """Only the original owner can resolve a running or uncertain paid request."""
    digest,token=_sha(request_hash),_uuid(claim_token)
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cursor:
        row=_one(cursor,"SELECT * FROM prod2_jev_decision_cache WHERE cache_key=%s FOR UPDATE",(digest,))
        if not row or str(row["claim_token"])!=token:
            raise AcceptedPersistenceError("Jev request is not owned by this claim")
        normalized=_result(result,model=row["model"],policy_version=row["policy_version"],request_hash=digest)
        if row["status"]=="done":
            if row["result_json"]!=normalized:
                raise AcceptedPersistenceError("Completed Jev result is immutable")
            return {"state":"done","request_hash":digest,"result":normalized,"cached":True}
        if row["status"] not in {"running","uncertain"}:
            raise AcceptedPersistenceError("Jev request cannot be completed from this state")
        cursor.execute("""UPDATE prod2_jev_decision_cache SET status='done',result_json=%s::jsonb,
            completed_at=clock_timestamp(),blocked_reason=NULL,updated_at=now() WHERE cache_key=%s""",(_json(normalized),digest))
    return {"state":"done","request_hash":digest,"result":normalized,"cached":False}


def _done(cursor, digest, result):
    row=_one(cursor,"SELECT * FROM prod2_jev_decision_cache WHERE cache_key=%s FOR UPDATE",(digest,))
    if not row or row["status"]!="done":
        raise AcceptedPersistenceError("A durable completed Jev decision is required")
    normalized=_result(result,model=row["model"],policy_version=row["policy_version"],request_hash=digest)
    if not _same_result(normalized,row["result_json"]):
        raise AcceptedPersistenceError("Supplied Jev decision differs from its durable cache")
    return normalized


def _member(cursor, run_id, conv, digest, accepted, result):
    if type(accepted) is not bool or accepted!=(result["affinity_index"]>=ACCEPTANCE_THRESHOLD):
        raise AcceptedPersistenceError("Accepted selection must use the fixed Jev threshold")
    prior=_one(cursor,"SELECT * FROM prod2_accepted_run_members WHERE run_id=%s AND convocation_id=%s FOR UPDATE",(run_id,conv))
    if prior and (prior["request_hash"],prior["accepted"],prior["result_json"])!=(digest,accepted,result):
        raise AcceptedPersistenceError("Selection membership is immutable; use a new run for changed inputs")
    cursor.execute("""INSERT INTO prod2_accepted_run_members(run_id,convocation_id,request_hash,accepted,result_json)
        VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(run_id,convocation_id) DO NOTHING""",(run_id,conv,digest,accepted,_json(result)))


def record_run_member(conn: Any, *, run_id: str, convocation_id: int, request_hash: str, accepted: bool, result: dict) -> dict:
    run,conv,digest=_run_id(run_id),_id(convocation_id),_sha(request_hash)
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_accepted_run'),hashtext(%s))",(run,))
        normalized=_done(cursor,digest,result)
        _member(cursor,run,conv,digest,accepted,normalized)
    return {"run_id":run,"convocation_id":conv,"accepted":accepted,"request_hash":digest}


def _capture_row(row, query, observed, allow_empty_keyword=False):
    # Accept either raw discovery input or an already validated Capture.rows row.
    value=_object(row)
    if set(value)==ROW_KEYS | {"object_type"}:
        published=datetime.fromisoformat(value["published_at"])
        if published.tzinfo is None or value["object_type"]!=query["object_type"]:
            raise AcceptedPersistenceError("Invalid normalized discovery row")
        value={key:val for key,val in value.items() if key!="object_type"}
        value["published_at"]=published.astimezone(LIMA).strftime("%d/%m/%Y %H:%M")
        value["reference_amount"]=value["reference_amount"] if value["reference_amount"] is not None else "---"
    return validate_capture({"query":query,"observed_at":observed.isoformat(),
        "page":{"number":1,"page_size":1,"total_pages":1,"total_results":1},"rows":[value]},
        allow_empty_keyword=allow_empty_keyword)


def promote_accepted(conn: Any, *, run_id: str, row: dict, query_manifest: dict,
                     observed_at: datetime, jev_result: dict, allow_empty_keyword: bool = False) -> dict:
    """Promote one accepted selection; the validation envelope is never a page.

    ``allow_empty_keyword`` is only for a candidate found by the national daily listing.
    """
    run,observed=_run_id(run_id),_aware(observed_at)
    capture=_capture_row(row,query_manifest,observed,allow_empty_keyword)
    canonical=capture.rows[0]
    digest=_sha(jev_result.get("request_hash") or str(jev_result.get("cache_key", "" )).removeprefix("prod2:jev:"))
    conv,procedure=canonical["convocation_id"],canonical["procedure_id"]
    with conn.transaction(),conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_accepted_run'),hashtext(%s))",(run,))
        result=_done(cursor,digest,jev_result)
        if result["affinity_index"]<ACCEPTANCE_THRESHOLD:
            raise AcceptedPersistenceError("Jev affinity below the acceptance threshold")
        context=result.get("request_context")
        if not context or " ".join(context["description"].split())!=canonical["description"]:
            raise AcceptedPersistenceError("Accepted decision does not bind this row description")
        _member(cursor,run,conv,digest,True,result)
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('seace_prod4'),hashtext(%s))",(str(procedure),))
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('seace_prod2_convocation'),hashtext(%s))",(str(conv),))
        opportunity,created=_identity(cursor,canonical,observed)
        cursor.execute("""INSERT INTO prod2_discoveries(id_convocatoria,id_procedimiento,opportunity_id,nomenclature,
            buyer_name,description,object_type,object_label,published_at,reference_amount,currency,seace_version,
            row_hash,first_seen_at,last_seen_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id_convocatoria) DO UPDATE SET nomenclature=EXCLUDED.nomenclature,buyer_name=EXCLUDED.buyer_name,
            description=EXCLUDED.description,published_at=EXCLUDED.published_at,reference_amount=EXCLUDED.reference_amount,
            currency=EXCLUDED.currency,row_hash=EXCLUDED.row_hash,last_seen_at=EXCLUDED.last_seen_at""",
            (conv,procedure,opportunity,canonical["nomenclature"],canonical["buyer_name"],canonical["description"],
             canonical["object_type"],canonical["object_label"],canonical["published_at"],canonical["reference_amount"],
             canonical["currency"],canonical["seace_version"],_hash(canonical),observed,observed))
        profile=result.get("profile_id")
        key=hash_request({"convocation":conv,"kind":"jev_affinity","request_hash":digest,
            "model":result["model"],"policy_version":result["policy_version"],"profile_id":profile})
        cursor.execute("""INSERT INTO prod2_evaluations(evaluation_key,id_convocatoria,kind,request_hash,model,policy_version,
            profile_id,status,completed_at,result_json,input_tokens,output_tokens,cost_usd)
            VALUES (%s,%s,'jev_affinity',%s,%s,%s,%s,'done',now(),%s::jsonb,%s,%s,%s)
            ON CONFLICT(evaluation_key) DO NOTHING""",(key,conv,digest,result["model"],result["policy_version"],profile,
                _json(result),0,0,0))
        evaluation=_one(cursor,"SELECT * FROM prod2_evaluations WHERE evaluation_key=%s FOR UPDATE",(key,))
        if not evaluation or evaluation["status"]!="done":
            raise AcceptedPersistenceError("Canonical Jev ledger contains an unresolved claim")
        existing=_result(evaluation["result_json"],model=result["model"],policy_version=result["policy_version"],request_hash=digest)
        if not _same_result(existing,result):
            raise AcceptedPersistenceError("Canonical Jev ledger disagrees with the durable decision")
        cursor.execute("""UPDATE prod2_accepted_run_members SET opportunity_id=%s,query_manifest=%s::jsonb,
            row_manifest=%s::jsonb,observed_at=%s,updated_at=now() WHERE run_id=%s AND convocation_id=%s""",
            (opportunity,_json(capture.query),_json(canonical),observed,run,conv))
    return {"opportunity_id":str(opportunity),"id_convocatoria":conv,"evaluation_id":int(evaluation["id"]),
            "accepted":True,"created":created,"run_id":run}
