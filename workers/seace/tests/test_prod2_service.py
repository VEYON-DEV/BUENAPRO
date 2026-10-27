"""Private synthetic service contracts; no source, PG, Redis or paid calls."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal

import pytest

from buenapro_worker.prod2 import service

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)


@pytest.fixture
def paths(tmp_path):
    root = tmp_path.resolve()
    state = root / "state"
    config = root / "service.private.json"
    return config, state


def write(path, value):
    path.write_bytes(service._canonical(value))
    path.chmod(0o600)


def configuration(*, max_cycles=1):
    return {"version": 1, "enabled": True, "first_epoch_id": "synthetic-explicit",
        "interval_seconds": 60, "max_cycles": max_cycles,
        "window": {"policy": "rolling_days", "days": 7, "first_to": "2026-10-19", "zone": "America/Lima"},
        "base_argv": ["--local", "--preserve-prior-readings", "--keyword", "software",
            "--document-storage", "official", "--llm1-input", "embeddings",
            "--llm1-scope", "qualification", "--llm1-model-routing", "pages20",
            "--llm1-fallback", "none", "--embedding-config",
            str(ROOT / "docs/sources/prod2-embedding-reading.runtime.v5.json"),
            "--years", "2026", "--max-accepted", "2", "--max-candidates", "4",
            "--max-jev-calls", "8", "--max-llm1-calls", "4", "--max-embedding-calls", "8",
            "--search-workers", "1", "--processing-workers", "1", "--model-workers", "1",
            "--request-interval", "2"]}


def report(epoch):
    return {"run_id": epoch, "pipeline": "prod2_selected_llm1_v1", "terminal_stage": "llm1",
        "source_coverage_complete": True, "queries_total": 1, "queries_complete": 1,
        "coverage": [{"coverage_complete": True, "errors": []}], "scope_proofs": [],
        "redis": {"queued": 0, "pending": 0, "payloads": 0, "seen": 1},
        "processed": 1, "accepted": 1, "llm1_complete": 1, "requires_review": 0,
        "records": [{"status": "llm1_complete"}], "embedding_dispatch_stop_requested": False,
        "costs": {key: 0 for key in ("usage_unknown_attempts", "embedding_unknown_dispatch_attempts",
            "embedding_usage_unknown_attempts", "embedding_receipt_conflicts")}}


class Clock:
    def __init__(self):
        self.current = NOW
        self.stopped = False
        self.on_wait = lambda: None

    def now(self):
        return self.current

    def wait(self, seconds):
        self.on_wait()
        self.current += timedelta(seconds=seconds)
        return self.stopped

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True


def fake_children(monkeypatch, *, mutate=lambda value: None, exit_code=0, no_report=False,
                  interrupt=False):
    children = []

    class Child:
        pid = 424242

        def __init__(self, argv, **kwargs):
            assert kwargs["cwd"] == ROOT
            self.signals = []
            self.interrupt = interrupt
            self.returncode = None if interrupt else exit_code
            folder = Path(argv[argv.index("--folder") + 1])
            plan = service._read(folder / "plan.private.json")
            write(folder / "manifest.private.json", {"run_id": plan["epoch_id"],
                "requested": plan["requested"], "published_from": plan["published_from"],
                "published_to": plan["published_to"]})
            value = report(plan["epoch_id"])
            mutate(value)
            if not no_report:
                write(folder / "report-synthetic.private.json", value)
            self.folder = folder
            children.append(self)

        def poll(self):
            if self.interrupt:
                self.interrupt = False
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return self.returncode

        def send_signal(self, sig):
            self.signals.append(sig)
            self.returncode = 0

        def wait(self):
            return self.returncode

    monkeypatch.setattr(service.subprocess, "Popen", Child)
    return children


@pytest.mark.parametrize("value,status", [({"version": 1, "operator_hold": "source_403"}, "blocked_source"),
    ({"version": 1, "enabled": False}, "prepared")])
def test_hold_has_no_ingestion_import_or_child(paths, monkeypatch, value, status):
    config, state = paths
    write(config, value)
    monkeypatch.setattr(service, "_selected", lambda *_: pytest.fail("No ingestion dependencies in hold"))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *_a, **_k: pytest.fail("No child in hold"))
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    saved = service._read(state / "state.private.json")
    assert saved["status"] == status
    assert not (state / "runs").exists()


@pytest.mark.parametrize("value", [{}, {"version": 2, "operator_hold": "source_403"},
    {"version": 1}, {"version": 1, "enabled": True},
    {"version": 1, "operator_hold": "source_403", "unknown": True}])
def test_missing_or_incomplete_configuration_never_starts(paths, monkeypatch, value):
    config, state = paths
    write(config, value)
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []


def test_absent_configuration_records_attention_without_child(paths, monkeypatch):
    config, state = paths
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []
    assert service._read(state / "state.private.json")["status"] == "attention_required"


def test_selected_service_explicit_taxonomy_flag_is_bound_into_epoch_without_automatic_activation(paths, monkeypatch):
    config, state = paths
    value = configuration()
    value["base_argv"] += ["--llm1-commercial-taxonomy", "commercial_taxonomy_v1"]
    write(config, value)
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert len(children) == 1
    plan = service._read(children[0].folder / "plan.private.json")
    assert plan["requested"]["llm1_commercial_taxonomy"] == "commercial_taxonomy_v1"
    assert "--llm1-commercial-taxonomy" in plan["argv"]
    assert service._read(children[0].folder / "manifest.private.json")["requested"] == plan["requested"]


def test_configuration_and_state_symlinks_are_rejected_even_in_hold(paths):
    config, state = paths
    target = config.with_name("target.private.json")
    write(target, {"version": 1, "operator_hold": "source_403"})
    config.symlink_to(target)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    config.unlink()
    write(config, {"version": 1, "operator_hold": "source_403"})
    linked = state.with_name("linked")
    linked.symlink_to(state, target_is_directory=True)
    with pytest.raises(service.ServiceError, match="symlinked"):
        service.run(config, linked, project_root=ROOT)


def test_one_explicit_epoch_finishes_and_restarting_does_not_repeat(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert len(children) == 1
    plan = service._read(children[0].folder / "completion.private.json")
    assert plan["published_from"] == "2026-10-13" and plan["published_to"] == "2026-10-19"
    assert plan["argv_sha256"] == service._hash(plan["argv"])
    assert plan["requested"]["keywords"] == ["software"]
    for path in state.rglob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_next_epoch_requires_completed_evidence_and_explicit_cycle_budget(paths, monkeypatch):
    config, state = paths
    write(config, configuration(max_cycles=2))
    children = fake_children(monkeypatch)
    clock = Clock()
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert len(children) == 2
    assert children[0].folder != children[1].folder
    assert clock.current == NOW + timedelta(seconds=60)


def test_completed_state_revalidates_immutable_evidence_before_success(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    path = children[0].folder / "report-synthetic.private.json"
    changed = service._read(path)
    changed["synthetic_evidence_changed"] = True
    write(path, changed)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert len(children) == 1
    assert service._read(state / "state.private.json")["reason"] == "completed_epoch_evidence_changed"


def test_checkpoint_failure_after_popen_drains_owned_child_without_retry(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    events = []

    class OwnedChild:
        pid = 424242
        returncode = None

        def __init__(self, *_args, **_kwargs):
            events.append("started")

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            events.append(sig)
            self.returncode = 0

        def wait(self):
            events.append("waited")
            return self.returncode

    monkeypatch.setattr(service.subprocess, "Popen", OwnedChild)
    original_write = service._write
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path.name == "state.private.json" and value.get("status") == "running" and not failed:
            failed = True
            raise OSError("private synthetic error must not reach logs")
        return original_write(path, value)

    monkeypatch.setattr(service, "_write", fail_once)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert events == ["started", signal.SIGTERM, "waited"]
    saved = service._read(state / "state.private.json")
    assert saved["status"] == "attention_required" and saved["reason"] == "OSError"
    assert "private synthetic" not in json.dumps(saved)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert events == ["started", signal.SIGTERM, "waited"]


@pytest.mark.parametrize("fault", ["source403", "queue", "unknown_payment", "dispatch", "guard", "no_report"])
def test_unsafe_completion_stops_without_second_child_or_restart(paths, monkeypatch, fault):
    config, state = paths
    write(config, configuration(max_cycles=2))

    def mutate(value):
        if fault == "source403":
            value["coverage"][0]["errors"] = [{"code": "source_403"}]
        elif fault == "queue":
            value["redis"]["pending"] = 1
        elif fault == "unknown_payment":
            value["costs"]["usage_unknown_attempts"] = 1
        elif fault == "dispatch":
            value["embedding_dispatch_stop_requested"] = True

    children = fake_children(monkeypatch, mutate=mutate, no_report=fault == "no_report")
    if fault == "guard":
        original = service._gate

        def guarded(plan):
            write(Path(plan["folder"]) / "guard.private.json", {"status": "open"})
            return original(plan)

        monkeypatch.setattr(service, "_gate", guarded)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert len(children) == 1
    assert service._read(state / "state.private.json")["status"] == "attention_required"


def test_signal_forwards_only_to_the_owned_child_and_never_restarts(paths, monkeypatch):
    config, state = paths
    write(config, configuration(max_cycles=2))
    children = fake_children(monkeypatch, interrupt=True)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert len(children) == 1 and children[0].signals == [signal.SIGTERM]
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert len(children) == 1


def test_second_instance_does_not_overwrite_active_state(paths):
    config, state = paths
    write(config, {"version": 1, "operator_hold": "source_403"})
    state.mkdir(mode=0o700)
    write(state / "state.private.json", {"status": "running", "child_pid": 111})
    before = (state / "state.private.json").read_bytes()
    with service._lock(state), pytest.raises(BlockingIOError):
        service.run(config, state, project_root=ROOT)
    assert (state / "state.private.json").read_bytes() == before


@pytest.mark.parametrize("extra", [["--all-results"], ["--preflight"], ["--folder", "old-run"],
    ["--embedding-resume-from", "historical"], ["--fo", "old-run"]])
def test_service_cannot_adopt_history_or_unbounded_arguments(paths, monkeypatch, extra):
    config, state = paths
    value = configuration()
    value["base_argv"] += extra
    write(config, value)
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []


def test_old_first_window_never_creates_historical_epoch(paths, monkeypatch):
    config, state = paths
    value = configuration()
    value["window"]["first_to"] = "2026-10-18"
    write(config, value)
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []


class Egress:
    """Scripted egress checks and recorded ops alerts; never touches a socket or Telegram."""

    def __init__(self, monkeypatch, outcomes):
        self.outcomes, self.alerts, self.checks = list(outcomes), [], 0
        monkeypatch.setattr(service, "check_egress", self.check)
        monkeypatch.setattr(service, "notify_ops", self.alerts.append)

    def check(self):
        self.checks += 1
        if self.outcomes and self.outcomes.pop(0) is False:
            raise service.EgressUnavailable("proxy unreachable")


def test_egress_check_failure_waits_in_egress_unavailable_and_alerts_once(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    egress = Egress(monkeypatch, [False, False, True])
    clock, seen = Clock(), []
    clock.on_wait = lambda: seen.append(service._read(state / "state.private.json"))
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert egress.checks == 3 and len(children) == 1
    assert [value["status"] for value in seen] == ["egress_unavailable"] * 2
    assert all(value["reason"] == "EgressUnavailable" for value in seen)
    assert clock.current == NOW + timedelta(seconds=120)
    assert len(egress.alerts) == 3 and "unavailable" in egress.alerts[0] and "recovered" in egress.alerts[1]
    assert "completo" in egress.alerts[2]
    saved = service._read(state / "state.private.json")
    assert saved["status"] == "complete" and "egress_down" not in saved


def test_egress_check_failure_never_starts_a_child_or_blocks_the_source(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [False])
    clock = Clock()
    clock.on_wait = clock.set
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 2
    assert children == [] and not (state / "runs").exists()
    saved = service._read(state / "state.private.json")
    assert saved["status"] == "attention_required" and saved["reason"] == "operator_stop_requires_review"
    assert saved["egress_down"] == "check"


def test_restart_during_an_outage_keeps_checking_without_a_second_alert(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    first = Egress(monkeypatch, [False])
    clock = Clock()
    clock.on_wait = clock.set
    service.run(config, state, project_root=ROOT, now=clock.now, stop=clock)
    state_file = state / "state.private.json"
    service._write(state_file, {**service._read(state_file), "status": "egress_unavailable"})
    second = Egress(monkeypatch, [True])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert len(first.alerts) == 2 and "unavailable" in first.alerts[0] and "attention_required" in first.alerts[1]
    assert len(children) == 1
    assert second.alerts[0] == "BuenaPro PROD2: SEACE egress recovered." and len(second.alerts) == 2


def test_child_exit_for_egress_is_not_a_gate_failure_or_source_block(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    codes = [service.EGRESS_EXIT_CODE, 0]
    real_popen = service.subprocess.Popen

    def popen(argv, **kwargs):
        child = real_popen(argv, **kwargs)
        child.returncode = codes.pop(0)
        return child

    monkeypatch.setattr(service.subprocess, "Popen", popen)
    egress = Egress(monkeypatch, [])
    clock, seen = Clock(), []
    clock.on_wait = lambda: seen.append(service._read(state / "state.private.json"))
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert len(children) == 2 and children[0].folder == children[1].folder  # Same epoch name, fresh folder.
    assert [value["status"] for value in seen] == ["egress_unavailable"]
    assert seen[0]["reason"] == "child_exit_egress" and seen[0]["plan"] is None
    runs = sorted(path.name for path in (state / "runs").iterdir())
    assert runs[0] == children[0].folder.name and runs[1].startswith(runs[0] + ".egress-") and len(runs) == 2
    assert service._read(state / "state.private.json")["status"] == "complete"
    assert len(egress.alerts) == 3 and "recovered" in egress.alerts[1] and "completo" in egress.alerts[2]


def test_source_denial_exit_is_still_attention_required_without_egress_alert(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    fake_children(monkeypatch, exit_code=2, mutate=lambda value: value["coverage"][0].update(
        errors=[{"code": "source_403"}], coverage_complete=False))
    egress = Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    saved = service._read(state / "state.private.json")
    assert saved["status"] == "attention_required" and saved["reason"] == "final_report_source_access_blocked"
    assert len(egress.alerts) == 1 and "egress" not in egress.alerts[0]
    assert "attention_required" in egress.alerts[0] and "final_report_source_access_blocked" in egress.alerts[0]


# --- Shared SEACE block: pause before the cycle, resume on its own, and operations alerts ---

def shared_block(gate, *, source="prod6", status=403, seconds=90):
    gate.write_block(json.dumps({"status": status, "source": source, "at": "2026-10-26T15:00:00.000Z"}),
                     seconds * 1000)


def test_shared_block_pauses_before_the_cycle_and_resumes_when_it_ends(paths, monkeypatch, fake_gate):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    egress = Egress(monkeypatch, [])
    shared_block(fake_gate, seconds=90)
    clock, seen = Clock(), []

    def on_wait():
        seen.append(service._read(state / "state.private.json"))
        fake_gate.advance(30_000 if len(seen) == 1 else 60_000)  # The pause outlives the first check.

    clock.on_wait = on_wait
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert [value["status"] for value in seen] == ["source_paused"] * 2 and len(children) == 1
    assert seen[0]["reason"] == "prod6" and seen[0]["http_status"] == 403
    assert seen[0]["resume_at"] == (NOW + timedelta(seconds=90)).isoformat()
    assert seen[1]["resume_at"] == (NOW + timedelta(seconds=90 + 60)).isoformat()  # Re-read: 60 s left.
    assert clock.current == NOW + timedelta(seconds=150)
    pauses = [alert for alert in egress.alerts if "bloqueó" in alert]
    assert len(pauses) == 1 and "HTTP 403" in pauses[0] and "prod6" in pauses[0]  # Alerted once per pause.
    saved = service._read(state / "state.private.json")
    assert saved["status"] == "complete"
    assert not {"paused_since", "resume_at", "http_status"} & set(saved)


def test_shared_block_never_starts_a_child_and_a_stop_ends_in_attention(paths, monkeypatch, fake_gate):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    egress = Egress(monkeypatch, [])
    shared_block(fake_gate, source="web", status=429)
    clock = Clock()
    clock.on_wait = clock.set
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 2
    assert children == [] and not (state / "runs").exists()
    saved = service._read(state / "state.private.json")
    assert saved["status"] == "attention_required" and saved["reason"] == "operator_stop_requires_review"
    assert saved["paused_since"] and saved["resume_at"] and saved["http_status"] == 429
    assert len(egress.alerts) == 2 and "HTTP 429" in egress.alerts[0] and "attention_required" in egress.alerts[1]


def test_restart_while_paused_resumes_without_a_plan(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    state.mkdir(mode=0o700)
    service._write(state / "state.private.json", {"version": 1, "status": "source_paused", "reason": "web"})
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert service._read(state / "state.private.json")["status"] == "complete" and len(children) == 1


def test_restart_while_paused_in_a_later_cycle_keeps_the_previous_plan_without_gating_it(paths, monkeypatch, fake_gate):
    config, state = paths
    write(config, configuration(max_cycles=2))
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    first = Clock()
    first.on_wait = first.set  # Cycle 1 completes; the stop arrives while waiting for cycle 2.
    assert service.run(config, state, project_root=ROOT, now=first.now, stop=first) == 2
    state_file = state / "state.private.json"
    paused = service._read(state_file)
    assert paused["plan"] and paused["completed_cycles"] == 1
    service._write(state_file, {**paused, "status": "source_paused", "reason": "prod2"})
    real_gate, gated = service._gate, []
    monkeypatch.setattr(service, "_gate", lambda plan: gated.append(plan["epoch_id"]) or real_gate(plan))
    shared_block(fake_gate, source="prod2")
    second = Clock()
    second.on_wait = lambda: fake_gate.advance(90_000)
    assert service.run(config, state, project_root=ROOT, now=second.now, stop=second) == 0
    assert gated == ["synthetic-explicit-000002"]  # The old plan is not re-gated while paused.
    assert len(children) == 2 and service._read(state_file)["status"] == "complete"


def test_gate_outage_between_checks_is_an_egress_outage_not_a_pause(paths, monkeypatch, fake_gate):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    egress = Egress(monkeypatch, [])
    fake_gate.fail = ConnectionError("down")
    clock, seen = Clock(), []

    def on_wait():
        seen.append(service._read(state / "state.private.json"))
        fake_gate.fail = None

    clock.on_wait = on_wait
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert [value["status"] for value in seen] == ["egress_unavailable"]
    assert seen[0]["reason"] == "SharedGateUnavailable" and len(children) == 1
    assert "unavailable" in egress.alerts[0] and "recovered" in egress.alerts[1]


def test_without_the_gate_nothing_pauses_and_redis_is_never_asked(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    monkeypatch.setattr(service.shared_gate, "_BACKEND", object())
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0 and len(children) == 1


@pytest.mark.parametrize("value,status", [({"version": 1, "operator_hold": "source_403"}, "blocked_source"),
    ({"version": 1, "enabled": False}, "prepared")])
def test_holds_do_not_alert(paths, monkeypatch, value, status):
    config, state = paths
    write(config, value)
    egress = Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert service._read(state / "state.private.json")["status"] == status and egress.alerts == []


def test_reaching_complete_alerts_once_and_a_later_restart_is_silent(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    fake_children(monkeypatch)
    egress = Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert len(egress.alerts) == 1 and "completo" in egress.alerts[0]


def test_attention_required_alerts_with_the_reason_and_a_restart_loop_does_not_repeat_it(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    fake_children(monkeypatch, exit_code=0, no_report=True)
    egress = Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    first = service._read(state / "state.private.json")["reason"]
    assert first == "missing_or_ambiguous_final_report"
    assert len(egress.alerts) == 1 and first in egress.alerts[0] and "revisión manual" in egress.alerts[0]
    for _ in range(3):  # A supervisor restarting the container finds the same stop each time.
        assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert service._read(state / "state.private.json")["reason"] == "interrupted_epoch_requires_manual_review"
    assert len(egress.alerts) == 2  # One alert for the new reason, none for the repeats.
