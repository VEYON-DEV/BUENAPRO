"""Sequential, restartable supervisor for explicitly prepared PROD2 batches.

It adopts the active batch and starts the next prepared launcher only after a
verified final report and an empty queue. It never retries a provider or changes
a frozen batch. --retry-current is reserved for diagnosed recovery by the agent.
"""
from __future__ import annotations

import argparse
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
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "workers/seace/src")]

from buenapro_worker.prod2.keyword_chain import assess_final_report

STOP = threading.Event()


def read(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlinked chain input is not allowed")
    return json.loads(path.read_text())


def digest(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlinked chain input is not allowed")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Symlinked chain state is not allowed")
    temporary = path.parent / (".chain-writing-" + os.urandom(12).hex())
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pid_matches(pid, job):
    if type(pid) is not int or pid <= 0:
        raise ValueError("Invalid worker PID")
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat=", "-o", "command="],
                            capture_output=True, text=True, timeout=10)
    if result.returncode or not result.stdout.strip():
        return False
    if result.stdout.strip().split(None, 1)[0].startswith("Z"):
        return False
    if str(ROOT / "scripts/run_prod2_selected.py") not in result.stdout or job["folder"] not in result.stdout:
        # A recycled PID is not an active PROD2 worker and must not be signaled.
        raise ValueError("Worker PID identity changed")
    return True


def validate_job(job):
    import run_prod2_selected as selected
    folder = Path(job["folder"])
    if (not folder.is_absolute() or folder.is_symlink() or not folder.is_relative_to(ROOT / "tmp")
            or folder.name != job["run_id"]):
        raise ValueError("Invalid prepared run folder")
    if digest(folder / "command.private.json") != job["command_sha256"]:
        raise ValueError("Prepared command changed")
    command = read(folder / "command.private.json")
    if (command[:3] != [str(ROOT / ".venv/bin/python"), "-u", str(ROOT / "scripts/run_prod2_selected.py")]
            or not all(isinstance(arg, str) for arg in command)):
        raise ValueError("Only the prepared PROD2 executor may run")
    args = selected.parser().parse_args(command[3:])
    requested = selected.plan(args)
    if (args.folder.resolve() != folder or not args.all_results
            or requested != job["requested"]
            or requested.get("prior_reading_policy") != "prod2_preserve_prior_readings_v1"
            or requested.get("llm1_input_policy") != "prod2_embedding_retrieved_text_v1"):
        raise ValueError("Frozen batch inputs changed")
    marker = folder / "manifest.private.json"
    if marker.exists() and read(marker).get("requested") != requested:
        raise ValueError("Existing manifest differs from prepared batch")
    if digest(folder / "launch.private.py") != job["launcher_sha256"]:
        raise ValueError("Prepared launcher changed")
    return command


def report_for(job, process, exit_receipt):
    if exit_receipt.get("worker_pid") != process.get("worker_pid"):
        raise ValueError("Exit receipt belongs to another worker")
    files = sorted(Path(job["folder"]).glob("report-*.private.json"))
    if not files:
        return assess_final_report(None, job["run_id"], exit_receipt.get("exit_code")), None
    path = files[-1]
    started = datetime.fromisoformat(process["started_at"]).timestamp()
    if path.stat().st_mtime < started - 1:
        raise ValueError("Final report predates this execution")
    return assess_final_report(read(path), job["run_id"], exit_receipt.get("exit_code")), path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--retry-current", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    folder = args.folder.absolute()
    if folder.is_symlink() or not folder.is_relative_to(ROOT / "tmp"):
        raise ValueError("Use a private prepared chain folder")
    plan_path, state_path = folder / "plan.private.json", folder / "state.private.json"
    plan, plan_hash = read(plan_path), digest(plan_path)
    if plan.get("policy") != "prod2_keyword_chain_v1" or not plan.get("jobs"):
        raise ValueError("Invalid chain plan")
    jobs = plan["jobs"]
    if len({job["run_id"] for job in jobs}) != len(jobs):
        raise ValueError("Repeated chain run identity")
    for job in jobs:
        validate_job(job)  # Offline: no source, database or providers.
    with (folder / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(state_path) if state_path.exists() else {
            "plan_sha256": plan_hash, "job_index": 0, "finished_jobs": []}
        if state["plan_sha256"] != plan_hash:
            raise ValueError("Chain plan changed")
        if type(state["job_index"]) is not int or not 0 <= state["job_index"] <= len(jobs):
            raise ValueError("Invalid chain checkpoint index")
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())
        power = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state.update(supervisor_pid=os.getpid(), caffeinate_pid=power.pid)

        def checkpoint(status, **facts):
            state.update(status=status, updated_at=datetime.now(timezone.utc).isoformat(), **facts)
            save(state_path, state)

        try:
            while state["job_index"] < len(jobs) and not STOP.is_set():
                job = jobs[state["job_index"]]
                validate_job(job)
                run = Path(job["folder"])
                process_path, exit_path = run / "process.private.json", run / "exit.private.json"
                process = read(process_path) if process_path.exists() else None
                live = process is not None and pid_matches(process["worker_pid"], job)
                launch = process is None and not job["adopt_active"]
                if args.retry_current and live:
                    args.retry_current = False  # Never apply recovery to a later group.
                if args.retry_current and not live:
                    # Explicit recovery preserves old execution coordination too.
                    stamp = str(time.time_ns())
                    for path in (process_path, exit_path):
                        if path.exists():
                            path.rename(path.with_name(path.stem + "-previous-" + stamp + path.suffix))
                    process = None
                    launch = True
                    args.retry_current = False
                if launch:
                    checkpoint("starting_batch", current_run_id=job["run_id"])
                    with (run / "supervisor.log").open("ab", buffering=0) as output:
                        launcher = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(run / "launch.private.py")],
                            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                            start_new_session=True)
                    state["launcher_pid"] = launcher.pid
                    # A prepared launcher normally publishes the PID immediately.
                    for _ in range(30):
                        if process_path.exists() or STOP.is_set():
                            break
                        if launcher.poll() is not None:
                            break
                        STOP.wait(1)
                    process = read(process_path) if process_path.exists() else None
                if process is None:
                    checkpoint("attention_required", reason="worker_process_receipt_missing", current_run_id=job["run_id"])
                    return 2
                checkpoint("waiting_batch", current_run_id=job["run_id"], worker_pid=process["worker_pid"])
                while pid_matches(process["worker_pid"], job) and not STOP.is_set():
                    STOP.wait(15)
                    checkpoint("waiting_batch")
                if STOP.is_set():
                    checkpoint("supervisor_stopped", reason="stop_requested_workers_untouched")
                    return 2
                for _ in range(30):
                    if exit_path.exists():
                        break
                    STOP.wait(1)
                if not exit_path.exists():
                    checkpoint("attention_required", reason="worker_exit_receipt_missing")
                    return 2
                assessment, report_path = report_for(job, process, read(exit_path))
                save(folder / (job["run_id"] + ".assessment.private.json"), {
                    **assessment, "report_path": str(report_path) if report_path else None,
                    "verified_at": datetime.now(timezone.utc).isoformat()})
                if not assessment["advance"]:
                    checkpoint("attention_required", reason=assessment["reason"])
                    return 2
                state["finished_jobs"].append({"run_id": job["run_id"], **assessment})
                state["job_index"] += 1
                checkpoint("batch_finished", reason=assessment["reason"])
            if STOP.is_set():
                checkpoint("supervisor_stopped", reason="stop_requested_workers_untouched")
                return 2
            review = any(job["state"] == "complete_with_review" for job in state["finished_jobs"])
            checkpoint("complete_with_review" if review else "complete", current_run_id=None, reason="all_prepared_batches_finished")
            return 0
        except Exception as error:
            checkpoint("attention_required", reason="supervisor_exception", error_type=type(error).__name__)
            return 1
        finally:
            if power.poll() is None:
                power.terminate()
                power.wait(timeout=10)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Do not expose connection strings, payloads, documents or API keys.
        print(json.dumps({"status": "supervisor_failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
