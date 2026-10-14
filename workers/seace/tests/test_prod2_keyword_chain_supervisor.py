"""Supervisor handoff tests: no network, real workers, or provider calls."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_chain_script", ROOT / "scripts/run_prod2_keyword_chain.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def final_report(run_id, *, complete=True):
    return {"run_id": run_id, "pipeline": "prod2_selected_llm1_v1", "terminal_stage": "llm1",
        "source_coverage_complete": complete, "queries_total": 1, "queries_complete": int(complete),
        "coverage": [{"coverage_complete": complete, "errors": []}], "scope_proofs": [],
        "redis": {"queued": 0, "pending": 0, "payloads": 0, "seen": 1}, "processed": 1,
        "accepted": 1, "llm1_complete": 0, "requires_review": 1,
        "records": [{"status": "needs_review"}], "costs": {"usage_unknown_attempts": 1}}


def prepare_job(root, name, adopt):
    import run_prod2_selected as selected
    folder = root / "tmp" / name
    folder.mkdir(parents=True)
    command = [str(root / ".venv/bin/python"), "-u", str(root / "scripts/run_prod2_selected.py"),
        "--folder", str(folder), "--keyword", "software", "--years", "2026", "--all-results",
        "--llm1-input", "embeddings", "--preserve-prior-readings"]
    subject.save(folder / "command.private.json", command)
    (folder / "launch.private.py").write_text("# controlled synthetic launcher\n")
    requested = selected.plan(selected.parser().parse_args(command[3:]))
    return {"folder": str(folder), "run_id": name, "adopt_active": adopt, "requested": requested,
        "command_sha256": subject.digest(folder / "command.private.json"),
        "launcher_sha256": subject.digest(folder / "launch.private.py")}


def finish(job, *, complete=True, embedding_stop=None):
    folder = Path(job["folder"])
    subject.save(folder / "process.private.json", {"worker_pid": 424242,
        "started_at": "2000-01-01T00:00:00+00:00"})
    subject.save(folder / "exit.private.json", {"worker_pid": 424242, "exit_code": 2})
    report = final_report(job["run_id"], complete=complete)
    if embedding_stop is not None:
        report["embedding_dispatch_stop_requested"] = embedding_stop
    subject.save(folder / "report-1.private.json", report)


@pytest.mark.parametrize("complete,embedding_stop,expected_launches", [(True, None, 1),
    (False, None, 0), (True, True, 0), (True, False, 1)])
def test_terminal_review_advances_but_incomplete_source_never_launches_next(tmp_path, monkeypatch, complete, embedding_stop, expected_launches):
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    subject.STOP.clear()
    jobs = [prepare_job(tmp_path, "first", True), prepare_job(tmp_path, "second", False)]
    finish(jobs[0], complete=complete, embedding_stop=embedding_stop)
    folder = tmp_path / "tmp" / "chain"
    folder.mkdir()
    subject.save(folder / "plan.private.json", {"policy": "prod2_keyword_chain_v1", "jobs": jobs})
    monkeypatch.setattr(subject, "pid_matches", lambda *args: False)
    monkeypatch.setattr(subject.signal, "signal", lambda *args: None)
    launched = []
    class Process:
        pid = 1234
        def poll(self): return None
        def terminate(self): pass
        def wait(self, **kw): return 0
    def popen(command, **kw):
        if command[0] != "/usr/bin/caffeinate":
            assert command == [str(tmp_path / ".venv/bin/python"), str(Path(jobs[1]["folder"]) / "launch.private.py")]
            launched.append(command)
            finish(jobs[1])
        return Process()
    monkeypatch.setattr(subject.subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "argv", ["chain", "--folder", str(folder)])
    can_advance = complete and embedding_stop is not True
    assert subject.main() == (0 if can_advance else 2)
    state = subject.read(folder / "state.private.json")
    assert len(launched) == expected_launches
    assert state["job_index"] == (2 if can_advance else 0)
    assert state["status"] == ("complete_with_review" if can_advance else "attention_required")
    if embedding_stop is True:
        assert state["reason"] == "embedding_provider_dispatch_stopped"
    if can_advance:
        # Restarting a completed supervisor cannot repeat the jobs or paid stages.
        assert subject.main() == 0
        assert len(launched) == 1


def test_command_and_launcher_mutation_stop_before_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    job = prepare_job(tmp_path, "one", True)
    assert subject.validate_job(job)
    path = Path(job["folder"]) / "launch.private.py"
    path.write_text("# altered launcher\n")
    with pytest.raises(ValueError, match="launcher changed"):
        subject.validate_job(job)
    job["launcher_sha256"] = subject.digest(path)
    command_path = Path(job["folder"]) / "command.private.json"
    command = subject.read(command_path)
    command.remove("--all-results")
    subject.save(command_path, command)
    with pytest.raises(ValueError, match="command changed"):
        subject.validate_job(job)


def test_exit_receipt_and_report_must_belong_to_completed_execution(tmp_path):
    job = {"folder": str(tmp_path), "run_id": "one"}
    subject.save(tmp_path / "report-1.private.json", final_report("one"))
    process = {"worker_pid": 1, "started_at": "2000-01-01T00:00:00+00:00"}
    with pytest.raises(ValueError, match="another worker"):
        subject.report_for(job, process, {"worker_pid": 2, "exit_code": 2})
    process["started_at"] = "2100-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="predates"):
        subject.report_for(job, process, {"worker_pid": 1, "exit_code": 2})


@pytest.mark.parametrize("command,expected", [("", False), ("Z <defunct>", False),
    (f"S {ROOT}/scripts/run_prod2_selected.py --folder /synthetic/run", True)])
def test_dead_zombie_and_matching_worker_identity(monkeypatch, command, expected):
    monkeypatch.setattr(subject.subprocess, "run", lambda *args, **kw:
        SimpleNamespace(returncode=0 if command else 1, stdout=command))
    assert subject.pid_matches(123, {"folder": "/synthetic/run"}) is expected


def test_recycled_pid_is_not_adopted_or_signaled(monkeypatch):
    monkeypatch.setattr(subject.subprocess, "run", lambda *args, **kw:
        SimpleNamespace(returncode=0, stdout="S unrelated-process"))
    with pytest.raises(ValueError, match="identity changed"):
        subject.pid_matches(123, {"folder": "/synthetic/run"})
