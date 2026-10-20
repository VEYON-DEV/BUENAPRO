"""Offline coordinator evidence, ownership, budget and no-retry regressions."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import din_sector_chain as chain

NOW = datetime(2026, 10, 20, 17, tzinfo=timezone.utc)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(chain._canonical(value))
    path.chmod(0o600)


def costs(**changes):
    return {**dict.fromkeys(chain._COST_COUNT_FIELDS + chain._COST_NUMBER_FIELDS, 0), **changes}


def outcome(identity):
    return dict(convocation_id=identity, accepted=False, status="closed_without_reading")


def report(ids, count, *, run_id="reader", limit=None):
    n = len(ids)
    cap = limit if limit is not None else count
    limits = chain.budget(max(1, (cap+9)//10), n)
    return dict(run_id=run_id, pipeline="prod2_selected_llm1_v1", terminal_stage="llm1",
        source_queries_repeated=0, source_coverage_complete=True, embedding_dispatch_stop_requested=False,
        costs=costs(), execution_policy=dict(all_results=False,
            effective_limits={k:limits[k] for k in ("max_accepted","max_candidates","max_jev_calls","max_llm1_calls")},
            max_new_documents=limits["max_new_documents"]), handoff_guards=dict(policy_version="prod2_handoff_guards_v1",
            dispatch_stopped=False, budget_deferred=count < n, candidate_admissions=count,
            document_admissions=0, max_candidates=cap, max_new_documents=limits["max_new_documents"]),
        redis=dict(queued=n-count, pending=0, payloads=n-count, seen=n), processed=count,
        accepted=0, llm1_complete=0, requires_review=0, records=[outcome(i) for i in ids[:count]],
        coverage=[dict(coverage_complete=True, selected_ids=ids, errors=[])],
        scope_proofs=[],queries_total=1,queries_complete=1,
        execution_status="budget_deferred" if count < n else "complete")


def test_finite_cumulative_caps_never_use_unbounded_all_results():
    assert chain.budget(1, 157) == dict(max_candidates=10, max_accepted=10, max_new_documents=10,
        max_llm1_calls=10, max_jev_calls=30, max_embedding_calls=100)
    assert chain.budget(16, 157)["max_candidates"] == 157
    assert chain.budget(16, 157)["max_jev_calls"] == 480
    assert chain.budget(16, 157)["max_embedding_calls"] == 1600
    assert chain.budget(24,157)["max_candidates"]==157
    assert chain.budget(24,157)["max_new_documents"]==240
    for number, total in ((25, 157), (True, 157), (0, 157), (1, True), (1, 158)):
        with pytest.raises(chain.ChainError):
            chain.budget(number, total)


@pytest.mark.parametrize("flag", [True, False])
def test_budget_deferred_nonempty_queue_is_normal_without_guard_stop(flag):
    ids = list(range(1, 22)); value = report(ids, 10)
    value["handoff_guards"]["budget_deferred"] = flag
    gate = chain.assess_batch(value, "reader", ids, chain.budget(1, len(ids)))
    assert gate["complete"] is False and gate["processed"] == 10


@pytest.mark.parametrize("key,bad", [("usage_unknown_attempts", 1), ("usage_unknown_attempts", None),
    ("usage_unknown_attempts", False), ("known_cost_usd", None), ("known_cost_usd", float("nan")),
    ("known_cost_usd", False), ("embedding_unknown_dispatch_attempts", 1),
    ("embedding_usage_unknown_attempts", 1), ("embedding_receipt_conflicts", 1)])
def test_unknown_or_malformed_cost_is_not_converted_to_zero(key, bad):
    value = report([1], 1); value["costs"][key] = bad
    with pytest.raises(chain.ChainError):
        chain.assess_batch(value, "reader", [1], chain.budget(1, 1))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(processed=True),
    lambda r: r.update(source_queries_repeated=False),
    lambda r: r["handoff_guards"].update(dispatch_stopped=True),
    lambda r: r["handoff_guards"].update(dispatch_stopped=0),
    lambda r: r["handoff_guards"].update(document_admissions=11),
    lambda r: r["records"][0].update(error_type="ProtocolError"),
    lambda r: r["records"][0].update(status="future_unknown_status"),
    lambda r: r["records"].append(deepcopy(r["records"][0])),
    lambda r: r["records"][0].update(convocation_id=999),
    lambda r: r["redis"].update(pending=1),
    lambda r: r["coverage"][0].update(selected_ids=[999]),
    lambda r: r.update(execution_status="attention_required"),
])
def test_failed_or_forged_report_never_advances(mutate):
    value = report([1], 1); mutate(value)
    with pytest.raises(chain.ChainError):
        chain.assess_batch(value, "reader", [1], chain.budget(1, 1))


def test_terminal_evidence_and_provider_counters_cannot_regress():
    ids = list(range(1, 22)); first = report(ids, 10)
    first["costs"].update(jev_calls=1, attempts=1, known_cost_usd=.01)
    previous = chain.assess_batch(first, "reader", ids, chain.budget(1, 21))
    second = report(ids, 20)
    with pytest.raises(chain.ChainError, match="regressed"):
        chain.assess_batch(second, "reader", ids, chain.budget(2, 21), previous)
    second["costs"].update(jev_calls=1, attempts=1, known_cost_usd=.01)
    second["records"][0]["status"] = "needs_review"
    with pytest.raises(chain.ChainError, match="terminal_outcome_changed"):
        chain.assess_batch(second, "reader", ids, chain.budget(2, 21), previous)


@pytest.mark.parametrize("reason", sorted(chain.DOCUMENT_REVIEW_REASONS))
def test_only_sha_bound_document_quality_review_can_advance_without_retry(tmp_path,reason):
    path=tmp_path/"1/embedding-preparation-review.private.json"
    receipt=dict(reason=reason,corpus_hash="a"*64,full_pdf_fallback=False)
    if reason=="retrieval_context_needs_expansion_or_review":receipt.update(retrieval_hash="b"*64,embedding_cost_usd=.001)
    else:receipt.update(unresolved_pages=[1,2],provider_calls=0)
    write(path,receipt)
    row=dict(convocation_id=1,accepted=True,status="needs_review",requires_human_review=True,
        error_type="EmbeddingPreparationReview",error_chain=[dict(error_type="EmbeddingPreparationReview")],
        document_preparation_review=dict(reason=reason,receipt_sha256=chain.file_hash(path)))
    from buenapro_worker.prod2.handoff_guards import document_preparation_review
    from buenapro_worker.prod2.embedding_runtime import EmbeddingPreparationReview
    assert document_preparation_review(EmbeddingPreparationReview("offline"),path.parent)==row["document_preparation_review"]
    value=report([1],1);value.update(records=[row],accepted=1,requires_review=1)
    assert chain.assess_batch(value,"reader",[1],chain.budget(1,1))["complete"] is True
    assert chain.validate_document_review(row,tmp_path)["reason"]==reason
    for bad in ("EmbeddingQueryReview","OperationalError","HTTPStatusError"):
        damaged=deepcopy(value);damaged["records"][0].update(error_type=bad,error_chain=[dict(error_type=bad)])
        with pytest.raises(chain.ChainError,match="technical"):
            chain.assess_batch(damaged,"reader",[1],chain.budget(1,1))
    receipt["full_pdf_fallback"]=True;write(path,receipt)
    with pytest.raises(chain.ChainError):chain.validate_document_review(row,tmp_path)


def redis_evidence():
    items = [dict(row=dict(convocation_id=i)) for i in (1, 2, 3)]
    snap = dict(dedup={"1":"done", "2":"queued", "3":"queued"},
        payloads={"2":items[1], "3":items[2]}, queue=["2", "3"], pending=[],
        stats=dict(queued=2, pending=0, payloads=2, seen=3))
    return items, snap


def test_atomic_queue_snapshot_partitions_scope_without_pg_member_equality():
    items, snap = redis_evidence()
    assert chain.validate_redis(snap, items, [1], snap["stats"])["seen"] == 3


@pytest.mark.parametrize("mutate", [
    lambda r:r["queue"].append("2"), lambda r:r["queue"].append("1"),
    lambda r:r["dedup"].update({"999":"done"}), lambda r:r["payloads"].pop("2"),
    lambda r:r["payloads"]["2"]["row"].update(convocation_id=999),
    lambda r:r["dedup"].update({"1":"queued"}), lambda r:r["stats"].update(seen=4),
    lambda r:r["stats"].update(queued=True),
])
def test_bad_queue_payload_identity_or_count_blocks(mutate):
    items, snap = redis_evidence(); snap = deepcopy(snap); mutate(snap)
    with pytest.raises(chain.ChainError):
        chain.validate_redis(snap, items, [1])


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path.resolve(); (root/"tmp").mkdir()
    ids = list(range(1, 22)); selection_sha = "a"*64
    plan = dict(version=chain.VERSION, planning_only=False, scope_id="scope", caffeinate=False,extra_batches=8,
        reader_folder=str(root/"tmp/reader"), matching_folder=str(root/"tmp/matching"),
        mapping_handoff=dict(path="/private/handoff",sha256="b"*64),
        selection=dict(path="/private/selection",sha256=selection_sha),
        embedding_config=dict(path="/private/v5",sha256="c"*64), runtime=dict(ssh_host="qa.invalid",ssh_key="/private/key"))
    imported = dict(binding=dict(candidate_ids=ids),items=[dict(row=dict(convocation_id=i)) for i in ids])
    review = dict(held_ids=[999],decisions=[dict(convocation_id=i,decision="included",
        sector="ia_automatizacion" if i==1 else "industrial",reason="Explicit synthetic scope") for i in ids])
    monkeypatch.setattr(chain,"load_plan",lambda *args:(plan,imported,review))
    monkeypatch.setattr(chain,"committed_runtime",lambda *args:None)
    children=[]; readbacks=[]
    def readback(plan_,imported_,path=None,*,now,expected_limits=None):
        readbacks.append(path)
        value=chain._read(path) if path else None
        done=[r["convocation_id"] for r in value["records"]] if value else []
        return dict(version=chain.VERSION,checked_at_utc=now().isoformat(),run_id="reader",
            folder=plan["reader_folder"],selection_sha256=selection_sha,
            report_sha256=chain.file_hash(path) if path else None,exact_ids_verified=True,
            active_coordinators=0,pg_members=[],readings=[],outcome_ids=sorted(done),remaining_ids=sorted(set(ids)-set(done)),
            redis=value["redis"] if value else dict(queued=0,pending=0,payloads=0,seen=0))
    class Child:
        def __init__(self,argv,**kwargs):
            self.pid=41000+len(children);self.argv=argv;self.returncode=0;self.signals=[]
            self.kwargs=kwargs;children.append(self)
            folder=Path(argv[argv.index("--folder")+1])
            if argv[1].endswith("run_prod2_selected.py"):
                if not (folder/"manifest.private.json").exists():write(folder/"manifest.private.json",dict(run_id="reader"))
                cap=int(argv[argv.index("--max-candidates")+1]);value=report(ids,cap)
                limits={key:int(argv[argv.index("--"+key.replace("_","-"))+1]) for key in chain.budget(1,len(ids))}
                value["execution_policy"]["effective_limits"]={k:limits[k] for k in ("max_accepted","max_candidates","max_jev_calls","max_llm1_calls")}
                value["execution_policy"]["max_new_documents"]=limits["max_new_documents"]
                value["handoff_guards"]["max_new_documents"]=limits["max_new_documents"]
                write(folder/f"report-{len(children):03d}.private.json",value)
            elif "execute" in argv:
                write(folder/"progress.private.json",dict(complete=True,target=0,selected=21,excluded=21,
                    results=[],known_cost_usd="0",uncertain_provider_attempts=0))
        def poll(self):return self.returncode
        def send_signal(self,sig):self.signals.append(sig);self.returncode=0
        def wait(self):return self.returncode
    return SimpleNamespace(root=root,plan=plan,imported=imported,review=review,children=children,
        readbacks=readbacks,readback=readback,popen=Child,folder=root/"tmp/chain",plan_path=root/"plan.private.json",sha="d"*64)


def run(h,**kwargs):
    return chain.run(h.plan_path,h.sha,h.folder,project_root=h.root,now=lambda:NOW,
        readback=kwargs.pop("readback",h.readback),popen=kwargs.pop("popen",h.popen),**kwargs)


def test_no_go_does_not_read_database_start_child_or_create_state(harness):
    h=harness
    assert run(h)["status"]=="prepared"
    assert not h.children and not h.readbacks and not h.folder.exists()


def test_planning_only_cannot_start_even_with_go(harness):
    h=harness;h.plan["planning_only"]=True
    with pytest.raises(chain.ChainError,match="planning_only"):
        run(h,go=True)
    assert not h.children and not h.readbacks


def test_chain_imports_once_uses_same_folder_expands_and_freezes_matching(harness):
    h=harness;assert run(h,go=True)["status"]=="complete"
    readers=[c for c in h.children if c.argv[1].endswith("run_prod2_selected.py")]
    assert len(readers)==3 and len(h.readbacks)==4
    assert [c.argv[c.argv.index("--max-candidates")+1] for c in readers]==["10","20","21"]
    for child in readers:
        assert child.argv[child.argv.index("--folder")+1]==h.plan["reader_folder"]
        assert "--all-results" not in child.argv and "--retry-current" not in child.argv
        assert "--keyword" not in child.argv and "--mapping-handoff" in child.argv
    assert len(h.children)==7
    saved=chain._read(h.folder/"state.private.json");assert saved["held_outside_scope"]==1
    match=chain._read(h.folder/"matching-selection.private.json")
    assert len(match["cases"])==21 and all(c["lifecycle"]["matching_eligible"] is None for c in match["cases"])
    assert all(p.stat().st_mode&0o777==0o600 for p in h.folder.glob("*.private.*"))
    assert run(h,go=True)["status"]=="complete" and len(h.children)==7


def test_fresh_readback_failure_stops_before_expansion_without_retry(harness):
    h=harness
    def rb(*args,**kwargs):
        if args[2] is not None:raise chain.ChainError("fresh_redis_report_mismatch")
        return h.readback(*args,**kwargs)
    assert run(h,go=True,readback=rb)["status"]=="attention_required"
    assert len(h.children)==1
    assert run(h,go=True)["status"]=="attention_required" and len(h.children)==1


def test_nonzero_reader_or_missing_new_report_never_retries(harness):
    h=harness
    def bad(argv,**kwargs):
        child=h.popen(argv,**kwargs);child.returncode=2;return child
    assert run(h,go=True,popen=bad)["reason"]=="child_failed_no_automatic_retry"
    assert len(h.children)==1


def test_extra_stage_can_expand_documents_after_all_ids_already_admitted(harness):
    h=harness;calls=0
    def deferred(argv,**kwargs):
        nonlocal calls
        child=h.popen(argv,**kwargs)
        if argv[1].endswith("run_prod2_selected.py"):
            calls+=1;path=Path(h.plan["reader_folder"])/f"report-{len(h.children):03d}.private.json"
            value=chain._read(path)
            if calls==3:
                value.update(processed=20,records=value["records"][:20],execution_status="budget_deferred")
                value["redis"].update(queued=1,payloads=1)
                value["handoff_guards"].update(candidate_admissions=21,document_admissions=30,budget_deferred=True)
            if calls==4:value["handoff_guards"].update(document_admissions=31)
            write(path,value)
        return child
    assert run(h,go=True,popen=deferred)["status"]=="complete"
    readers=h.children[:4]
    assert [c.argv[c.argv.index("--max-candidates")+1] for c in readers]==["10","20","21","21"]
    assert [c.argv[c.argv.index("--max-new-documents")+1] for c in readers]==["10","20","30","40"]


def test_successful_exit_without_new_report_is_attention(harness):
    h=harness
    def missing(argv,**kwargs):
        child=h.popen(argv,**kwargs)
        for p in Path(h.plan["reader_folder"]).glob("report-*.private.json"):p.unlink()
        return child
    assert run(h,go=True,popen=missing)["reason"]=="missing_or_ambiguous_new_report"
    assert len(h.children)==1


def test_completed_state_rechecks_evidence_and_does_not_trust_status(harness):
    h=harness;assert run(h,go=True)["status"]=="complete"
    state=chain._read(h.folder/"state.private.json");path=Path(state["report_path"])
    value=chain._read(path);value["costs"]["usage_unknown_attempts"]=1;write(path,value)
    assert run(h,go=True)["reason"]=="approved_report_changed"
    assert len(h.children)==7


def test_completed_state_rechecks_frozen_manifest(harness):
    h=harness;assert run(h,go=True)["status"]=="complete"
    write(Path(h.plan["reader_folder"])/"manifest.private.json",dict(run_id="changed"))
    assert run(h,go=True)["reason"]=="reader_manifest_changed"
    assert len(h.children)==7


def test_no_adoption_of_an_existing_matching_folder(harness):
    h=harness;Path(h.plan["matching_folder"]).mkdir()
    assert run(h,go=True)["reason"]=="existing_matching_folder_requires_review"
    assert not h.children and not h.readbacks


def test_checkpoint_failure_after_popen_drains_only_owned_child_and_saves_exit(harness,monkeypatch):
    h=harness;original_write=chain._write;failed=False
    def checkpoint(path,value):
        nonlocal failed
        if path.name=="state.private.json" and value.get("status")=="running_child" and not failed:
            failed=True;raise OSError("synthetic disk failure")
        original_write(path,value)
    def running(argv,**kwargs):
        child=h.popen(argv,**kwargs);child.returncode=None;return child
    monkeypatch.setattr(chain,"_write",checkpoint)
    assert run(h,go=True,popen=running)["status"]=="attention_required"
    assert len(h.children)==1 and h.children[0].signals==[signal.SIGTERM]
    exit_receipt=chain._read(h.folder/"reader-001.exit.private.json")
    assert exit_receipt["pid"]==h.children[0].pid and exit_receipt["interrupted"] is True


def test_preexisting_interrupted_state_is_attention_without_child(harness):
    h=harness;write(h.folder/"state.private.json",dict(version=chain.VERSION,plan_sha256=h.sha,status="running_child"))
    assert run(h,go=True)["status"]=="attention_required"
    assert not h.children and not h.readbacks


def test_single_instance_lock_never_starts_second_owner(harness):
    h=harness;h.folder.mkdir()
    with chain.instance_lock(h.folder):
        with pytest.raises(BlockingIOError):run(h,go=True)
    assert not h.children


@pytest.mark.parametrize("age", [121,-1])
def test_readback_must_be_fresh_and_cannot_come_from_future(harness,age):
    h=harness;value=h.readback(h.plan,h.imported,None,now=lambda:NOW-timedelta(seconds=age))
    with pytest.raises(chain.ChainError,match="stale"):
        chain.check_readback(value,h.plan,None,h.imported["binding"]["candidate_ids"],NOW)


def test_matching_lifecycle_exact_copy_never_promotes_missing_or_closed_gate(tmp_path):
    source=tmp_path/"report.private.json";rows=[outcome(1),outcome(2)]
    rows[0].update(status="llm1_complete",confirmed_open=True,temporal_state="submission_open",
        temporal_observed_at=NOW.isoformat(),requires_fresh_lifecycle=False,matching_eligible=True,
        closes_at=(NOW+timedelta(hours=1)).isoformat())
    value=dict(records=rows);write(source,value)
    review=dict(decisions=[dict(convocation_id=i,decision="included",sector="industrial",reason="Scope") for i in (1,2)])
    selected,excluded=chain.matching_selection(value,review,source,NOW)
    assert len(selected["cases"])==2 and excluded==[dict(convocation_id=2,reason="reader_or_fresh_lifecycle_not_matching_ready")]
    assert selected["cases"][0]["lifecycle"]=={k:rows[0].get(k) for k in (
        "confirmed_open","temporal_state","temporal_observed_at","requires_fresh_lifecycle","matching_eligible","closes_at")}


@pytest.mark.parametrize("mutate", [lambda p:p.update(complete=False),lambda p:p.update(held=True),
    lambda p:p.update(uncertain_provider_attempts=1),lambda p:p.update(uncertain_provider_attempts=False),
    lambda p:p.update(known_cost_usd=None),lambda p:p.update(known_cost_usd="NaN"),
    lambda p:p.update(selected=True),lambda p:p.update(target=1),lambda p:p.update(excluded=2)])
def test_matching_progress_is_not_qa_or_success_if_incomplete_or_uncertain(mutate):
    selection=dict(cases=[dict(source_key="seace_prod2",source_id=1)])
    progress=dict(complete=True,target=0,selected=1,excluded=1,results=[],known_cost_usd="0",uncertain_provider_attempts=0)
    chain.assess_matching(progress,selection);mutate(progress)
    with pytest.raises(chain.ChainError):chain.assess_matching(progress,selection)


def test_pg_snapshot_uses_only_selects_and_scopes_members_to_new_run():
    class Connection:
        def __init__(self):self.calls=[]
        def execute(self,sql,params=()):self.calls.append((sql,params));return self
        def fetchone(self):return dict(count=0)
        def fetchall(self):return []
    conn=Connection();assert chain._pg_snapshot(conn,"new-run")== (0,[])
    assert all(sql.lstrip().startswith("SELECT") for sql,_ in conn.calls)
    assert conn.calls[1][1]==("new-run",) and "WHERE m.run_id=%s" in conn.calls[1][0]
    assert all("try_advisory_lock" not in sql for sql,_ in conn.calls)


def test_redis_snapshot_uses_only_own_four_keys_and_read_commands():
    class Pipe:
        def __init__(self):self.calls=[]
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def hgetall(self,key):self.calls.append(("hgetall",key));return self
        def lrange(self,key,*args):self.calls.append(("lrange",key));return self
        def hlen(self,key):self.calls.append(("hlen",key));return self
        def llen(self,key):self.calls.append(("llen",key));return self
        def execute(self):return [{},{},[],[],0,0,0,0]
    pipe=Pipe()
    class Client:
        def pipeline(self,transaction):assert transaction is True;return pipe
    result=chain._redis_snapshot(Client(),"new-run")
    assert result["stats"]["seen"]==0 and len(pipe.calls)==8
    assert all(key.startswith("buenapro:prod2:accepted:new-run:") for _,key in pipe.calls)
    assert {name for name,_ in pipe.calls}=={"hgetall","lrange","hlen","llen"}


def test_fresh_preflight_connects_pg_default_read_only_and_rolls_back(harness,monkeypatch):
    from buenapro_worker.prod2 import runtime
    import psycopg
    import redis
    h=harness;seen={}
    @contextmanager
    def settings(**kwargs):yield SimpleNamespace(database_url="postgresql://loopback.invalid/qa"),"redis://loopback.invalid/0"
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql):seen.setdefault("sql",[]).append(sql);return self
        def fetchone(self):return dict(transaction_read_only="on")
        def rollback(self):seen["rollback"]=True
    def connect(url,**kwargs):seen["connect"]=kwargs;return Connection()
    client=SimpleNamespace(close=lambda:seen.update(redis_closed=True))
    monkeypatch.setattr(runtime,"production_staging_settings",settings)
    monkeypatch.setattr(psycopg,"connect",connect)
    monkeypatch.setattr(redis.Redis,"from_url",lambda *a,**kw:client)
    monkeypatch.setattr(chain,"_pg_snapshot",lambda *a:(0,[]))
    monkeypatch.setattr(chain,"_redis_snapshot",lambda *a:dict(dedup={},payloads={},queue=[],pending=[],
        stats=dict(queued=0,pending=0,payloads=0,seen=0)))
    value=chain.fresh_readback(h.plan,h.imported,now=lambda:NOW)
    assert value["pg_writes"]==value["redis_writes"]==value["source_calls"]==value["model_calls"]==0
    assert "default_transaction_read_only=on" in seen["connect"]["options"]
    assert seen["sql"][0]=="SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert seen["rollback"] and seen["redis_closed"]


def test_actual_reader_configuration_and_dry_run_accept_complete_v5_args_without_legacy_compact(tmp_path,monkeypatch,capsys):
    from buenapro_worker.prod2 import handoff_selection
    root=Path(__file__).resolve().parents[3];selected=chain._selected(root)
    binding=dict(candidate_ids=[1]);imported=dict(binding=binding,keywords=["tablero"],years=[2026],
        published_from="2026-10-04",published_to="2026-10-20",items=[],coverage=[dict(coverage_complete=True)],scope_proofs=[])
    monkeypatch.setattr(handoff_selection,"load_selection",lambda *a,**k:imported)
    plan=dict(reader_folder=str(tmp_path/"reader"),mapping_handoff=dict(path=str(tmp_path/"mapping/handoff.private.json")),
        selection=dict(path=str(tmp_path/"selection.private.json"),sha256="a"*64),
        embedding_config=dict(path=str(root/"docs/sources/prod2-embedding-reading.runtime.v5.json")),
        runtime=dict(ssh_host="qa.invalid",ssh_key="/private/key"))
    argv=chain.reader_argv(plan,root,chain.budget(1,1))
    assert "--compact-unique-requirements" not in argv
    args=selected.parser().parse_args(argv[2:]);requested=selected.plan(args)
    assert requested["llm1_scope_policy"]=="prod2_qualification_focus_v1"
    assert requested["llm1_input_policy"]=="prod2_embedding_retrieved_text_v1"
    assert requested["llm1_commercial_taxonomy"]=="commercial_taxonomy_v1"
    assert "llm1_reading_policy" not in requested
    monkeypatch.setattr("sys.argv",argv[1:]+["--dry-run"])
    assert selected.main()==0
    assert json.loads(capsys.readouterr().out)["base_queries"]==0


def test_historical_unlisted_document_keeps_strict_paid_proof_without_claiming_readiness():
    from test_prod2_prior_reading_guard import prior,Connection,call,CONV,PROFILE
    row=prior();original=call(Connection([row]));row["is_listed"]=False;row["is_current"]=False
    record={**original,"convocation_id":CONV,"matching_eligible":False,"canonical_document_still_listed":False}
    conn=Connection([row]);before=deepcopy(conn.rows)
    proof=chain.historical_reading_proof(conn,record,PROFILE)
    assert proof==original["prior_reading_preserved"]["canonical"]
    assert conn.rows==before and conn.rows[0]["is_listed"] is False and conn.rows[0]["is_current"] is False
    for field in ("status","sha256_original","extraction_id","reserved_model","download_sha256"):
        damaged=deepcopy(row);damaged[field]="changed"
        with pytest.raises((chain.ChainError,ValueError,TypeError)):
            chain.historical_reading_proof(Connection([damaged]),record,PROFILE)
    damaged=deepcopy(row);damaged["result_json"]["cost_usd"]=None
    with pytest.raises(chain.ChainError):chain.historical_reading_proof(Connection([damaged]),record,PROFILE)


def test_plan_hash_input_hashes_runtime_and_literal_paths_are_verified(tmp_path,monkeypatch):
    from buenapro_worker.prod2 import handoff_selection
    root=tmp_path.resolve();(root/"tmp").mkdir()
    imported=dict(binding=dict(candidate_ids=[1]),items=[dict(row=dict(convocation_id=1))])
    monkeypatch.setattr(handoff_selection,"load_selection",lambda *a,**k:imported)
    paths={}
    for name in ("selection","mapping_handoff","selection_review","embedding_config"):
        path=root/(name+".json");write(path,{"scope_id":"scope"});paths[name]=dict(path=str(path),sha256=chain.file_hash(path))
    review=dict(scope_id="scope",selection_sha256=paths["selection"]["sha256"],included_ids_priority_order=[1],held_ids=[])
    write(Path(paths["selection_review"]["path"]),review);paths["selection_review"]["sha256"]=chain.file_hash(paths["selection_review"]["path"])
    write(root/"docs/sources/prod2-embedding-reading.runtime.v5.json",dict(scope_id="scope"))
    bindings={p:"a"*64 for p in ("scripts/run_din_sector_chain.py","scripts/run_prod2_selected.py",
        "scripts/run_dinaut_sector_matches.py","workers/seace/src/buenapro_worker/prod2/din_sector_chain.py")}
    monkeypatch.setattr(chain,"runtime_bindings",lambda *a:bindings)
    monkeypatch.setattr(chain,"matching_folder",lambda value,root:Path(value))
    monkeypatch.setattr(chain,"_selected",lambda *a:SimpleNamespace(parser=lambda:SimpleNamespace(parse_args=lambda x:x),
        plan=lambda x:dict(handoff_selection=imported["binding"])))
    plan=dict(version=chain.VERSION,scope_id="scope",planning_only=True,**paths,
        reader_folder=str(root/"tmp/reader"),matching_folder=str(root/"tmp/matching"),
        runtime=dict(ssh_host="qa.invalid",ssh_key="/private/key"),runtime_bindings=bindings,
        caffeinate=False,extra_batches=8,budget_note="Reference only, no USD hard cap")
    plan["code_commit"]="d"*40
    file=root/"plan.json";write(file,plan);digest=chain.file_hash(file)
    assert chain.load_plan(file,digest,root)[0]==plan
    with pytest.raises(chain.ChainError,match="plan_sha256"):
        chain.load_plan(file,"f"*64,root)
    write(Path(paths["selection"]["path"]),dict(scope_id="changed"))
    with pytest.raises(chain.ChainError,match="bound_file_changed"):
        chain.load_plan(file,digest,root)
    link=root/"linked.json";link.symlink_to(file)
    with pytest.raises(ValueError,match="symlinked"):
        chain.load_plan(link,digest,root)


def test_go_requires_tracked_code_equal_to_recorded_commit(monkeypatch,tmp_path):
    plan=dict(code_commit="a"*40,runtime_bindings={"scripts/reader.py":"b"*64})
    calls=[]
    def command(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=b"scripts/reader.py\0" if argv[1]=="ls-files" else b"")
    monkeypatch.setattr(chain.subprocess,"run",command)
    chain.committed_runtime(tmp_path,plan)
    assert all(argv[0]=="git" for argv in calls) and len(calls)==3
    monkeypatch.setattr(chain.subprocess,"run",lambda *a,**k:SimpleNamespace(stdout=b""))
    with pytest.raises(chain.ChainError,match="uncommitted_runtime_file"):
        chain.committed_runtime(tmp_path,plan)


def test_matching_folder_contract_is_checked_offline_before_any_reader():
    root=Path(__file__).resolve().parents[3]
    good=root/"tmp/dinaut-ia-industrial-20261020/batch-01"
    assert chain.matching_folder(str(good),root)==good.resolve()
    for bad in (root/"tmp/anything",good.parent/"batch-1",good.parent/"nested/batch-01"):
        with pytest.raises(ValueError):chain.matching_folder(str(bad),root)
