"""Manual grading («Calificar») of PROD2 convocatorias: the consumer of ``manual_grading_requests``.

A long-running process next to the daily service (``buenapro_worker.prod2.service``). Every
``--poll-seconds`` it looks for queued requests of ``seace_prod2`` and, only when it is safe, reads the
convocatoria (paid, like a cycle) unless a reading exists and evaluates the requesting company. It does
nothing while the service runs a cycle or is about to (``--safety-minutes`` before ``next_cycle_at``),
while the shared SEACE gate is blocked, or while another coordinator owns the PostgreSQL executor lock.
Logic, gates and payment rules: ``buenapro_worker.prod2.manual_grading``.

  --state-dir          its own private directory: cases/, evaluations/ receipts and guard.private.json
  --service-state-dir  the daily service's state directory, mounted read only (state.private.json)
  --service-config     the daily service's private configuration, mounted read only

A payment doubt fails its request and opens ``<state-dir>/guard.private.json``: nothing is sent until an
operator reviews the case and removes the file. Exit codes: 0 stopped normally, 1 the last pass failed
(--once), 2 refused to start, or a guard is open (--once).

Automatic reading queue (off unless --auto-reading-window is given; without it nothing below applies):

  --auto-reading-window HH:MM-HH:MM  Lima time, may cross midnight (01:00-07:00, 22:00-06:00). Inside it, once the
                                     manual requests are drained, the pass also reads the convocatorias queued in
                                     prod2_reading_queue (accepted, affinity >= 85, never read), earliest close first,
                                     in <state-dir>/auto/<convocatoria>, and evaluates each one it read with the
                                     automatic rules (receipts in <state-dir>/auto-evaluations)
  --max-auto-per-pass N              Readings started per pass (default 3); closed or already read items do not count
  --auto-evaluation-max-calls N      Ceiling of evaluations for the one convocatoria just read (default 20)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
from datetime import timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from buenapro_worker.prod2 import manual_grading
from buenapro_worker.settings import Settings


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--loop", action="store_true", help="Run passes until SIGTERM")
    mode.add_argument("--once", action="store_true", help="Run one pass and exit")
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--service-state-dir", type=Path, required=True)
    p.add_argument("--service-config", type=Path, required=True)
    p.add_argument("--max-requests-per-pass", type=int, default=5)
    p.add_argument("--poll-seconds", type=float, default=60)
    p.add_argument("--safety-minutes", type=int, default=60,
                   help="Claim nothing within this many minutes of the service's next cycle (at least 15)")
    p.add_argument("--auto-reading-window", metavar="HH:MM-HH:MM",
                   help="Lima-time window in which the automatic reading queue is served; without it, it is off")
    p.add_argument("--max-auto-per-pass", type=int, default=3,
                   help="Automatic readings started per pass (default 3)")
    p.add_argument("--auto-evaluation-max-calls", type=int, default=20,
                   help="Ceiling of evaluations for each automatically read convocatoria (default 20)")
    return p


def refuse(reason: str) -> int:
    print(json.dumps({"event": "startup_refused", "reason": reason}), flush=True)
    return 2


def private_dir(path: Path) -> Path | None:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        return None
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return None if path.stat().st_mode & 0o077 else path


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not (1 <= args.max_requests_per_pass <= 50 and args.poll_seconds >= 5 and args.safety_minutes >= 15
            and 1 <= args.max_auto_per_pass <= 50 and 1 <= args.auto_evaluation_max_calls <= 200):
        return refuse("invalid_bounds")
    auto_window = None
    if args.auto_reading_window is not None:
        try:
            auto_window = manual_grading.parse_window(args.auto_reading_window)
        except ValueError:
            return refuse("invalid_auto_window")
    if not args.service_state_dir.is_dir() or not args.service_config.is_file():
        return refuse("service_paths_missing")  # An unmounted state directory must never read as "no cycle running".
    os.umask(0o077)
    state = private_dir(args.state_dir)
    if state is None:
        return refuse("state_directory_not_private")
    settings = Settings(_env_file=ROOT / ".env.local")
    if not os.environ.get("TYPESAFE_API_KEY") or not settings.openai_api_key:
        return refuse("missing_provider_credentials")
    lock = open(state / "consumer.lock", "a+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return refuse("another_consumer_owns_the_state_directory")
    runtime = manual_grading.Runtime(
        state_dir=state, service_state_dir=args.service_state_dir, service_config=args.service_config,
        project_root=ROOT, settings=settings, connect=lambda: manual_grading.database(settings.database_url),
        max_requests=args.max_requests_per_pass, safety=timedelta(minutes=args.safety_minutes),
        auto_window=auto_window, max_auto=args.max_auto_per_pass,
        auto_evaluation_max_calls=args.auto_evaluation_max_calls)

    def stopping(*_):
        runtime.stop.set()
        runtime.abort()  # A running read stops at its next safe boundary and resumes after the restart.
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, stopping)
    status = manual_grading.loop(runtime, poll_seconds=args.poll_seconds, once=args.once)
    return status if args.once else 0


if __name__ == "__main__":
    sys.exit(main())
