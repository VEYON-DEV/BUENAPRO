"""Selected DIN sector batch contracts; no network, database or model I/O."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import signal
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/"scripts"))
import dinaut_batch_scope as scopes
import dinaut_sector_match_scope as sector
import dinaut_three_dot_persistence as persistence
import run_dinaut_sector_matches as entry
import run_dinaut_three_dot_matches as run
from match_provider_journal import fingerprint,private_json,dispatch_once,ProviderBlocked


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    import requests
    monkeypatch.setattr(requests,"post",lambda *a,**k:pytest.fail("No real HTTP allowed"))
    monkeypatch.setattr(run.psycopg,"connect",lambda *a,**k:pytest.fail("No real PG allowed"))
    monkeypatch.setattr(run,"production_settings",lambda *a,**k:pytest.fail("No real SSH allowed"))


def proof(identity=19,source="seace_prod2",at=None):
    at=at or datetime.now(timezone.utc)
    return {"source_key":source,"source_id":identity,"sector_categories":["industrial"],
        "selection_reason":"Objeto industrial explícito en el mapeo congelado",
        "lifecycle":{"confirmed_open":True,"temporal_state":"submission_open",
            "temporal_observed_at":at.isoformat(),"requires_fresh_lifecycle":False,
            "matching_eligible":True,"closes_at":(at+timedelta(days=3)).isoformat()}}


def sample(identity=19,source="seace_prod2",at=None):
    selected=proof(identity,source,at)
    return {"source_key":source,"source_id":identity,"opportunity_id":f"opp-{identity}",
        "fit_level":1,"code":"EXAMPLE","description":"Tableros eléctricos","buyer_name":"Entidad",
        "extraction_id":27,"document_sha256":"a"*64,"is_current":True,"quality":"auto",
        "requires_human_review":True,"raw_extraction_json":{"read":True},"summary_json":{"object":"Tableros"},
        "existing_match_id":None,"prior_match_reservations":[],"prior_worker_jobs":[],"prior_provider_events":[],
        "selection":selected,"closes_at":selected["lifecycle"]["closes_at"],
        "facets":[{"extraction_id":27,"is_current":True,"facet":"specific_experience",
            "label":"Tableros","required":True,"details_json":{}}]}


def selection(cases=None):
    return {"version":sector.VERSION,"profile_id":run.PROFILE_ID,"tenant_id":run.TENANT_ID,
        "cases":cases or [proof()]}


def evidence_files(tmp_path,chosen):
    records=[]
    for case in chosen["cases"]:
        records.append({"convocation_id":case["source_id"],"source_key":case["source_key"],**case["lifecycle"]})
    path=tmp_path/"reader-report.private.json"
    raw=json.dumps({"records":records}).encode();path.write_bytes(raw)
    chosen["reader_evidence"]={"path":str(path),"sha256":hashlib.sha256(raw).hexdigest()}
    return raw


def test_selection_sha_exact_and_no_provider_configuration_needed(tmp_path):
    chosen=selection();raw=json.dumps(chosen).encode();path=tmp_path/"selected.json";path.write_bytes(raw)
    actual,digest,bytes_=sector.read_selection(path,hashlib.sha256(raw).hexdigest())
    assert actual==chosen and bytes_==raw
    with pytest.raises(ValueError,match="mismatch"):sector.read_selection(path,"0"*64)
    assert entry.selection_report(actual,digest)["fit_level_restriction"] is None


@pytest.mark.parametrize("change",[
    {"profile_id":"not-DIN"},{"tenant_id":"not-DIN"},{"version":"legacy"},{"cases":[]},
    {"cases":[proof(),proof()]},{"cases":[{**proof(),"source_id":True}]},
    {"cases":[{**proof(),"source_key":"prod4"}]},
    {"cases":[{**proof(),"sector_categories":["licencias_suscripciones"]}]},
    {"cases":[{**proof(),"selection_reason":" "}]},
])
def test_selection_rejects_scope_expansion_and_duplicates(change):
    with pytest.raises(ValueError):sector.validate_selection({**selection(),**change})


def test_dynamic_folders_allow_immutable_batches_but_not_old_or_arbitrary_folders(tmp_path):
    base=tmp_path/"tmp"/scopes.SECTOR_SCOPE.folder_name
    for name in ("pilot","batch-02","batch-1000"):
        assert sector.sector_folder(base/name,tmp_path)==base/name
    for path in (base/"copy",base/"nested"/"pilot",tmp_path/"tmp"/scopes.THREE_DOT_SCOPE.folder_name):
        with pytest.raises(ValueError):sector.sector_folder(path,tmp_path)
    base.mkdir(parents=True);(base/"batch-03").symlink_to(tmp_path)
    with pytest.raises(ValueError):sector.sector_folder(base/"batch-03",tmp_path)


@pytest.mark.parametrize("fit",[None,0,1,2,3])
def test_exact_sector_scope_has_no_dot_restriction_but_default_keeps_it(fit):
    row=sample();row["fit_level"]=fit
    persistence._ready(row,None,scopes.SECTOR_SCOPE.name)
    if fit!=3:
        with pytest.raises(persistence.PublicationConflict):persistence._ready(row)
    with pytest.raises(persistence.PublicationConflict):persistence._ready(row,None)
    with pytest.raises(persistence.PublicationConflict):persistence._ready(row,None,"other")


def test_sector_sql_only_removes_affinity_gate_and_keeps_tenant_current_facets():
    original=run.prod2_sql();sql=scopes.scoped_source_sql(original,scopes.SECTOR_SCOPE)
    assert "WHERE true" in sql and "cp.tenant_id=%(tenant_id)s::uuid" in sql
    assert "f.extraction_id=ex.id" in sql and "e.is_current" in sql
    assert scopes.scoped_source_sql(original,scopes.THREE_DOT_SCOPE)==original
    assert "fit.fit_level=2" in scopes.scoped_source_sql(original,scopes.TWO_DOT_50_SCOPE)


@pytest.mark.parametrize("change,reason",[
    ({"extraction_id":None},"pending_reading"),
    ({"raw_extraction_json":{}},"empty_reading"),
    ({"quality":"failed"},"failed_or_unknown_reading"),
    ({"existing_match_id":100},"existing_match_preserved"),
    ({"prior_match_reservations":[{"id":1,"status":"done"}]},"historical_matching_reservation_preserved"),
    ({"prior_worker_jobs":[{"id":1,"status":"failed"}]},"historical_matching_job_preserved"),
    ({"prior_provider_events":[{"id":1,"status":"failed"}]},"historical_provider_attempt_preserved"),
])
def test_new_only_excludes_unread_failed_and_all_historical_attempts(change,reason):
    assert sector.sector_disposition({**sample(),**change})==(False,reason)


def test_lifecycle_is_separate_and_deadline_recent_official_evidence_required():
    at=datetime.now(timezone.utc);row=sample(at=at)
    assert sector.lifecycle_disposition(row,now=at)[0]
    for key,value in (("matching_eligible",False),("confirmed_open",False),("requires_fresh_lifecycle",True)):
        changed=deepcopy(row);changed["selection"]["lifecycle"][key]=value
        assert sector.lifecycle_disposition(changed,now=at)[0] is False
    assert sector.lifecycle_disposition(row,now=at+timedelta(hours=2))[0]
    assert sector.lifecycle_disposition(row,now=at+timedelta(hours=7))==(False,"needs_fresh_lifecycle")
    changed=deepcopy(row);changed["closes_at"]=(at+timedelta(days=2)).isoformat()
    assert sector.lifecycle_disposition(changed,now=at)==(False,"official_deadline_changed")
    changed=deepcopy(row);changed["selection"]["lifecycle"]["closes_at"]=at.isoformat();changed["closes_at"]=at.isoformat()
    assert sector.lifecycle_disposition(changed,now=at)==(False,"closed_at_matching_cutoff")


def test_reader_report_sha_and_exact_lifecycle_binding(tmp_path):
    chosen=selection();raw=evidence_files(tmp_path,chosen)
    assert sector.reader_evidence(chosen)[1]==raw
    chosen["cases"][0]["lifecycle"]["closes_at"]="2030-01-01T00:00:00+00:00"
    with pytest.raises(ValueError,match="differs"):sector.reader_evidence(chosen)
    chosen["reader_evidence"]["sha256"]="0"*64
    with pytest.raises(ValueError,match="SHA"):sector.reader_evidence(chosen)


def test_company_document_ownership_and_actual_sha_must_match():
    docs=[{"id":"doc","profile_id":run.PROFILE_ID,"tenant_id":run.TENANT_ID,"content_sha256":"a"*64}]
    profile={"experience_json":[{"documentary_summary":{"sources":[{"document_id":"doc","sha256":"a"*64}]}}]}
    sector.validate_document_ownership(profile,docs)
    for changes in ({"profile_id":"VEYON"},{"tenant_id":"foreign"},{"content_sha256":"b"*64}):
        with pytest.raises(ValueError):sector.validate_document_ownership(profile,[{**docs[0],**changes}])


def test_provider_stop_rejects_new_sends_and_preserves_one_exact_cached_response(tmp_path):
    calls=[];control=sector.SectorProviderControl(tmp_path,check_owner=lambda:calls.append("owner"))
    response=SimpleNamespace(status_code=200,text='{"ok":true}')
    post=control.guarded_post(lambda *a,**k:calls.append("HTTP") or response)
    payload={"id":1}
    receipt=dispatch_once(tmp_path/"case","jev",payload,url="https://api.typesafe.ai/v1/systemone",
        api_key="test-only",header_name="Authorization",post=post)
    control.fail("uncertain")
    assert dispatch_once(tmp_path/"case","jev",payload,url="https://api.typesafe.ai/v1/systemone",
        api_key="test-only",header_name="Authorization",post=post)==receipt
    with pytest.raises(ProviderBlocked):post("url")
    assert calls==["owner","HTTP"]
    restarted=sector.SectorProviderControl(tmp_path)
    with pytest.raises(ProviderBlocked):restarted.guarded_post(lambda *a,**k:response)("url")


@pytest.mark.parametrize("status",[403,429,500])
def test_first_http_error_holds_following_requests(tmp_path,status):
    calls=[];control=sector.SectorProviderControl(tmp_path)
    post=control.guarded_post(lambda *a,**k:calls.append(1) or SimpleNamespace(status_code=status))
    assert post("url").status_code==status
    with pytest.raises(ProviderBlocked):post("url")
    assert calls==[1] and (tmp_path/"provider-stop.private.json").exists()


def test_lost_pg_owner_stops_before_any_provider_send(tmp_path):
    def lost():raise RuntimeError("private detail")
    control=sector.SectorProviderControl(tmp_path,check_owner=lost)
    with pytest.raises(ProviderBlocked,match="ownership"):
        control.guarded_post(lambda *a,**k:pytest.fail("must not send"))("url")
    assert json.loads((tmp_path/"provider-stop.private.json").read_text())["reason"]=="coordinator_connection_lost"


def test_two_slots_preserve_inflight_replies_and_do_not_start_waiting_send_after_failure(tmp_path):
    barrier=threading.Barrier(2);lock=threading.Lock();active=0;maximum=0;sent=[]
    control=sector.SectorProviderControl(tmp_path,slots=2)
    def post(identity):
        nonlocal active,maximum
        with lock:active+=1;maximum=max(maximum,active);sent.append(identity)
        barrier.wait(timeout=3)
        if identity==1:
            control.fail("test_uncertain")
            time.sleep(.02)
        else:time.sleep(.03)
        with lock:active-=1
        return SimpleNamespace(status_code=200)
    guarded=control.guarded_post(post)
    outputs=[]
    def worker(identity):
        try:outputs.append(guarded(identity).status_code)
        except ProviderBlocked:outputs.append("blocked")
    threads=[threading.Thread(target=worker,args=(i,)) for i in (1,2,3)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=5)
    assert maximum==2 and len(sent)==2 and sorted(outputs,key=str)==[200,200,"blocked"]


def test_publisher_sector_event_cannot_enable_replacement_or_unscoped_none():
    row=sample()
    base=dict(profile_id=run.PROFILE_ID,tenant_id=run.TENANT_ID,expected_profile_fingerprint="x",
        score=70,verdict="gris",breakdown={},missing_actions=[],source_reader=lambda *_:row,
        backup_callback=lambda *_:None,expected_fit_level=None,event_type=scopes.SECTOR_SCOPE.publication_event)
    with pytest.raises(persistence.PublicationConflict):persistence.publish_case(None,row,**base)
    with pytest.raises(persistence.PublicationConflict,match="new matches"):
        persistence.publish_case(None,row,**base,scope_name=scopes.SECTOR_SCOPE.name,expected_previous={"id":1})


def test_failed_sector_context_validation_restores_legacy_globals(tmp_path,monkeypatch):
    monkeypatch.setattr(run,"ROOT",tmp_path)
    before=(run.ACTIVE_SCOPE,run.GEMMA_MODEL,run.MODEL,run.ACTIVE_FOLDER)
    with pytest.raises(ValueError):
        with run.execution_scope(scopes.SECTOR_SCOPE,folder=tmp_path/"elsewhere"):pass
    assert (run.ACTIVE_SCOPE,run.GEMMA_MODEL,run.MODEL,run.ACTIVE_FOLDER)==before
    with run.execution_scope(scopes.SECTOR_SCOPE,folder=tmp_path/"tmp"/scopes.SECTOR_SCOPE.folder_name/"pilot"):
        assert run.scope_persistence_kwargs()=={"expected_fit_level":None,"scope_name":scopes.SECTOR_SCOPE.name}
        assert run.GEMMA_MODEL=="gemini-3.1-flash-lite" and run.scope_replacements()==frozenset()
    assert (run.ACTIVE_SCOPE,run.GEMMA_MODEL,run.MODEL,run.ACTIVE_FOLDER)==before


def test_execute_without_go_rejects_before_ssh_pg_or_models(tmp_path,monkeypatch):
    monkeypatch.setattr(entry,"ROOT",tmp_path)
    chosen=selection();path=tmp_path/"selected.json";path.write_text(json.dumps(chosen))
    folder=tmp_path/"tmp"/scopes.SECTOR_SCOPE.folder_name/"pilot"
    with pytest.raises(ValueError,match="--go"):
        entry.main(["execute","--selection",str(path),"--expected-sha256",hashlib.sha256(path.read_bytes()).hexdigest(),"--folder",str(folder)])
    assert not folder.exists()


def test_grouping_preserves_complete_requirements_and_finite_two_case_batches():
    contexts={str(i):{"evaluation":{"requirements":[{}]*size}} for i,size in enumerate([9,8,2,3,40])}
    groups=entry.groups_for_contexts(contexts)
    assert groups==[["0"],["1","2"],["3"],["4"]]
    assert {item for group in groups for item in group}==set(contexts)


def test_readonly_freeze_and_offline_prepare_bind_exact_din_evidence_without_ingestion_enable(tmp_path,monkeypatch):
    monkeypatch.setattr(run,"ROOT",tmp_path)
    folder=tmp_path/"tmp"/scopes.SECTOR_SCOPE.folder_name/"pilot";folder.mkdir(parents=True)
    row=sample();chosen=selection([row["selection"]]);evidence_files(tmp_path,chosen)
    raw=json.dumps(chosen).encode();digest=hashlib.sha256(raw).hexdigest()
    profile={"id":run.PROFILE_ID,"tenant_id":run.TENANT_ID,"is_active":True,
        "ingestion_enabled":False,"razon_social":"DIN","experience_json":[]}
    lines=[{"id":"line","profile_id":run.PROFILE_ID,"is_active":True,
        "nombre":"Industrial","keyword_phrases":[],"keyword_terms":[],"cubso_segmentos":[]}]
    class Conn:
        def execute(self,sql,args):
            assert sql.startswith("SELECT ") and not any(word in sql for word in ("INSERT ","UPDATE ","DELETE "))
            if "company_profiles" in sql:self.result=profile
            elif "business_lines" in sql:self.result=lines
            elif "company_documents" in sql:
                assert "content BYTEA" not in sql and "encode(sha256(content),'hex')" in sql
                self.result=[]
            else:pytest.fail("Unexpected freeze SQL")
            return self
        def fetchone(self):return self.result
        def fetchall(self):return self.result
    monkeypatch.setattr(run,"source_reader",lambda conn,case:deepcopy(row))
    monkeypatch.setattr(run,"build_profile_cards",lambda profile:{"company":{},"coverage":{"document_count":0}})
    monkeypatch.setattr(run.tiktoken,"get_encoding",lambda _:SimpleNamespace(encode=lambda text:[]))
    monkeypatch.setattr(run,"build_jev_payload",lambda cards,op,facets,**kw:{"model":run.JEV_MODEL,"state":{"requirements":facets},"questions":{}})
    monkeypatch.setattr(run,"token_budget",lambda *a,**k:{"offline":True})
    with run.execution_scope(scopes.SECTOR_SCOPE,folder=folder):
        receipt=entry.freeze_with_connection(Conn(),chosen,digest,raw,folder)
        frozen,cards,cases,manifest=run.prepare(folder)
        assert receipt["ready"]==1 and receipt["max_lifecycle_age_seconds"]==21600
        assert frozen["profile"]["ingestion_enabled"] is False
        assert len(cases)==1 and cases[0]["case"]["fit_level"]==1
        assert cases[0]["binding"]["lifecycle_policy"]==sector.LIFECYCLE_POLICY
        assert cases[0]["binding"]["sector_selection_sha256"]==digest
        assert manifest["cohort"]==scopes.SECTOR_SCOPE.name and manifest["fit_level"] is None
        assert entry.freeze_with_connection(Conn(),chosen,digest,raw,folder)==receipt
    assert not list(folder.glob("**/*started.private.json"))


def test_fresh_historical_ledger_still_blocks_paid_copy_in_new_sector_folder(tmp_path,monkeypatch):
    monkeypatch.setattr(run,"ROOT",tmp_path)
    monkeypatch.setattr(run,"get_cached_jev_receipt",lambda folder:None)
    for name in ("pilot","batch-02"):
        folder=tmp_path/"tmp"/scopes.SECTOR_SCOPE.folder_name/name
        with run.execution_scope(scopes.SECTOR_SCOPE,folder=folder):
            live={"prior_provider_events":[{"stage":scopes.SECTOR_SCOPE.event_prefix+"jev","provider_request_hash":"already-paid"}]}
            with pytest.raises(run.PublicationConflict,match="outside this cohort"):
                run.guard_live_provider_events(live,folder/"seace_prod2-19")


def test_zero_target_writes_final_progress_without_models(tmp_path,monkeypatch):
    folder=tmp_path/"zero";folder.mkdir()
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,args):return SimpleNamespace(fetchone=lambda:{"locked":True})
    monkeypatch.setattr(run.psycopg,"connect",lambda *a,**k:Conn())
    frozen={"cases":[{"ready":False}]}
    result=entry.execute_with_settings(SimpleNamespace(database_url="fake"),frozen,{},[],folder,"not-used",
        batch_size=10,max_batches=1,model_workers=2,replay_only=False,code={"commit":"test"})
    assert result["complete"] is True and result["target"]==0 and result["excluded"]==1
    assert json.loads((folder/"progress.private.json").read_text())==result
    assert not list(folder.glob("**/*started.private.json"))


def test_all_published_resume_verifies_live_ids_and_writes_complete_progress(tmp_path,monkeypatch):
    folder=tmp_path/"resume";folder.mkdir();item={"case":{"source_key":"seace_prod2","source_id":19},"jev_payload":{}}
    path=folder/"seace_prod2-19"/"published.private.json";private_json(path,{"match_id":10})
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,args):return SimpleNamespace(fetchone=lambda:{"locked":True})
    monkeypatch.setattr(run.psycopg,"connect",lambda *a,**k:Conn())
    monkeypatch.setattr(run,"get_cached_jev_receipt",lambda *a:None)
    calls=[]
    monkeypatch.setattr(run,"run_case",lambda *a,**k:calls.append(1) or {"source_key":"seace_prod2","source_id":19,"match_id":10,"cost_usd":"0.01"})
    frozen={"cases":[{"ready":True}]}
    result=entry.execute_with_settings(SimpleNamespace(database_url="fake"),frozen,{},[item],folder,"not-used",
        batch_size=10,max_batches=1,model_workers=2,replay_only=False,code={"commit":"test"})
    assert result["complete"] and len(calls)==1 and len(result["results"])==1
    assert json.loads((folder/"progress.private.json").read_text())==result


def test_known_unpublished_usage_and_uncertain_attempts_are_not_mixed(tmp_path,monkeypatch):
    item={"case":{"source_key":"seace_prod2","source_id":19},"jev_payload":{}}
    monkeypatch.setattr(run,"get_cached_jev_receipt",lambda *a:{"body":{"usage":{"input_tokens":1000}}})
    batch=tmp_path/"gemma-batches"/"batch-one";batch.mkdir(parents=True)
    private_json(batch/"gemma-started.private.json",{})
    private_json(batch/"gemma-response.private.json",{"http_status":200,"body":{"usageMetadata":{
        "promptTokenCount":1000,"candidatesTokenCount":100,"thoughtsTokenCount":20}}})
    other=tmp_path/"gemma-batches"/"batch-uncertain";other.mkdir()
    private_json(other/"gemma-started.private.json",{})
    costs=entry.provider_costs(tmp_path,[item])
    assert costs["known_cost_usd"]=="0.000472" and costs["uncertain_provider_attempts"]==1
    assert costs["provider_responses_with_known_usage"]==2


def test_mocked_sigterm_holds_new_sends_and_drains_started_responses(tmp_path,monkeypatch):
    installed={};restored=[]
    monkeypatch.setattr(entry.signal,"getsignal",lambda number:"previous-"+str(number))
    def install(number,handler):
        if callable(handler):installed[number]=handler
        else:restored.append((number,handler))
    monkeypatch.setattr(entry.signal,"signal",install)
    started=threading.Event();finish=threading.Event();replies=[]
    control=sector.SectorProviderControl(tmp_path)
    def post(*args,**kwargs):
        started.set();assert finish.wait(3)
        return SimpleNamespace(status_code=200,text='{"saved":true}')
    def worker():
        replies.append(dispatch_once(tmp_path/"case","jev",{"id":19},
            url="https://api.typesafe.ai/v1/systemone",api_key="test-only",header_name="Authorization",
            post=control.guarded_post(post)))
    with entry.graceful_provider_stop(control):
        thread=threading.Thread(target=worker);thread.start();assert started.wait(3)
        installed[signal.SIGTERM](signal.SIGTERM,None)
        assert control.stop.is_set()
        with pytest.raises(ProviderBlocked):control.guarded_post(post)("unused")
        finish.set();thread.join(timeout=3)
        assert not thread.is_alive()
    assert replies[0]["body"]=={"saved":True}
    assert (tmp_path/"case"/"jev-response.private.json").exists()
    assert json.loads((tmp_path/"provider-stop.private.json").read_text())["reason"]=="operator_stop"
    assert {number for number,_ in restored}=={signal.SIGTERM,signal.SIGINT}
