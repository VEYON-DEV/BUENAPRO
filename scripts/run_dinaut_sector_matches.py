"""Selected-ID DIN matching: immutable freeze, offline prepare, explicit execute.

No ingestion, PDF download, queue activation or notification. New matches only.
Scoring reuses JEV/rules; compact explanations use Gemini 3.1 Flash-Lite.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
sys.path.insert(0,str(ROOT/"workers/seace/src"))

from dinaut_batch_scope import SECTOR_SCOPE, iso_snapshot, scoped_source_sql
from dinaut_sector_match_scope import (
    DOCUMENT_SQL, SectorProviderControl, active_provider_control, read_selection,
    sector_folder, sector_disposition, validate_document_ownership,
    validate_frozen_sector_inputs, reader_evidence, LIFECYCLE_POLICY, MAX_LIFECYCLE_AGE_SECONDS,
)
import run_dinaut_three_dot_matches as run
import run_dinaut_three_dot_gemma_batches as batches


def immutable_bytes(path: Path, raw: bytes):
    if path.exists():
        if path.read_bytes()!=raw:raise run.PublicationConflict("Immutable selection bytes changed")
        return
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),"wb") as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    descriptor=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def selection_report(selection, digest):
    return {"scope":SECTOR_SCOPE.name,"selection_sha256":digest,
        "selected":len(selection["cases"]),"sources":dict(Counter(c["source_key"] for c in selection["cases"])),
        "sector_memberships":dict(Counter(s for c in selection["cases"] for s in c["sector_categories"])),
        "fit_level_restriction":None,"new_only":True,"inference_calls":0}


@contextmanager
def locked_folder(folder: Path):
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (folder/"coordinator.lock").open("a+") as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def freeze_with_connection(conn,selection,digest,raw,folder):
    """Caller owns a repeatable-read, read-only transaction. No paid keys used."""
    immutable_bytes(folder/"selection.private.json",raw)
    if (folder/"inputs.private.json").exists():
        frozen=json.loads((folder/"inputs.private.json").read_text())
        receipt=json.loads((folder/"preflight.private.json").read_text())
        validate_frozen_sector_inputs(frozen,receipt,folder)
        if receipt["selection_sha256"]!=digest:raise run.PublicationConflict("Different selection in batch folder")
        return receipt
    _,reader_raw=reader_evidence(selection)
    immutable_bytes(folder/"reader-evidence.private.json",reader_raw)
    profile=conn.execute("SELECT * FROM company_profiles WHERE id=%s AND tenant_id=%s AND is_active",
        (run.PROFILE_ID,run.TENANT_ID)).fetchone()
    if not profile:raise run.ProviderBlocked("Active DIN tenant/profile unavailable")
    lines=conn.execute("SELECT * FROM business_lines WHERE profile_id=%s AND is_active ORDER BY id",
        (run.PROFILE_ID,)).fetchall()
    if not lines:raise run.ProviderBlocked("DIN has no active business lines")
    documents=iso_snapshot(conn.execute(DOCUMENT_SQL,(run.TENANT_ID,run.PROFILE_ID)).fetchall())
    profile=iso_snapshot(profile);lines=iso_snapshot(lines)
    validate_document_ownership(profile,documents)
    cases=[]
    cutoff=datetime.now(timezone.utc)
    for proof in selection["cases"]:
        live=run.source_reader(conn,proof)
        if live is None:live={"source_key":proof["source_key"],"source_id":proof["source_id"]}
        live={**live,"selection":proof}
        ready,reason=sector_disposition(live,now=cutoff)
        cases.append({**live,"ready":ready,"readiness_reason":reason})
    receipt={"scope":SECTOR_SCOPE.name,"profile_id":run.PROFILE_ID,"tenant_id":run.TENANT_ID,
        "selection_sha256":digest,"cutoff_utc":cutoff.isoformat(),
        "lifecycle_policy":LIFECYCLE_POLICY,"max_lifecycle_age_seconds":MAX_LIFECYCLE_AGE_SECONDS,
        "selector_sql_sha256":{source:hashlib.sha256(scoped_source_sql(
            run.prod2_sql() if source=="seace_prod2" else run.PROD6_SQL,SECTOR_SCOPE).encode()).hexdigest()
            for source in SECTOR_SCOPE.allowed_sources},
        "selected":len(cases),"ready":sum(c["ready"] for c in cases),
        "dispositions":dict(Counter(c["readiness_reason"] for c in cases)),
        "company_documents_sha256":run.fingerprint(documents),"manual_scope_does_not_enable_ingestion":True,
        "inference_calls":0}
    frozen={"profile":profile,"business_lines":lines,"company_documents":documents,
        "cases":cases,"receipt":receipt}
    run.once_json(folder/"inputs.private.json",frozen)
    outer={**receipt,"inputs_sha256":hashlib.sha256((folder/"inputs.private.json").read_bytes()).hexdigest()}
    run.once_json(folder/"preflight.private.json",outer)
    validate_frozen_sector_inputs(frozen,outer,folder)
    return outer


def owner_checker(conn):
    def check():
        row=conn.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks
            WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted
              AND classid::bigint=(hashtext('dinaut_three_dot_llm2')::bigint & 4294967295)
              AND objid::bigint=(hashtext(%s)::bigint & 4294967295)
              AND objsubid=2) AS locked""",(run.PROFILE_ID,)).fetchone()
        if not row or row["locked"] is not True:raise run.ProviderBlocked("DIN coordinator lock ownership lost")
    return check


def committed_runtime():
    paths=sorted(set(run.scoped_runtime_paths()+[
        "scripts/run_dinaut_sector_matches.py","scripts/dinaut_sector_match_scope.py",
        "scripts/run_dinaut_three_dot_matches.py","scripts/run_dinaut_three_dot_gemma_batches.py",
        "scripts/dinaut_three_dot_persistence.py","scripts/dinaut_batch_scope.py"]))
    for path in paths:
        if subprocess.run(["git","ls-files","--error-unmatch",path],cwd=ROOT,capture_output=True).returncode:
            raise run.ProviderBlocked("Commit the selected matching runtime before execution")
    if subprocess.run(["git","diff","--quiet","HEAD","--",*paths],cwd=ROOT).returncode:
        raise run.ProviderBlocked("Operational matching code differs from Git HEAD")
    return {"commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "runtime_sha256":{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}}


@contextmanager
def batch_environment(folder,frozen):
    previous=(batches.FOLDER,batches.INPUT_BUDGET,batches.frozen_global)
    batches.FOLDER=folder;batches.INPUT_BUDGET=30000;batches.frozen_global=frozen
    try:yield
    finally:batches.FOLDER,batches.INPUT_BUDGET,batches.frozen_global=previous


@contextmanager
def graceful_provider_stop(control):
    """SIGTERM/SIGINT hold new sends; in-flight threads retain their receipts."""
    previous={}
    if threading.current_thread() is threading.main_thread():
        def stop(_signal,_frame):
            control.fail("operator_stop")
        for name in (signal.SIGTERM,signal.SIGINT):
            previous[name]=signal.getsignal(name)
            signal.signal(name,stop)
    try:yield
    finally:
        for name,handler in previous.items():signal.signal(name,handler)


def parallel_stage(work,values,workers,control):
    """Wait for in-flight work to preserve replies, then report the first error."""
    results={};first_error=None
    def guarded(value):
        if control.stop.is_set() and not control.replay_only:
            raise run.ProviderBlocked("Sector batch held before starting another case")
        try:return work(value)
        except Exception:
            control.fail("matching_stage_failed")
            raise
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="din-sector") as pool:
        futures={pool.submit(guarded,value):index for index,value in enumerate(values)}
        for future in as_completed(futures):
            try:results[futures[future]]=future.result()
            except Exception as exc:
                if first_error is None:first_error=exc
    if first_error is not None:raise first_error
    return [results[index] for index in range(len(values))]


def groups_for_contexts(contexts):
    pending=list(contexts);groups=[]
    while pending:
        size=min(2,len(pending))
        if size==2 and sum(len(contexts[k]["evaluation"]["requirements"]) for k in pending[:2])>16:size=1
        groups.append(pending[:size]);pending=pending[size:]
    return groups


def execute_with_settings(settings,frozen,cards,prepared,folder,jev_key,*,batch_size,max_batches,
        model_workers,replay_only,code):
    """Only function that writes matching PG state or dispatches providers."""
    if not 1<=batch_size<=50 or not 1<=max_batches<=5000 or model_workers not in {1,2}:
        raise ValueError("Finite batch size, count and one/two model slots required")
    with run.psycopg.connect(settings.database_url,row_factory=run.dict_row,autocommit=True) as owner:
        if not owner.execute("SELECT pg_try_advisory_lock(hashtext('dinaut_three_dot_llm2'),hashtext(%s)) AS locked",
                (run.PROFILE_ID,)).fetchone()["locked"]:
            raise run.ProviderBlocked("Another DIN matching coordinator owns the profile")
        control=SectorProviderControl(folder,check_owner=owner_checker(owner),slots=model_workers,replay_only=replay_only)
        if control.stop.is_set() and not replay_only:raise run.ProviderBlocked("Saved provider hold requires evidence review; no automatic retry")
        run.private_json(folder/"run-code.private.json",{**code,"started_utc":datetime.now(timezone.utc).isoformat(),
            "recommendation_model":run.GEMMA_MODEL,"models_max_in_flight":model_workers,
            "no_services_changed":True,"replay_only":replay_only})
        items={batches.key(item):item for item in prepared};results={}
        def connection():return run.psycopg.connect(settings.database_url,row_factory=run.dict_row,autocommit=True)
        with active_provider_control(control),batch_environment(folder,frozen),graceful_provider_stop(control):
            # Rebind responses saved just before an interruption; no provider I/O.
            for response in sorted((folder/"gemma-batches").glob("*/gemma-response.private.json")):
                plan=json.loads(response.with_name("plan.private.json").read_text())
                if not set(plan["case_keys"])<=set(items):raise run.ProviderBlocked("Foreign saved batch in sector folder")
                batches.bind_saved_batch(response.parent,items)
            for item in prepared:
                key=batches.key(item)
                if (folder/key/"published.private.json").exists():
                    with connection() as conn:results[key]=run.run_case(conn,settings,frozen,cards,item,folder,jev_key)
            selected=[item for item in prepared if batches.key(item) not in results][:batch_size*max_batches]
            def scoring(item):
                key=batches.key(item);case_folder=folder/key
                with connection() as conn:
                    if (case_folder/"published.private.json").exists() or (case_folder/"gemma-batch-binding.private.json").exists():
                        result=run.run_case(conn,settings,frozen,cards,item,folder,jev_key)
                        return key,None,result
                    context=run.cached_scoring_context(frozen,cards,item,folder)
                    if context is None:
                        if replay_only:raise run.ProviderBlocked("Local recovery has no exact cached JEV response")
                        run.run_case(conn,settings,frozen,cards,item,folder,jev_key,jev_only=True)
                        scoring=json.loads((case_folder/"scoring.private.json").read_text())
                        context=run.explanation_context(item["jev_payload"],scoring,cards,item["case"])
                    return key,context,None
            def explain(group,contexts):
                with connection() as conn:
                    digest=run.fingerprint(batches.batch_request({k:contexts[k] for k in group}))
                    success,_=batches.execute_batch(conn,settings,folder/"gemma-batches"/("batch-"+digest[:16]),group,contexts,items)
                    if not success:
                        if len(group)==1:raise run.ProviderBlocked("Complete single-case context exceeds token budget; no generation sent")
                        for key in group:explain([key],contexts)
                return group
            try:
                for start in range(0,len(selected),batch_size):
                    contexts={}
                    for key,context,result in parallel_stage(scoring,selected[start:start+batch_size],model_workers,control):
                        if result is not None:results[key]=result
                        else:contexts[key]=context
                    parallel_stage(lambda group:explain(group,contexts),groups_for_contexts(contexts),model_workers,control)
                    # Publication is local replay + transactional CAS; no second inference.
                    for key in contexts:
                        with connection() as conn:results[key]=run.run_case(conn,settings,frozen,cards,items[key],folder,jev_key)
                    if control.stop.is_set() and not replay_only:
                        raise run.ProviderBlocked("Sector batch stopped after preserving in-flight work")
                    progress=progress_report(results,prepared,frozen,code,folder=folder)
                    run.private_json(folder/"progress.private.json",progress)
                    run.emit({"status":"batch_complete","published":len(results),"target":len(prepared),
                        "complete":progress["complete"],"known_cost_usd":progress["known_cost_usd"]})
            except Exception as exc:
                control.fail("matching_stage_failed")
                progress=progress_report(results,prepared,frozen,code,folder=folder)
                progress.update(held=True,error_class=type(exc).__name__,
                    cost_uncertain=progress.get("uncertain_provider_attempts",0)>0)
                run.private_json(folder/"progress.private.json",progress)
                raise
            final=progress_report(results,prepared,frozen,code,folder=folder)
            run.private_json(folder/"progress.private.json",final)
            return final


def provider_costs(folder,prepared):
    """Count original durable responses once, including unpublished paid work."""
    from decimal import Decimal
    total=Decimal(0);uncertain=0;responses=0
    for item in prepared:
        case_folder=folder/batches.key(item)
        try:receipt=run.get_cached_jev_receipt(case_folder,item["jev_payload"])
        except (run.ProviderBlocked,ValueError,OSError):receipt=None
        if receipt is not None:
            usage=receipt.get("body",{}).get("usage") or {}
            tokens=usage.get("input_tokens")
            if type(tokens) is int and tokens>=0:
                total+=Decimal(tokens)*Decimal(".042")/Decimal(1_000_000);responses+=1
            else:uncertain+=1
        else:
            uncertain+=sum(1 for _ in case_folder.glob("**/jev-started.private.json"))
    for started in (folder/"gemma-batches").glob("*/gemma-started.private.json"):
        path=started.with_name("gemma-response.private.json")
        receipt=json.loads(path.read_text()) if path.exists() else {}
        usage=receipt.get("body",{}).get("usageMetadata") or {}
        if (receipt.get("http_status")==200 and all(type(usage.get(k)) is int and usage[k]>=0
                for k in ("promptTokenCount","candidatesTokenCount"))
                and type(usage.get("thoughtsTokenCount",0)) is int and usage.get("thoughtsTokenCount",0)>=0):
            output=usage["candidatesTokenCount"]+usage.get("thoughtsTokenCount",0)
            total+=(Decimal(usage["promptTokenCount"])*Decimal(".25")+Decimal(output)*Decimal("1.50"))/Decimal(1_000_000)
            responses+=1
        else:uncertain+=1
    return {"known_cost_usd":str(total),"uncertain_provider_attempts":uncertain,
        "provider_responses_with_known_usage":responses,"provider_bill_verified":False}


def progress_report(results,prepared,frozen,code,*,folder=None):
    from decimal import Decimal
    result={"scope":SECTOR_SCOPE.name,"commit":code["commit"],"results":list(results.values()),
        "target":len(prepared),"selected":len(frozen["cases"]),"excluded":sum(not c["ready"] for c in frozen["cases"]),
        "complete":len(results)==len(prepared),"known_cost_usd":str(sum(
            (Decimal(r.get("cost_usd","0")) for r in results.values()),Decimal(0))),
        "source_review_preserved":True,"notifications":0,"held":False}
    if folder is not None:result.update(provider_costs(folder,prepared))
    return result


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument("command",choices=["scope","freeze","prepare","execute"])
    result.add_argument("--selection",type=Path,required=True)
    result.add_argument("--expected-sha256",required=True)
    result.add_argument("--folder",type=Path,required=True)
    result.add_argument("--batch-size",type=int,default=10)
    result.add_argument("--max-batches",type=int,default=1)
    result.add_argument("--model-workers",type=int,choices=[1,2],default=2)
    result.add_argument("--go",action="store_true")
    result.add_argument("--recover-local",action="store_true",help="Exact cached responses only; never clears a provider hold")
    result.add_argument("--ssh-host",default="ubuntu@168.129.177.199")
    result.add_argument("--ssh-key",type=Path,default=ROOT/"credentials/ssh-key-2026-07-14.key")
    return result


def main(argv=None):
    args=parser().parse_args(argv)
    selection,digest,raw=read_selection(args.selection,args.expected_sha256)
    folder=sector_folder(args.folder,ROOT)
    if args.command=="scope":
        result=selection_report(selection,digest);run.emit(result);return result
    if args.command=="execute" and not args.go:raise ValueError("Execution requires explicit --go")
    with locked_folder(folder),run.execution_scope(SECTOR_SCOPE,folder=folder):
        if args.command=="freeze":
            with run.production_settings(ssh_host=args.ssh_host,ssh_key=args.ssh_key) as settings:
                with run.psycopg.connect(settings.database_url,row_factory=run.dict_row,autocommit=True) as conn:
                    with conn.transaction():
                        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        result=freeze_with_connection(conn,selection,digest,raw,folder)
            run.emit({**selection_report(selection,digest),"status":"frozen","ready":result["ready"],
                "dispositions":result["dispositions"]});return result
        frozen,cards,prepared,manifest=run.prepare(folder)
        if frozen["receipt"]["selection_sha256"]!=digest:raise ValueError("CLI selection differs from frozen batch")
        report={**selection_report(selection,digest),"status":"prepared","ready":len(prepared),
            "excluded":len(frozen["cases"])-len(prepared),"documents":len(frozen["company_documents"])}
        if args.command=="prepare":run.emit(report);return report
        code=committed_runtime()
        jev_key=run.dotenv_values(ROOT/".env.jev.local").get("TYPESAFE_API_KEY")
        if not jev_key:raise run.ProviderBlocked("Existing JEV credential unavailable")
        with run.production_settings(ssh_host=args.ssh_host,ssh_key=args.ssh_key) as settings:
            result=execute_with_settings(settings,frozen,cards,prepared,folder,jev_key,
                batch_size=args.batch_size,max_batches=args.max_batches,model_workers=args.model_workers,
                replay_only=args.recover_local,code=code)
        run.emit({"status":"complete" if result["complete"] else "batch_limit_reached",
            "published":len(result["results"]),"target":result["target"],"known_cost_usd":result["known_cost_usd"]})
        return result


if __name__=="__main__":
    try:main()
    except Exception as exc:
        run.emit({"status":"blocked","error_class":type(exc).__name__,"no_automatic_retry":True})
        raise SystemExit(2)
