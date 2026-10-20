"""Offline identity, immutable-evidence and one-item recovery regressions."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import din_sector_chain as chain
from buenapro_worker.prod2 import handoff_runtime_recovery as recovery
from buenapro_worker.prod2 import handoff_selection
from test_din_sector_chain import report, costs, write


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    root = tmp_path
    folder = root / "tmp/work/runs/reader"
    old_state = root / "tmp/work/chain/state.private.json"
    chain_folder = root / "tmp/new-chain"
    ids = [recovery.CASE, *range(1, 21)]
    old_runtime = {name: "a" * 64 for name in recovery.ALLOWED_RUNTIME_FILES
                   if not name.endswith("handoff_runtime_recovery.py")}
    current = {name: "b" * 64 for name in recovery.ALLOWED_RUNTIME_FILES}
    frozen = dict(scope_id=recovery.SCOPE, candidate_ids=ids, runtime_bindings=old_runtime,
                  items_sha256="c" * 64)
    imported = dict(binding={**frozen, "runtime_bindings": current},
                    items=[dict(row=dict(convocation_id=i)) for i in ids])
    value = report(ids, 6, run_id="reader", limit=10)
    value["handoff_selection"] = frozen
    value["records"][0].update(accepted=True, status="needs_review", error_type="AttributeError",
                              error_chain=[dict(error_type="AttributeError")])
    value.update(accepted=1, requires_review=1)
    value["costs"] = costs(attempts=4, jev_calls=4, known_cost_usd=.001)
    manifest = dict(handoff_selection=frozen, requested=dict(handoff_selection=frozen), run_id="reader")
    write(folder / "manifest.private.json", manifest)
    write(folder / "report-001.private.json", value)
    for row in value["records"]:
        write(folder / str(row["convocation_id"]) / "outcome.private.json", row)
    write(folder / str(recovery.CASE) / "ocr-page-0001.private.json", dict(text_sha256="d"*64))
    write(old_state, dict(parent_pid=1234501, child_pid=1234502))
    old_plan_path = root / "tmp/old-plan.private.json"
    write(old_plan_path, dict(reader_folder=str(folder), code_commit="a"*40))
    snapshot = dict(version="din_pre_embedding_recovery_inputs_v1", reader_folder=str(folder),
        all_case_files=[dict(path=str(p.relative_to(folder)), sha256=recovery.sha(p), bytes=p.stat().st_size)
                        for p in sorted(folder.glob("*/*.private.json"))],
        auxiliary_immutable_before_recovery=[dict(path=p.name, sha256=recovery.sha(p), bytes=p.stat().st_size)
            for p in (folder / "manifest.private.json", folder / "report-001.private.json")],
        paid_jev_attempts=[dict(usage_known=True, status="response_received", provider="jev", http_status=200)]*4,
        uncertain_jev_attempts=0)
    snapshot.update(total_files=len(snapshot["all_case_files"]),
                    total_bytes=sum(r["bytes"] for r in snapshot["all_case_files"]))
    snapshot_path = root / "tmp/snapshot.private.json"; write(snapshot_path, snapshot)
    health = dict(plan_sha256=recovery.sha(old_plan_path), pg=dict(coordinator_locks=0),
        dispatch_stop_marker=False, redis=value["redis"], cost_audit_may_include_in_flight=value["costs"])
    health_path = root / "tmp/health.private.json"; write(health_path, health)
    monkeypatch.setattr(handoff_selection, "runtime_bindings", lambda root: current)
    monkeypatch.setattr(recovery, "verify_commit", lambda *args: None)
    auth_path = root / "tmp/auth.private.json"
    auth = recovery.prepare_authorization(root=root, old_plan_path=old_plan_path, snapshot_path=snapshot_path,
        health_path=health_path, new_commit="b"*40, output=auth_path)
    plan = dict(runtime_recovery=recovery.binding(auth_path), reader_folder=str(folder),
                code_commit="b"*40, runtime=dict(ssh_host="qa.invalid", ssh_key="/private/unused"))
    return SimpleNamespace(root=root, folder=folder, chain_folder=chain_folder, ids=ids, report=value,
        snapshot=snapshot, snapshot_path=snapshot_path, health_path=health_path, auth=auth, auth_path=auth_path,
        plan=plan, imported=imported, frozen=frozen, current=current)


def test_external_binding_adapts_only_runtime_and_never_rewrites_originals(evidence, monkeypatch):
    e = evidence
    before = {p: p.read_bytes() for p in e.folder.rglob("*.private.json")}
    monkeypatch.setattr(handoff_selection, "load_selection", lambda *a, **k: e.imported)
    result = recovery.load_selection("h", "s", "c"*64, root=e.root,
        recovery_path=e.auth_path, recovery_sha256=recovery.sha(e.auth_path), reader_folder=e.folder)
    assert result["binding"] == e.frozen and result["items"] == e.imported["items"]
    assert all(p.read_bytes() == data for p, data in before.items())
    assert recovery.report_context(e.auth_path, recovery.sha(e.auth_path), root=e.root,
        reader_folder=e.folder)["runtime_bindings"] == e.current
    assert e.auth_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["items_sha256", "candidate_ids", "scope_id"])
def test_no_adapter_can_adopt_changed_paid_or_source_inputs(evidence, monkeypatch, change):
    e = evidence; altered = deepcopy(e.imported)
    altered["binding"][change] = [] if change == "candidate_ids" else "changed"
    monkeypatch.setattr(handoff_selection, "load_selection", lambda *a, **k: altered)
    with pytest.raises(recovery.RecoveryError, match="paid_or_source"):
        recovery.load_selection("h", "s", "c"*64, root=e.root,
            recovery_path=e.auth_path, recovery_sha256=recovery.sha(e.auth_path), reader_folder=e.folder)


@pytest.mark.parametrize("change", ["other_id", "runtime", "manifest", "uncertain", "llm1", "embedding", "other_error"])
def test_authorization_rejects_other_identity_runtime_or_paid_state(evidence, change):
    e = evidence
    if change == "other_id": e.auth["convocation_id"] += 1
    elif change == "runtime": e.current["prompts/paid.md"] = "f"*64
    elif change == "manifest": write(e.folder / "manifest.private.json", {})
    else:
        value = deepcopy(e.report)
        if change == "uncertain": value["costs"]["usage_unknown_attempts"] = 1
        elif change == "other_error": value["records"][0]["error_type"] = "EmbeddingQueryReview"
        else: value["costs"][change + "_calls"] = 1
        write(e.folder / "report-001.private.json", value)
        e.auth["old_report"] = recovery.binding(e.folder / "report-001.private.json")
    write(e.auth_path, e.auth)
    with pytest.raises((recovery.RecoveryError, chain.ChainError)):
        recovery.authorization(e.auth_path, recovery.sha(e.auth_path), root=e.root, reader_folder=e.folder)


def test_baseline_is_new_and_retains_four_known_attempts_and_other_five(evidence):
    e = evidence; original = deepcopy(e.report)
    baseline = recovery.baseline_report(e.report)
    assert e.report == original
    assert baseline["processed"] == 5 and baseline["redis"]["queued"] == 16
    assert baseline["records"] == original["records"][1:]
    assert baseline["costs"] == original["costs"]
    assert baseline["handoff_guards"] == original["handoff_guards"]
    assert baseline["handoff_selection"] == original["handoff_selection"]
    assert chain.assess_batch(baseline, "reader", e.ids, chain.budget(1, len(e.ids)))["processed"] == 5


class FakeRedis:
    def __init__(self, e):
        terminal = {str(row["convocation_id"]) for row in e.report["records"]}
        queued = [item for item in e.imported["items"] if str(item["row"]["convocation_id"]) not in terminal]
        self.snap = dict(dedup={str(i): "done" if str(i) in terminal else "queued" for i in e.ids},
            payloads={str(item["row"]["convocation_id"]):item for item in queued},
            queue=[str(item["row"]["convocation_id"]) for item in queued], pending=[], stats=e.report["redis"])
        self.marker = None; self.calls = 0; self.lose_reply = False; self.corrupt_after = False
    def close(self): pass
    def get(self, key): return self.marker
    def hgetall(self, key): return {k: chain._canonical(v).decode() for k,v in self.snap["payloads"].items()}
    def eval(self, script, count, *args):
        assert script == recovery.CAS and count == 5
        self.calls += 1
        keys, marker, identity, payload, expected = args[:5], *args[5:]
        assert identity == str(recovery.CASE)
        raw = json.loads(expected)
        assert raw == [self.snap["dedup"], self.hgetall(keys[1]), self.snap["queue"], self.snap["pending"]]
        assert self.snap["dedup"][identity] == "done" and identity not in self.snap["payloads"]
        self.snap["dedup"][identity] = "queued"; self.snap["payloads"][identity] = json.loads(payload)
        self.snap["queue"].insert(0, identity)
        self.snap["stats"] = {**self.snap["stats"], "queued":self.snap["stats"]["queued"]+1,
                              "payloads":self.snap["stats"]["payloads"]+1}
        self.marker = marker
        if self.corrupt_after: self.snap["queue"].reverse()
        if self.lose_reply: self.lose_reply = False; raise ConnectionError("simulated lost CAS reply")
        return 1


@pytest.fixture
def application(evidence, monkeypatch):
    e = evidence; client = FakeRedis(e)
    conn = SimpleNamespace(locked=False)
    def execute(sql):
        if sql.startswith("SHOW"): value = dict(transaction_read_only="on")
        elif "pg_try_advisory_lock" in sql: conn.locked=True; value=dict(locked=True)
        elif "pg_advisory_unlock" in sql: conn.locked=False; value={}
        else: raise AssertionError(sql)
        return SimpleNamespace(fetchone=lambda:value)
    conn.execute = execute
    @contextmanager
    def connect(*args, **kwargs):
        assert "default_transaction_read_only=on" in kwargs["options"]
        yield conn
    @contextmanager
    def settings(**kwargs): yield SimpleNamespace(database_url="unused-qa"), "unused-qa"
    from buenapro_worker.prod2 import runtime, selected_payments
    monkeypatch.setattr(runtime, "production_staging_settings", settings)
    monkeypatch.setattr(selected_payments, "audit_summary", lambda folder: deepcopy(e.report["costs"]))
    monkeypatch.setattr(chain, "_pg_snapshot", lambda conn, run:(int(conn.locked), []))
    monkeypatch.setattr(chain, "validate_members", lambda *args: [])
    monkeypatch.setattr(chain, "_redis_snapshot", lambda client, run:deepcopy(client.snap))
    monkeypatch.setattr(recovery.os, "kill", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    frozen = {**e.imported, "binding":e.frozen}
    def apply():
        return recovery.apply_recovery(e.plan, frozen, e.chain_folder, root=e.root,
            plan_sha256="d"*64, go=True, connect=connect, redis_factory=lambda *a, **k:client)
    return e, client, conn, apply


def test_only_1048448_is_archived_and_cas_requeued_original_payload(application):
    e, client, conn, apply = application
    files = {p:p.read_bytes() for p in e.folder.rglob("*.private.json")}
    old = e.folder / str(recovery.CASE) / "outcome.private.json"
    receipt = apply()
    assert not old.exists() and not conn.locked and client.calls == 1
    _, baseline, archive = recovery.recovery_paths(e.chain_folder)
    assert archive.read_bytes() == files.pop(old)
    assert all(p.read_bytes() == data for p,data in files.items())
    assert client.snap["queue"][0] == str(recovery.CASE)
    assert client.snap["queue"][1:] == [str(i) for i in e.ids[6:]]
    assert receipt["queue_priority"] == "recovered_pre_embedding_case_first_other_relative_order_preserved"
    assert client.snap["payloads"][str(recovery.CASE)] == e.imported["items"][0]
    assert receipt["preserved_costs"] == e.report["costs"]
    assert recovery.validate_applied({**e.plan, "_recovery_plan_sha256":"d"*64}, e.chain_folder, root=e.root) == baseline
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in e.chain_folder.glob("*.json"))


def test_lost_cas_response_reconciles_same_marker_without_second_enqueue(application):
    e, client, conn, apply = application; client.lose_reply = True
    with pytest.raises(ConnectionError): apply()
    assert not conn.locked and client.calls == 1
    receipt = apply()
    assert client.calls == 1 and client.snap["queue"].count(str(recovery.CASE)) == 1
    assert receipt["redis_requeued_ids"] == [recovery.CASE]


def test_archive_entry_is_durable_before_unlink_and_cas(application, monkeypatch):
    e, client, conn, apply = application
    events = []; fsync = recovery.fsync_directory; unlink = Path.unlink; evaluate = client.eval
    old = e.folder / str(recovery.CASE) / "outcome.private.json"
    def sync(path):
        events.append(("directory_fsync", Path(path)))
        return fsync(path)
    def remove(path, *args, **kwargs):
        if path == old: events.append(("unlink", path))
        return unlink(path, *args, **kwargs)
    def cas(*args):
        events.append(("cas", None)); return evaluate(*args)
    monkeypatch.setattr(recovery, "fsync_directory", sync)
    monkeypatch.setattr(Path, "unlink", remove)
    monkeypatch.setattr(client, "eval", cas)
    apply()
    removed = events.index(("unlink", old)); dispatched = events.index(("cas", None))
    assert ("directory_fsync", e.chain_folder) in events[:removed]
    assert ("directory_fsync", old.parent) in events[removed+1:dispatched]


@pytest.mark.parametrize("bad", ["cached_ocr", "other_outcome", "payload", "uncertain", "guard", "live_pid", "post_order"])
def test_any_changed_cache_payment_other_id_payload_or_live_owner_blocks(application, monkeypatch, bad):
    e, client, conn, apply = application
    if bad == "cached_ocr": write(e.folder / str(recovery.CASE) / "ocr-page-0001.private.json", {})
    elif bad == "other_outcome": write(e.folder / "1/outcome.private.json", {})
    elif bad == "payload": client.snap["payloads"]["6"] = dict(row=dict(convocation_id=999))
    elif bad == "guard": write(e.folder / "handoff-guards/dispatch-stop.private.json", {})
    elif bad == "live_pid": monkeypatch.setattr(recovery.os, "kill", lambda *a:None)
    elif bad == "post_order": client.corrupt_after = True
    else:
        from buenapro_worker.prod2 import selected_payments
        monkeypatch.setattr(selected_payments, "audit_summary", lambda p:costs(usage_unknown_attempts=1))
    with pytest.raises((recovery.RecoveryError, chain.ChainError)): apply()
    assert not conn.locked
    if bad != "post_order": assert client.calls == 0


def test_go_and_new_plan_are_required_before_any_mutation(application):
    e, client, conn, _ = application
    with pytest.raises(recovery.RecoveryError, match="explicit_go"):
        recovery.apply_recovery(e.plan, e.imported, e.chain_folder, root=e.root, plan_sha256="d"*64)
    assert client.calls == 0


def test_reader_argv_includes_explicit_recovery_pair_only_when_requested(evidence):
    e = evidence; base = dict(reader_folder=str(e.folder), mapping_handoff=dict(path="h"),
        selection=dict(path="s",sha256="a"*64), embedding_config=dict(path="v5"),runtime=e.plan["runtime"])
    assert "--runtime-recovery" not in chain.reader_argv(base,e.root,chain.budget(1,21))
    argv = chain.reader_argv({**base,"runtime_recovery":e.plan["runtime_recovery"]},e.root,chain.budget(1,21))
    assert argv[argv.index("--runtime-recovery")+1] == str(e.auth_path)
    assert argv[argv.index("--runtime-recovery-sha256")+1] == recovery.sha(e.auth_path)


def test_fresh_readback_accepts_only_exact_applied_baseline_outside_reader(application, monkeypatch):
    e, client, conn, apply = application
    apply()
    _, baseline, _ = recovery.recovery_paths(e.chain_folder)
    plan = {**e.plan, "_recovery_plan_sha256":"d"*64, "_recovery_chain_folder":str(e.chain_folder),
            "selection":dict(sha256="c"*64), "extra_batches":8}
    from buenapro_worker.prod2 import runtime
    import psycopg, redis
    @contextmanager
    def connect(*args, **kwargs):
        assert "default_transaction_read_only=on" in kwargs["options"]
        assert kwargs["application_name"] == "buenapro-din-sector-readback"
        def execute(sql):
            if sql == "SHOW transaction_read_only":
                return SimpleNamespace(fetchone=lambda:dict(transaction_read_only="on"))
            assert sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        yield SimpleNamespace(execute=execute, rollback=lambda:None, locked=False)
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k:client)
    imported = {**e.imported, "binding":e.frozen}
    fresh = chain.fresh_readback(plan, imported, baseline, expected_limits=chain.budget(1,len(e.ids)))
    assert fresh["report_sha256"] == recovery.sha(baseline)
    assert fresh["outcome_ids"] == sorted(e.ids[1:6])
    assert recovery.CASE in fresh["remaining_ids"] and fresh["redis"]["queued"] == 16
    new_report = e.folder / "report-new.private.json"
    data = chain._read(baseline)
    write(new_report, data)
    with pytest.raises(chain.ChainError, match="recovery_execution_runtime_not_reported"):
        chain.fresh_readback(plan, imported, new_report, expected_limits=chain.budget(1,len(e.ids)))
    data["runtime_recovery"] = recovery.report_context(e.auth_path, recovery.sha(e.auth_path),
        root=e.root, reader_folder=e.folder)
    write(new_report, data)
    assert chain.fresh_readback(plan, imported, new_report,
        expected_limits=chain.budget(1,len(e.ids)))["report_sha256"] == recovery.sha(new_report)
    outsider = e.chain_folder / "identical-copy.private.json"
    outsider.write_bytes(baseline.read_bytes())
    with pytest.raises(chain.ChainError, match="report_outside_reader_folder"):
        chain.fresh_readback(plan, imported, outsider, expected_limits=chain.budget(1,len(e.ids)))
