"""Offline-only regression contracts for the three-case timestamp recovery."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import din_sector_chain as chain
from buenapro_worker.prod2 import handoff_runtime_recovery as recovery
from buenapro_worker.prod2 import handoff_selection, service
from buenapro_worker.prod2.handoff_guards import lifecycle_fields
from buenapro_worker.prod2.repository import hash_request, prepare_detail
from test_din_sector_chain import report, costs, write


@pytest.fixture
def metadata_evidence(tmp_path, monkeypatch):
    """A complete immutable 001 history, then the three local-only 002 failures."""
    root = tmp_path
    folder = root / "tmp/work/runs/reader"
    previous = root / "tmp/chain001"
    chain_folder = root / "tmp/chain002"
    ids = [recovery.CASE, *range(1, 6), *recovery.METADATA_CASES[1:], *range(6, 155)]
    old_runtime = {p:"a"*64 for p in recovery.ALLOWED_RUNTIME_FILES
                   if not p.endswith("handoff_runtime_recovery.py")}
    current001 = {p:"b"*64 for p in recovery.ALLOWED_RUNTIME_FILES}
    current002 = {p:"c"*64 for p in recovery.ALLOWED_RUNTIME_FILES}
    frozen = dict(scope_id=recovery.SCOPE, candidate_ids=ids, runtime_bindings=old_runtime,
                  items_sha256="d"*64)
    imported = dict(binding=frozen, items=[dict(row=dict(convocation_id=i)) for i in ids])
    manifest = dict(handoff_selection=frozen, requested=dict(handoff_selection=frozen), run_id="reader")
    write(folder / "manifest.private.json", manifest)
    initial_state = root / "tmp/work/chain/state.private.json"
    write(initial_state, dict(parent_pid=1234501, child_pid=1234502))
    initial_plan = root / "tmp/initial-plan.private.json"
    write(initial_plan, dict(reader_folder=str(folder), code_commit="a"*40))
    first_report = report(ids, 6, run_id="reader", limit=10)
    first_report["handoff_selection"] = frozen
    first_report["costs"] = costs(attempts=4, jev_calls=4, known_cost_usd=.000295176)
    first_report["records"][0].update(accepted=True, status="needs_review", error_type="AttributeError",
        error_chain=[dict(error_type="AttributeError")])
    first_report.update(accepted=1, requires_review=1)
    first_path = folder / "report-1792510112683972000.private.json"
    write(first_path, first_report)
    for row in first_report["records"]:
        write(folder / str(row["convocation_id"]) / "outcome.private.json", row)
    for identity in recovery.METADATA_CASES:
        write(folder / str(identity) / "ocr-page-0001.private.json", dict(text_sha256="e"*64))
    def snapshot(version):
        rows = [dict(path=str(p.relative_to(folder)), sha256=recovery.sha(p), bytes=p.stat().st_size)
                for p in sorted(folder.rglob("*.private.json")) if p.parent != folder]
        return dict(version=version, reader_folder=str(folder), all_case_files=rows,
            total_files=len(rows), total_bytes=sum(r["bytes"] for r in rows),
            auxiliary_immutable_before_recovery=[dict(path=p.name, sha256=recovery.sha(p), bytes=p.stat().st_size)
                for p in sorted(folder.glob("*.private.json"))],
            paid_jev_attempts=[dict(usage_known=True,status="response_received",provider="jev",http_status=200)]*4,
            uncertain_jev_attempts=0)
    snapshot001 = root / "tmp/snapshot001.private.json"
    write(snapshot001, snapshot("din_pre_embedding_recovery_inputs_v1"))
    health001 = root / "tmp/health001.private.json"
    write(health001, dict(plan_sha256=recovery.sha(initial_plan), pg=dict(coordinator_locks=0),
        dispatch_stop_marker=False, redis=first_report["redis"], cost_audit_may_include_in_flight=first_report["costs"]))
    monkeypatch.setattr(handoff_selection,"runtime_bindings",lambda root:current001)
    monkeypatch.setattr(recovery,"verify_commit",lambda *args:None)
    auth001 = root / "tmp/auth001.private.json"
    old_auth = recovery.prepare_authorization(root=root, old_plan_path=initial_plan,
        snapshot_path=snapshot001, health_path=health001, new_commit="b"*40, output=auth001)
    old_archive = previous / "original-1048448-outcome.private.json"
    write(old_archive, first_report["records"][0])
    old_baseline = previous / "recovery-baseline.private.json"
    write(old_baseline, recovery.baseline_report(first_report))
    plan001 = root / "tmp/plan001.private.json"
    write(plan001, dict(reader_folder=str(folder),code_commit="b"*40,
                       runtime_bindings=current001,runtime_recovery=recovery.binding(auth001)))
    applied001 = previous / "recovery-applied.private.json"
    write(applied001,dict(version=recovery.VERSION,authorization_sha256=recovery.sha(auth001),
        plan_sha256=recovery.sha(plan001),archived_outcome=recovery.binding(old_archive),
        baseline=recovery.binding(old_baseline)))
    state001 = previous / "state.private.json"
    write(state001, dict(parent_pid=1234503, child_pid=1234504))
    argv = ["python","reader.py","--folder",str(folder)]
    write(previous / "reader-001.argv.private.json",dict(plan_sha256=recovery.sha(plan001),argv=argv))
    write(previous / "reader-001.exit.private.json",dict(plan_sha256=recovery.sha(plan001),argv=argv,
        exit_code=0,started_at_utc="2026-10-20T15:50:00+00:00",ended_at_utc="2026-10-20T17:30:00+00:00"))
    selected = service._selected(Path(__file__).resolve().parents[3])
    monkeypatch.setattr(service,"_selected",lambda root:selected)
    gates = {}
    for identity in recovery.METADATA_CASES:
        case = folder / str(identity)
        detail = dict(procedure_id=identity+1000000,convocatoria_id=identity,nomenclature="QA-1",
            entity="Entidad QA",object="Servicio",description="Servicio técnico QA",
            schedule=[],documents=[],document_inventory_total=0,document_inventory_complete=True,
            item_states=["Vigente"])
        gate = dict(state="submission_open",as_of="2026-10-20T17:10:00+00:00",confirmed_open=True,
            reason_codes=[],evidence=dict(windows=dict(submission=dict(end=dict(at="2026-10-21T23:59:00-05:00")))))
        write(case / "ficha-observations/002/detail.private.json",dict(detail=detail))
        gate_path = case / "participation-gate-002.private.json"
        write(gate_path,gate); gates[identity]=gate_path
        common = dict(schedule=detail["schedule"],item_states=detail["item_states"],
            document_scope=selected.SELECTED_DOCUMENTS_POLICY,document_inventory_complete=True,
            documents=selected.official_inventory(detail),detail_hash=hash_request(prepare_detail(detail)),
            document_storage_policy=selected.OFFICIAL_LINKS_POLICY,**lifecycle_fields(gate))
        common["temporal_observed_at"]="2026-10-20T15:00:00+00:00"
        write(case / "metadata.private.json",common)
    value = report(ids,9,run_id="reader",limit=10)
    value["handoff_selection"]=frozen
    value["costs"]=deepcopy(first_report["costs"])
    for row in value["records"]:
        if row["convocation_id"] in recovery.METADATA_CASES:
            row.update(accepted=True,status="needs_review",error_type="ValueError",
                error_chain=[dict(error_type="ValueError")])
        write(folder / str(row["convocation_id"]) / "outcome.private.json",row)
    value.update(accepted=3,requires_review=3)
    report_path=folder / "report-1792511770032841000.private.json"
    write(report_path,value)
    diagnosis=root / "tmp/diagnosis.private.json";write(diagnosis,dict(only_delta="temporal_observed_at"))
    history_paths = dict(initial_plan=initial_plan,initial_stopped_chain_state=initial_state,
        snapshot001=snapshot001,runtime_recovery001_plan=plan001,runtime_recovery001_authorization=auth001,
        runtime_recovery001_applied=applied001,runtime_recovery001_baseline=old_baseline,
        initial_1048448_archived_outcome=old_archive,runtime_recovery001_stopped_chain_state=state001,
        independent_metadata_collision_diagnosis=diagnosis)
    snapshot002=snapshot("din_metadata_lifecycle_recovery_inputs_v2")
    snapshot002.update(recovery_case_ids=list(recovery.METADATA_CASES),
        current_report_ref=recovery.binding(report_path),
        history001_bindings=[dict(role=role,**recovery.binding(path)) for role,path in history_paths.items()])
    snapshot_path=root / "tmp/snapshot002.private.json";write(snapshot_path,snapshot002)
    health_path=root / "tmp/health002.private.json"
    write(health_path,dict(plan_sha256=recovery.sha(plan001),pg=dict(coordinator_locks=0),
        dispatch_stop_marker=False,redis=value["redis"],cost_audit_may_include_in_flight=value["costs"]))
    monkeypatch.setattr(handoff_selection,"runtime_bindings",lambda root:current002)
    auth_path=root / "tmp/auth002.private.json"
    def prepare():
        return recovery.prepare_authorization(root=root,old_plan_path=plan001,snapshot_path=snapshot_path,
            health_path=health_path,new_commit="c"*40,output=auth_path,
            previous_recovery_folder=previous,report_path=report_path)
    auth=prepare()
    plan=dict(runtime_recovery=recovery.binding(auth_path),reader_folder=str(folder),code_commit="c"*40,
              runtime=dict(ssh_host="qa.invalid",ssh_key="/private/unused"))
    return SimpleNamespace(root=root,folder=folder,chain_folder=chain_folder,previous=previous,
        ids=ids,report=value,report_path=report_path,snapshot=snapshot002,snapshot_path=snapshot_path,
        health_path=health_path,auth=auth,auth_path=auth_path,plan=plan,imported=imported,
        frozen=frozen,prepare=prepare,history_paths=history_paths,gates=gates,old_auth=old_auth)


class MetadataRedis:
    """Faithful decoded state and full CAS argv comparison, without Redis IO."""
    def __init__(self,e):
        terminal={str(r["convocation_id"]) for r in e.report["records"]}
        queued=[item for item in e.imported["items"] if str(item["row"]["convocation_id"]) not in terminal]
        self.snap=dict(dedup={str(i):"done" if str(i) in terminal else "queued" for i in e.ids},
            payloads={str(i["row"]["convocation_id"]):i for i in queued},
            queue=[str(i["row"]["convocation_id"]) for i in queued],pending=[],stats=deepcopy(e.report["redis"]))
        self.marker=None;self.calls=0;self.lose_reply=False;self.fail_before=False
        self.corrupt_after=False;self.corrupt_before_payload=False
    def close(self):pass
    def get(self,key):return self.marker
    def hgetall(self,key):
        data={k:chain._canonical(v).decode() for k,v in self.snap["payloads"].items()}
        if self.corrupt_before_payload:data[next(iter(data))]='{"changed":true}'
        return data
    def eval(self,script,count,*args):
        assert script==recovery.CAS_METADATA and count==5
        self.calls+=1
        keys,marker,entries,expected=args[:5],*args[5:]
        raw=json.loads(expected)
        assert raw==[self.snap["dedup"],self.hgetall(keys[1]),self.snap["queue"],self.snap["pending"]]
        rows=json.loads(entries)
        assert [r["id"] for r in rows]==[str(i) for i in recovery.METADATA_CASES]
        assert all(self.snap["dedup"][r["id"]]=="done" and r["id"] not in self.snap["payloads"] for r in rows)
        if self.fail_before:self.fail_before=False;raise ConnectionError("simulated pre-CAS interruption")
        for row in rows:
            self.snap["dedup"][row["id"]]="queued"
            self.snap["payloads"][row["id"]]=json.loads(row["payload"])
        self.snap["queue"]=[r["id"] for r in rows]+self.snap["queue"]
        self.snap["stats"].update(queued=self.snap["stats"]["queued"]+3,payloads=self.snap["stats"]["payloads"]+3)
        self.marker=marker
        if self.corrupt_after:self.snap["queue"].reverse()
        if self.lose_reply:self.lose_reply=False;raise ConnectionError("simulated lost CAS reply")
        return 1


@pytest.fixture
def metadata_application(metadata_evidence,monkeypatch):
    e=metadata_evidence;client=MetadataRedis(e);conn=SimpleNamespace(locked=False)
    def execute(sql):
        if sql.startswith("SHOW"):value=dict(transaction_read_only="on")
        elif "pg_try_advisory_lock" in sql:conn.locked=True;value=dict(locked=True)
        elif "pg_advisory_unlock" in sql:conn.locked=False;value={}
        else:raise AssertionError(sql)
        return SimpleNamespace(fetchone=lambda:value)
    conn.execute=execute
    @contextmanager
    def connect(*args,**kwargs):
        assert "default_transaction_read_only=on" in kwargs["options"]
        yield conn
    @contextmanager
    def settings(**kwargs):yield SimpleNamespace(database_url="unused-qa"),"unused-qa"
    from buenapro_worker.prod2 import runtime,selected_payments
    monkeypatch.setattr(runtime,"production_staging_settings",settings)
    monkeypatch.setattr(selected_payments,"audit_summary",lambda folder:deepcopy(e.report["costs"]))
    monkeypatch.setattr(chain,"_pg_snapshot",lambda conn,run:(int(conn.locked),[]))
    monkeypatch.setattr(chain,"validate_members",lambda *args:[])
    monkeypatch.setattr(chain,"_redis_snapshot",lambda client,run:deepcopy(client.snap))
    monkeypatch.setattr(recovery.os,"kill",lambda *args:(_ for _ in ()).throw(ProcessLookupError()))
    def apply():
        return recovery.apply_recovery(e.plan,e.imported,e.chain_folder,root=e.root,
            plan_sha256="f"*64,go=True,connect=connect,redis_factory=lambda *a,**k:client)
    return e,client,conn,apply


def test_metadata_authorization_binds_all_three_and_full_previous_history(metadata_evidence):
    e=metadata_evidence
    assert e.auth["version"]==recovery.METADATA_VERSION
    assert e.auth["convocation_ids"]==list(recovery.METADATA_CASES)
    assert [r["convocation_id"] for r in e.auth["failed_outcomes"]]==list(recovery.METADATA_CASES)
    assert len(e.auth["chain_history"])==12
    value,frozen=recovery.authorization(e.auth_path,recovery.sha(e.auth_path),root=e.root,reader_folder=e.folder)
    assert value==e.auth and frozen==e.frozen
    assert all(p["changed_fields"]==["temporal_observed_at"] for p in value["metadata_timestamp_proofs"])


def test_metadata_baseline_preserves_other_six_and_four_known_payments(metadata_evidence):
    e=metadata_evidence;before=deepcopy(e.report)
    baseline=recovery.baseline_report(e.report,recovery.METADATA_VERSION)
    assert e.report==before
    assert baseline["processed"]==6 and baseline["redis"]==dict(queued=151,pending=0,payloads=151,seen=157)
    assert baseline["records"]==[r for r in before["records"] if r["convocation_id"] not in recovery.METADATA_CASES]
    assert baseline["costs"]==before["costs"]
    assert baseline["handoff_selection"]==before["handoff_selection"]


@pytest.mark.parametrize("mutation",["other_id","error","sent_embedding","uncertain","missing_case"])
def test_metadata_baseline_rejects_nonlocal_or_unscoped_failures(metadata_evidence,mutation):
    value=deepcopy(metadata_evidence.report)
    if mutation=="other_id":value["records"][0]["convocation_id"]=999
    elif mutation=="error":value["records"][0]["error_type"]="EmbeddingQueryReview"
    elif mutation=="sent_embedding":value["costs"]["embedding_attempts"]=1
    elif mutation=="uncertain":value["costs"]["usage_unknown_attempts"]=1
    else:value["records"].pop()
    with pytest.raises((recovery.RecoveryError,chain.ChainError)):
        recovery.baseline_report(value,recovery.METADATA_VERSION)


@pytest.mark.parametrize("field,value",[("detail_hash","f"*64),("schedule",[dict(stage="changed")]),
    ("documents",[dict(document_uuid="changed")]),("confirmed_open",False),("closes_at","2026-10-22T23:59:00-05:00"),
    ("temporal_reason_codes",["changed"]),("temporal_observed_at","2026-10-20T18:00:00+00:00")])
def test_metadata_proof_rejects_any_static_or_nonnewer_timestamp_delta(metadata_evidence,field,value):
    e=metadata_evidence;path=e.folder / str(recovery.CASE) / "metadata.private.json"
    original=json.loads(path.read_bytes());original[field]=value;write(path,original)
    with pytest.raises(recovery.RecoveryError,match="delta_not_timestamp_only|timestamp_not_newer"):
        recovery.metadata_timestamp_proof(e.folder,recovery.CASE,root=e.root)


@pytest.mark.parametrize("mutation",["case_scope","failed_scope","history","proof_capture","newer_capture","cached_ocr"])
def test_metadata_authorization_or_apply_never_adopts_changed_evidence(metadata_application,mutation):
    e,client,_,apply=metadata_application
    if mutation=="case_scope":e.auth["convocation_ids"][1]=999
    elif mutation=="failed_scope":e.auth["failed_outcomes"][1]["convocation_id"]=999
    elif mutation=="history":write(e.history_paths["runtime_recovery001_applied"],dict(changed=True))
    elif mutation=="proof_capture":write(e.gates[recovery.CASE],dict(changed=True))
    elif mutation=="newer_capture":
        path=e.folder / str(recovery.CASE) / "metadata.private.json"
        obj=json.loads(path.read_bytes());obj["temporal_observed_at"]="2026-10-20T19:00:00+00:00";write(path,obj)
    else:write(e.folder / str(recovery.CASE) / "ocr-page-0001.private.json",dict(text_sha256="changed"))
    if mutation in {"case_scope","failed_scope"}:
        write(e.auth_path,e.auth);e.plan["runtime_recovery"]=recovery.binding(e.auth_path)
    with pytest.raises((recovery.RecoveryError,chain.ChainError)):
        apply()
    assert client.calls==0


def test_metadata_recovery_archives_exact_three_and_prioritizes_without_repaying(metadata_application):
    e,client,conn,apply=metadata_application
    original={p:p.read_bytes() for p in e.folder.rglob("*.private.json")}
    history={p:p.read_bytes() for p in e.history_paths.values()}
    remaining=list(client.snap["queue"])
    receipt=apply()
    assert client.calls==1 and not conn.locked
    assert receipt["redis_requeued_ids"]==list(recovery.METADATA_CASES)
    assert client.snap["queue"]==[str(i) for i in recovery.METADATA_CASES]+remaining
    assert len(client.snap["queue"])==len(set(client.snap["queue"]))==151
    assert receipt["preserved_costs"]==e.report["costs"]
    assert receipt["model_calls"]==receipt["source_calls"]==receipt["pg_writes"]==0
    for identity in recovery.METADATA_CASES:
        old=e.folder / str(identity) / "outcome.private.json"
        assert not old.exists()
        archive=e.chain_folder / f"original-{identity}-outcome.private.json"
        assert archive.read_bytes()==original.pop(old)
        assert archive.stat().st_mode & 0o777==0o600
    assert all(path.read_bytes()==data for path,data in original.items())
    assert all(path.read_bytes()==data for path,data in history.items())
    e.plan["_recovery_plan_sha256"]="f"*64
    baseline=recovery.validate_applied(e.plan,e.chain_folder,root=e.root)
    assert json.loads(baseline.read_bytes())["processed"]==6


@pytest.mark.parametrize("interruption",["before_cas","lost_reply"])
def test_metadata_partial_apply_reconciles_exact_evidence_without_duplicate_queue(metadata_application,interruption):
    e,client,conn,apply=metadata_application
    client.fail_before=interruption=="before_cas";client.lose_reply=interruption=="lost_reply"
    with pytest.raises(ConnectionError):apply()
    assert not conn.locked and not (e.chain_folder / "recovery-applied.private.json").exists()
    assert all((e.chain_folder / f"original-{i}-outcome.private.json").exists() for i in recovery.METADATA_CASES)
    assert all(not (e.folder / str(i) / "outcome.private.json").exists() for i in recovery.METADATA_CASES)
    receipt=apply()
    assert receipt["redis_requeued_ids"]==list(recovery.METADATA_CASES)
    assert client.calls==(2 if interruption=="before_cas" else 1)
    assert client.snap["queue"][:3]==[str(i) for i in recovery.METADATA_CASES]
    assert len(client.snap["queue"])==len(set(client.snap["queue"]))==151


@pytest.mark.parametrize("change",["payload_before","order_after","other_outcome","uncertain_audit"])
def test_metadata_apply_fails_closed_on_payload_cost_or_unrelated_evidence_change(metadata_application,monkeypatch,change):
    e,client,_,apply=metadata_application
    if change=="payload_before":client.corrupt_before_payload=True
    elif change=="order_after":client.corrupt_after=True
    elif change=="other_outcome":write(e.folder / "1/outcome.private.json",dict(changed=True))
    else:
        from buenapro_worker.prod2 import selected_payments
        monkeypatch.setattr(selected_payments,"audit_summary",lambda _:costs(usage_unknown_attempts=1))
    with pytest.raises((recovery.RecoveryError,chain.ChainError)):
        apply()
    assert not (e.chain_folder / "recovery-applied.private.json").exists()
    if change!="order_after":assert client.calls==0
