"""Conservative service around the selected CLI; no historical-run adoption.

A private ``{"version": 1, "operator_hold": "source_403"}`` is sufficient
to record a blocked deployment without importing any ingestion dependencies.
Enabled configurations must explicitly authorize their first fresh epoch,
rolling publication window, base arguments, interval and finite cycle budget.
An interrupted child is never restarted or signaled by a later service process.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

from .keyword_chain import assess_final_report

VERSION = 1
LIMA = ZoneInfo("America/Lima")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


class ServiceError(ValueError):
    """Only controlled, credential-free reasons reach service state/logs."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode()


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _literal(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ServiceError("symlinked_path")
    return path


def _read(path):
    path = _literal(path)
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ServiceError("missing_or_oversized_private_file")
    return json.loads(path.read_bytes())


def _write(path, value):
    path = _literal(path)
    temporary = path.parent / (".service-" + os.urandom(12).hex())
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(directory):
    path = _literal(directory / "service.lock")
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _config(path):
    path = _literal(path)
    value = _read(path)
    if path.stat().st_mode & 0o077:
        raise ServiceError("configuration_not_private")
    allowed = {"version", "enabled", "operator_hold", "first_epoch_id", "base_argv",
               "interval_seconds", "window", "max_cycles"}
    if (not isinstance(value, dict) or set(value) - allowed
            or type(value.get("version")) is not int or value["version"] != VERSION
            or ("enabled" in value and type(value["enabled"]) is not bool)
            or value.get("operator_hold") not in (None, "source_403")):
        raise ServiceError("invalid_configuration")
    if value.get("operator_hold") == "source_403" or value.get("enabled") is False:
        return value
    if value.get("enabled") is not True or not allowed - {"operator_hold"} <= set(value):
        raise ServiceError("explicit_enabled_configuration_required")
    if (not isinstance(value["first_epoch_id"], str)
            or not IDENTIFIER.fullmatch(value["first_epoch_id"])
            or type(value["interval_seconds"]) is not int
            or not 60 <= value["interval_seconds"] <= 86400
            or type(value["max_cycles"]) is not int or not 1 <= value["max_cycles"] <= 10000
            or not isinstance(value["base_argv"], list) or not value["base_argv"]
            or any(not isinstance(v, str) or not v or len(v) > 4096 or "\0" in v
                   for v in value["base_argv"])):
        raise ServiceError("invalid_enabled_configuration")
    window = value["window"]
    if (not isinstance(window, dict) or set(window) != {"policy", "days", "first_to", "zone"}
            or window["policy"] != "rolling_days" or type(window["days"]) is not int
            or not 1 <= window["days"] <= 366 or window["zone"] != "America/Lima"):
        raise ServiceError("invalid_window")
    date.fromisoformat(window["first_to"])
    return value


def _selected(project_root):
    scripts = _literal(project_root / "scripts")
    sys.path.insert(0, str(scripts))
    module = importlib.import_module("run_prod2_selected")
    if Path(module.__file__).resolve() != (scripts / "run_prod2_selected.py").resolve():
        raise ServiceError("unexpected_executor_module")
    return module


def _plan(config, directory, number, now, project_root):
    """Validate with the existing pure CLI planner; never call its main()."""
    selected = _selected(project_root)
    epoch = config["first_epoch_id"] + f"-{number:06d}"
    folder = _literal(directory / "runs" / epoch)
    if folder.exists():
        raise ServiceError("existing_epoch_requires_manual_review")
    today = now.astimezone(LIMA).date()
    end = date.fromisoformat(config["window"]["first_to"]) if number == 1 else today
    if end != today:
        raise ServiceError("historical_or_future_epoch_not_authorized")
    start = end - timedelta(days=config["window"]["days"] - 1)
    parser = selected.parser()
    forbidden = {"--folder", "--sweep-id", "--cache-folder", "--keyword-cache-folder",
                 "--from-date", "--to-date", "--months", "--embedding-resume-from",
                 "--all-results", "--dry-run", "--preflight", "--keyword-status"}
    flags = {v.split("=", 1)[0] for v in config["base_argv"] if v.startswith("--")}
    if (flags & forbidden or any(v.startswith("--prepare-") for v in flags)
            or flags - set(parser._option_string_actions)):
        raise ServiceError("forbidden_or_abbreviated_base_argument")
    required = {"--local", "--preserve-prior-readings", "--document-storage", "--llm1-input",
                "--llm1-scope", "--embedding-config", "--llm1-model-routing", "--llm1-fallback",
                "--years", "--max-accepted", "--max-candidates", "--max-jev-calls",
                "--max-llm1-calls", "--max-embedding-calls", "--search-workers",
                "--processing-workers", "--model-workers", "--request-interval"}
    selectors = {"--keyword", "--lane", "--business-batch", "--business-scope",
                 "--keyword-preset", "--all-keywords"}
    if not required <= flags or not selectors & flags:
        raise ServiceError("explicit_scope_and_paid_policy_required")
    argv = [*config["base_argv"], "--folder", str(folder), "--sweep-id", epoch,
            "--cache-folder", str(directory / "selected-cache"),
            "--keyword-cache-folder", str(directory / "keyword-cache"),
            "--from-date", start.isoformat(), "--to-date", end.isoformat()]
    try:
        with redirect_stderr(io.StringIO()):
            args = parser.parse_args(argv)
    except SystemExit:
        raise ServiceError("invalid_executor_arguments") from None
    if (not args.local or not args.preserve_prior_readings or args.document_storage != "official"
            or args.llm1_input != "embeddings" or args.llm1_scope != "qualification"
            or args.llm1_model_routing == "auto" or args.llm1_fallback == "auto"):
        raise ServiceError("unsupported_paid_policy")
    requested = selected.plan(args)
    from .embedding_runtime import load_config, validate_frozen_config
    expected = load_config(project_root / "docs/sources/prod2-embedding-reading.runtime.v5.json")
    if validate_frozen_config(requested) != expected:
        raise ServiceError("explicit_embeddings_v5_required")
    command = [sys.executable, "-u", str(project_root / "scripts/run_prod2_selected.py"), *argv]
    return {"version": VERSION, "epoch_id": epoch, "number": number, "folder": str(folder),
            "argv": command, "argv_sha256": _hash(command), "requested": requested,
            "requested_sha256": _hash(requested), "published_from": start.isoformat(),
            "published_to": end.isoformat(), "window_policy": dict(config["window"]),
            "config_sha256": _hash(config)}


def _gate(plan):
    folder = _literal(plan["folder"])
    original = {k: v for k, v in plan.items() if k not in {"exit_code", "completion_refs"}}
    if (_read(folder / "plan.private.json") != original
            or _hash(plan["argv"]) != plan["argv_sha256"]
            or _hash(plan["requested"]) != plan["requested_sha256"]
            or _read(folder / "argv.private.json") != plan["argv"]):
        raise ServiceError("epoch_argv_changed")
    manifest = _read(folder / "manifest.private.json")
    if (manifest.get("run_id") != plan["epoch_id"] or manifest.get("requested") != plan["requested"]
            or any(manifest.get(k) != plan[k] for k in ("published_from", "published_to"))):
        raise ServiceError("epoch_manifest_changed")
    reports = sorted(folder.glob("report-*.private.json"))
    if len(reports) != 1:
        raise ServiceError("missing_or_ambiguous_final_report")
    report = _read(reports[0])
    decision = assess_final_report(report, plan["epoch_id"], plan["exit_code"])
    if not decision["advance"]:
        raise ServiceError("final_report_" + decision["reason"])
    costs = report.get("costs", {})
    guarded = ("usage_unknown_attempts", "embedding_unknown_dispatch_attempts",
               "embedding_usage_unknown_attempts", "embedding_receipt_conflicts")
    if (any(type(costs.get(k)) is not int or costs[k] != 0 for k in guarded)
            or report.get("embedding_dispatch_stop_requested") is not False
            or list(folder.rglob("*guard*.json"))):
        raise ServiceError("payment_or_dispatch_guard_requires_review")
    refs = {"report_sha256": hashlib.sha256(reports[0].read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256((folder / "manifest.private.json").read_bytes()).hexdigest()}
    if "completion_refs" in plan and plan["completion_refs"] != refs:
        raise ServiceError("completed_epoch_evidence_changed")
    return refs


def run(config_path, state_dir, *, project_root=Path("/app"), stop=None, now=None):
    stop = stop or threading.Event()
    now = now or (lambda: datetime.now(timezone.utc))
    directory = _literal(state_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ServiceError("state_directory_not_private")
    project_root = _literal(project_root)
    state_path = directory / "state.private.json"
    with _lock(directory):
        state = _read(state_path) if state_path.exists() else {}
        if not isinstance(state, dict):
            raise ServiceError("invalid_service_state")
        child = None
        termination_forwarded = False
        previous_handlers = {}

        def checkpoint(status, **facts):
            state.update(version=VERSION, status=status, updated_at=now().isoformat(), **facts)
            _write(state_path, state)

        def stopping(*_):
            nonlocal termination_forwarded
            stop.set()
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)  # Only this invocation's owned child.
                termination_forwarded = True

        try:
            config = _config(config_path)
            if config.get("operator_hold") == "source_403" or config.get("enabled") is False:
                checkpoint("blocked_source" if config.get("operator_hold") else "prepared",
                           reason=config.get("operator_hold") or "operator_disabled")
                return 2
            if state.get("status") not in (None, "waiting", "complete", "blocked_source", "prepared"):
                raise ServiceError("interrupted_epoch_requires_manual_review")
            if state.get("plan") and state.get("config_sha256") != _hash(config):
                raise ServiceError("active_configuration_changed")
            if state.get("status") == "complete":
                _gate(state["plan"])
                return 0
            # A hold may be lifted only with the full explicit enabled contract.
            if state.get("plan") and state.get("status") != "waiting":
                raise ServiceError("interrupted_epoch_requires_manual_review")
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    previous_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, stopping)
            number = state.get("completed_cycles", 0) + 1
            while not stop.is_set():
                if state.get("status") == "waiting":
                    _gate(state["plan"])
                    remaining = max(0, datetime.fromisoformat(state["next_cycle_at"]).timestamp() - now().timestamp())
                    if stop.wait(remaining):
                        break
                    _gate(state["plan"])
                if _hash(_config(config_path)) != _hash(config):
                    raise ServiceError("active_configuration_changed")
                plan = _plan(config, directory, number, now(), project_root)
                folder = Path(plan["folder"])
                folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                folder.mkdir(mode=0o700)  # Never adopt/retry another execution's folder.
                _write(folder / "argv.private.json", plan["argv"])
                _write(folder / "plan.private.json", plan)
                checkpoint("starting", plan=plan, config_sha256=_hash(config))
                with os.fdopen(os.open(_literal(folder / "runner.private.log"),
                                      os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as log:
                    termination_forwarded = False
                    child = subprocess.Popen(plan["argv"], cwd=project_root, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT)
                    checkpoint("running", child_pid=child.pid)
                    while child.poll() is None:
                        if stop.is_set() and not termination_forwarded:
                            child.send_signal(signal.SIGTERM)
                            termination_forwarded = True
                        time.sleep(0.2)  # SIGTERM is forwarded; never escalate or retry.
                    plan["exit_code"] = child.returncode
                    child = None
                checkpoint("validating", plan=plan)
                if stop.is_set():
                    raise ServiceError("interrupted_epoch_requires_manual_review")
                plan["completion_refs"] = _gate(plan)
                _write(folder / "completion.private.json", plan)
                checkpoint("complete" if number >= config["max_cycles"] else "waiting",
                           plan=plan, completed_cycles=number,
                           next_cycle_at=(now() + timedelta(seconds=config["interval_seconds"])).isoformat())
                if number >= config["max_cycles"]:
                    return 0
                number += 1
            raise ServiceError("operator_stop_requires_review")
        except Exception as error:
            reason = str(error) if isinstance(error, ServiceError) else type(error).__name__
            checkpoint("attention_required", reason=reason)
            return 2
        finally:
            if child is not None and child.poll() is None:
                if not termination_forwarded:
                    child.send_signal(signal.SIGTERM)
                child.wait()  # Retain instance ownership while the owned child drains.
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return run(args.config, args.state_dir, project_root=args.project_root)
    except (OSError, ValueError):
        print('{"status":"attention_required","reason":"private_configuration_or_ownership_unavailable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
