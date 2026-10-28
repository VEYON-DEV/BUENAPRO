"""Conservative service around the selected CLI; no historical-run adoption.

A private ``{"version": 1, "operator_hold": "source_403"}`` is sufficient
to record a blocked deployment without importing any ingestion dependencies.
Enabled configurations must explicitly authorize their first fresh epoch,
rolling publication window, base arguments, interval and finite cycle budget.
An interrupted child is never restarted or signaled by a later service process.
A failed SEACE egress (proxy down, or a child exiting with EGRESS_EXIT_CODE) is
not a source denial: the service waits one interval in ``egress_unavailable``
and checks again; a child's partial epoch folder is kept renamed, never adopted.
A shared SEACE block (403/429 seen by PROD6, the web or an earlier child, see
``seace.shared_gate``) is not a failure either: before each cycle the service waits it out
in ``source_paused``, with the block's source as ``reason`` and ``resume_at``.
A cycle cut by such a block, or a search pass left incomplete without hitting the candidate
cap, is retried in a fresh folder with the same sweep (completed queries replay from the
keyword cache) only while its payment and dispatch evidence is clean: blocks up to
``RETRY_LIMITS["blocked"]`` times per cycle after the shared pause, an incomplete pass once
after ``INCOMPLETE_RETRY_SECONDS`` in ``retry_wait``. Every other stop still needs review.
``--years window`` searches the years the cycle's window spans. An optional ``start_time``
(HH:MM, Lima) starts each later cycle at that hour, at least ``interval_seconds`` after the
previous one ends; ``interval_seconds`` still paces egress checks.
Operations get a best-effort alert when a pause starts, after each cycle, on
``attention_required`` and on ``complete``.
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

from ..seace import shared_gate
from ..seace.egress import EGRESS_EXIT_CODE, EgressUnavailable, check_egress, notify_ops
from .keyword_chain import assess_final_report

VERSION = 1
LIMA = ZoneInfo("America/Lima")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
START_TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
RETRY_LIMITS = {"blocked": 3, "incomplete": 1}
INCOMPLETE_RETRY_SECONDS = 1800
RESUMABLE = ("waiting", "egress_unavailable", "source_paused", "retry_wait")
_PAYMENT_GUARDS = ("usage_unknown_attempts", "embedding_unknown_dispatch_attempts",
                   "embedding_usage_unknown_attempts", "embedding_receipt_conflicts")


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
               "interval_seconds", "window", "max_cycles", "start_time"}
    if (not isinstance(value, dict) or set(value) - allowed
            or type(value.get("version")) is not int or value["version"] != VERSION
            or ("enabled" in value and type(value["enabled"]) is not bool)
            or value.get("operator_hold") not in (None, "source_403")):
        raise ServiceError("invalid_configuration")
    if value.get("operator_hold") == "source_403" or value.get("enabled") is False:
        return value
    if value.get("enabled") is not True or not allowed - {"operator_hold", "start_time"} <= set(value):
        raise ServiceError("explicit_enabled_configuration_required")
    if "start_time" in value and (not isinstance(value["start_time"], str)
                                  or not START_TIME.fullmatch(value["start_time"])):
        raise ServiceError("invalid_start_time")
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


def _window_years(base_argv, start, end):
    """``--years window`` becomes the years the window spans, so a new year needs no new config."""
    years = ",".join(str(year) for year in range(start.year, end.year + 1))
    argv = list(base_argv)
    for index, value in enumerate(argv):
        if value == "--years=window":
            argv[index] = "--years=" + years
        elif value == "--years" and index + 1 < len(argv) and argv[index + 1] == "window":
            argv[index + 1] = years
    return argv


def _plan(config, directory, number, now, project_root, *, started=False):
    """Validate with the existing pure CLI planner; never call its main().

    ``started`` (a pause or retry already recorded for this cycle) lets cycle 1 move its
    window to today instead of stopping when the wait crossed midnight in Lima.
    """
    selected = _selected(project_root)
    epoch = config["first_epoch_id"] + f"-{number:06d}"
    folder = _literal(directory / "runs" / epoch)
    if folder.exists():
        raise ServiceError("existing_epoch_requires_manual_review")
    today = now.astimezone(LIMA).date()
    end = date.fromisoformat(config["window"]["first_to"]) if number == 1 and not started else today
    if end != today:
        raise ServiceError("historical_or_future_epoch_not_authorized")
    start = end - timedelta(days=config["window"]["days"] - 1)
    parser = selected.parser()
    forbidden = {"--folder", "--sweep-id", "--cache-folder", "--keyword-cache-folder",
                 "--from-date", "--to-date", "--months", "--embedding-resume-from",
                 "--all-results", "--dry-run", "--preflight", "--keyword-status"}
    base_argv = _window_years(config["base_argv"], start, end)
    flags = {v.split("=", 1)[0] for v in base_argv if v.startswith("--")}
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
    argv = [*base_argv, "--folder", str(folder), "--sweep-id", epoch,
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
    # v6 reads the bases' "Sección Específica" and trims to its maximum instead of sending the
    # case to review; v5 stays valid for the epochs that started with it.
    accepted = [load_config(project_root / f"docs/sources/prod2-embedding-reading.runtime.{version}.json")
                for version in ("v5", "v6")]
    if validate_frozen_config(requested) not in accepted:
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
    if not _payments_clean(report, folder):
        raise ServiceError("payment_or_dispatch_guard_requires_review")
    refs = {"report_sha256": hashlib.sha256(reports[0].read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256((folder / "manifest.private.json").read_bytes()).hexdigest()}
    if "completion_refs" in plan and plan["completion_refs"] != refs:
        raise ServiceError("completed_epoch_evidence_changed")
    return refs


def _payments_clean(report, folder):
    costs = report.get("costs")
    return (isinstance(costs, dict)
            and all(type(costs.get(k)) is int and costs[k] == 0 for k in _PAYMENT_GUARDS)
            and report.get("embedding_dispatch_stop_requested") is False
            and not list(folder.rglob("*guard*.json")))


def _retry_kind(plan, reason):
    """``blocked``/``incomplete`` when a stopped cycle may run again, else None.

    Only called after ``_gate`` refused the final report itself, so plan, argv, manifest
    and the single report were already proven to belong to this cycle.
    """
    if reason not in ("final_report_source_access_blocked", "final_report_source_incomplete"):
        return None
    folder = _literal(plan["folder"])
    report = _read(next(folder.glob("report-*.private.json")))
    if not _payments_clean(report, folder):
        return None
    if reason == "final_report_source_access_blocked":
        return "blocked"
    limits = (report.get("execution_policy") or {}).get("effective_limits") or {}
    seen = (report.get("redis") or {}).get("seen")
    if limits.get("max_candidates") is not None and (type(seen) is not int or seen >= limits["max_candidates"]):
        return None  # The candidate cap cut the search; the same cap would cut it again.
    return "incomplete"


def _next_cycle_at(config, moment):
    if "start_time" not in config:
        return moment + timedelta(seconds=config["interval_seconds"])
    hour, minute = (int(part) for part in config["start_time"].split(":"))
    earliest = (moment + timedelta(seconds=config["interval_seconds"])).astimezone(LIMA)
    start = earliest.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (start if start >= earliest else start + timedelta(days=1)).astimezone(timezone.utc)


def _cycle_alert(plan, number, next_at):
    """Counts and known cost of a completed cycle; never record text from the report."""
    folder = _literal(plan["folder"])
    report = _read(next(folder.glob("report-*.private.json")))
    summary = assess_final_report(report, plan["epoch_id"], plan["exit_code"])["summary"]
    cost = summary.get("costs", {}).get("known_cost_usd", 0)
    text = (f"BuenaPro PROD2: ciclo {number} listo ({plan['published_from']} a {plan['published_to']}): "
            f"{summary.get('redis', {}).get('seen', 0)} candidatas, {summary.get('accepted', 0)} aceptadas, "
            f"{summary.get('llm1_complete', 0)} leídas, {summary.get('requires_review', 0)} en revisión; "
            f"US$ {cost:.4f}.")
    if next_at is not None:
        text += f" Próximo ciclo: {next_at.astimezone(LIMA):%d/%m %H:%M} (Lima)."
    return text


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

        def egress_outage(reason, origin, **facts):
            """Alert once per outage; True when stopped while waiting for the next check."""
            if not state.get("egress_down"):
                notify_ops(f"BuenaPro PROD2: SEACE egress unavailable ({reason}); retrying every "
                           f"{config['interval_seconds']}s.")
            checkpoint("egress_unavailable", reason=reason, egress_down=state.get("egress_down") or origin,
                       next_cycle_at=(now() + timedelta(seconds=config["interval_seconds"])).isoformat(),
                       **facts)
            return stop.wait(config["interval_seconds"])

        def egress_recovered():
            if state.pop("egress_down", None):
                notify_ops("BuenaPro PROD2: SEACE egress recovered.")

        def source_pause(block):
            """Alert once per pause; True when stopped while waiting for the shared block to end."""
            wait = max(1.0, block["remaining_seconds"])
            resume_at = (now() + timedelta(seconds=wait)).isoformat()
            if not state.get("paused_since"):
                notify_ops(f"BuenaPro PROD2: SEACE bloqueó el acceso compartido (HTTP {block['status']}, "
                           f"origen {block['source']}); en pausa hasta {resume_at}.")
            checkpoint("source_paused", reason=block["source"], http_status=block["status"],
                       paused_since=state.get("paused_since") or now().isoformat(), resume_at=resume_at)
            return stop.wait(wait)

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
            if state.get("status") not in (None, "complete", "blocked_source", "prepared", *RESUMABLE):
                raise ServiceError("interrupted_epoch_requires_manual_review")
            if state.get("plan") and state.get("config_sha256") != _hash(config):
                raise ServiceError("active_configuration_changed")
            if state.get("status") == "complete":
                _gate(state["plan"])
                return 0
            # A hold may be lifted only with the full explicit enabled contract.
            if state.get("plan") and state.get("status") not in RESUMABLE:
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
                try:
                    check_egress()
                    shared_block = shared_gate.blocked()
                except EgressUnavailable as error:
                    if egress_outage(type(error).__name__, "check"):
                        break
                    continue
                if state.get("egress_down") == "check":
                    egress_recovered()
                if shared_block is not None:
                    if source_pause(shared_block):
                        break
                    continue
                started = state.get("status") in ("egress_unavailable", "source_paused", "retry_wait")
                for stale in ("paused_since", "resume_at", "http_status"):
                    state.pop(stale, None)
                plan = _plan(config, directory, number, now(), project_root, started=started)
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
                if plan["exit_code"] == EGRESS_EXIT_CODE and not stop.is_set():
                    # Egress failed inside the child: keep its evidence out of the way
                    # so the retry gets a fresh epoch folder (cycle number unchanged).
                    folder.rename(folder.with_name(f"{folder.name}.egress-{time.time_ns()}"))
                    if egress_outage("child_exit_egress", "child", plan=None):
                        break
                    continue
                if plan["exit_code"] != EGRESS_EXIT_CODE:
                    egress_recovered()  # The child reached SEACE through the egress.
                checkpoint("validating", plan=plan)
                if stop.is_set():
                    raise ServiceError("interrupted_epoch_requires_manual_review")
                try:
                    plan["completion_refs"] = _gate(plan)
                except ServiceError as error:
                    try:
                        kind = _retry_kind(plan, str(error))
                    except Exception:
                        kind = None
                    retries = state.get("retries") or {}
                    retries = retries if retries.get("cycle") == number else {"cycle": number}
                    if kind is None or retries.get(kind, 0) >= RETRY_LIMITS[kind]:
                        raise
                    retries = {**retries, kind: retries.get(kind, 0) + 1}
                    # As with an egress failure the attempt stays as evidence and the same sweep
                    # resumes in a fresh folder; a block first waits out the shared pause.
                    folder.rename(folder.with_name(f"{folder.name}.{kind}-{time.time_ns()}"))
                    wait = (INCOMPLETE_RETRY_SECONDS if kind == "incomplete"
                            else 0 if shared_gate.enabled() else shared_gate.block_ms() / 1000)
                    checkpoint("retry_wait", reason=str(error), plan=None, retries=retries,
                               next_cycle_at=(now() + timedelta(seconds=wait)).isoformat())
                    if wait:
                        notify_ops(f"BuenaPro PROD2: ciclo {number} sin terminar ({error}); "
                                   f"se reintenta en {int(wait // 60)} min.")
                        if stop.wait(wait):
                            break
                    continue
                _write(folder / "completion.private.json", plan)
                last = number >= config["max_cycles"]
                next_at = _next_cycle_at(config, now())
                state.pop("retries", None)
                checkpoint("complete" if last else "waiting", plan=plan, completed_cycles=number,
                           next_cycle_at=next_at.isoformat())
                try:
                    alert = _cycle_alert(plan, number, None if last else next_at)
                except Exception:
                    alert = f"BuenaPro PROD2: ciclo {number} listo."
                if last:
                    notify_ops(f"{alert} Servicio completo; ciclos terminados: {number}.")
                    return 0
                notify_ops(alert)
                number += 1
            raise ServiceError("operator_stop_requires_review")
        except Exception as error:
            reason = str(error) if isinstance(error, ServiceError) else type(error).__name__
            repeated = state.get("status") == "attention_required" and state.get("reason") == reason
            checkpoint("attention_required", reason=reason)
            if not repeated:  # A restart loop on the same stop must not repeat the alert.
                notify_ops(f"BuenaPro PROD2: servicio detenido (attention_required): {reason}; "
                           "requiere revisión manual.")
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
