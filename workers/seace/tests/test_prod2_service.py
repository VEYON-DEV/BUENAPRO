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
