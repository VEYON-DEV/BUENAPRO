"""Manual grading («Calificar») of PROD2 convocatorias, driven by ``manual_grading_requests``.

The web records a ``queued`` request (``source_key`` ``seace_prod2``, ``source_id`` the id_convocatoria) after
checking that the convocatoria is published, JEV >= 60, open and not yet graded for the company. This consumer
moves it ``pending -> reading -> evaluating -> done | failed`` (every transition is a compare-and-set that
bumps ``updated_at``): it reads the documents through the same paid chain as a cycle unless a reading exists
(``enrich`` of the selected runner in its ID-handoff mode: fresh ficha, lifecycle gate, dispatch guards), then
evaluates the requesting company only (``evaluation.evaluate_manual``).

It runs next to the daily service (``prod2.service``) and never disturbs it. A pass runs only when ALL hold:
the service state is ``waiting`` (or absent) with ``next_cycle_at`` at least ``safety`` away, no guard of this
consumer is open, the shared SEACE gate is not blocked and the egress answers, and the PostgreSQL executor
lock (the one ``run_prod2_selected`` takes) was acquired without waiting. The lock is held for the whole pass,
new work is not claimed after ``next_cycle_at - safety`` and a watchdog stops a running read ``HARD_STOP``
before the cycle. A stopped read stays ``reading`` and resumes in a later pass.

Payments follow the cycle's rules. Every paid stage is receipt driven, so resuming a request after a crash
replays what was answered and never sends again. A case left with an unresolved paid attempt (started
receipt without answer, a claim without response, a dispatch-stop marker) fails its request as
``payment_uncertain`` and opens ``guard.private.json`` in the state directory: no pass sends anything until
an operator reviews it and removes the file. An unresolved reservation of an earlier cycle only fails the
request. Evaluation receipts live in ``<state>/evaluations``, apart from the service's own.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

import psycopg

from buenapro_worker.prod6.evaluation_guard import GUARD_FILENAME
from buenapro_worker.seace import shared_gate
from buenapro_worker.seace.egress import EgressUnavailable, check_egress, notify_ops, seace_http_client

from . import evaluation, service
from .analysis import ficha_reading_description
from .batch_plan import LIMA, MEMBER_IDS, native_arguments
from .batch_runner import private_write, read_private
from .client import Prod2HttpClient
from .embedding_runtime import EmbeddingPreparationReview
from .handoff_guards import HandoffGuards
from .prior_reading_guard import POLICY as PRIOR_POLICY, preserve_prior_reading
from .selected_control import RunLimits
from .selected_payments import audit_summary
from .temporary_storage import _payment_uncertain

SOURCE_KEY = "seace_prod2"
PROFILE = MEMBER_IDS[0]  # The profile every PROD2 reading is stored under.
VERSION = "prod2_manual_grading_v1"
STUCK_HOURS = 24
GUARD = "guard.private.json"
# A pass hands the executor back this long before the cycle; claims stop ``safety`` before it.
HARD_STOP = timedelta(minutes=10)
PROVIDER_COOLDOWN = timedelta(minutes=15)
SOURCE_COOLDOWN = timedelta(hours=3)  # As the shared SEACE pause, for when the shared gate is off.
WATCH_SECONDS = 20
# One convocatoria, one request: selector 1 call; LLM1 1 call plus the saturation or Luna backup (each its own
# reservation); embedding batches and official documents (inventory plus archive members) are capped.
MAX_SELECTOR_CALLS, MAX_LLM1_CALLS, MAX_EMBEDDING_CALLS, MAX_NEW_DOCUMENTS = 2, 2, 60, 300
NARROW_PAGES, SEARCH_PAGES = 6, 40
MANUAL_EPOCH = 900001  # Only names the planned sweep of service._plan; nothing is written under it.
DISPATCH_STOP = "sent_provider_attempt_with_uncertain_usage"
UNKNOWN_COSTS = ("usage_unknown_attempts", "embedding_unknown_dispatch_attempts",
                 "embedding_usage_unknown_attempts", "embedding_receipt_conflicts")
# Why a pair is not evaluated -> the failure code the web shows.
SKIPPED = {"closed": "convocatoria_cerrada", "reading_failed": "reading_failed", "no_requirements": "no_requirements",
           "profile_inactive": "profile_inactive", "profile_hash_missing": "profile_inactive",
           "profile_not_supported": "profile_not_supported", "jev_missing": "jev_below_60",
           "jev_rejected": "jev_below_60", "no_current_reading": "reading_requires_review",
           "reservation_failed": "invalid_model_output"}
# What the reading ends as when no extraction was stored.
UNREAD = {"closed_without_reading": "convocatoria_cerrada", "opening_not_confirmed": "convocatoria_cerrada",
          "documents_missing": "reading_requires_review", "selection_needs_review": "reading_requires_review",
          "needs_review": "reading_requires_review"}

LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)"
OWNS_LOCK_SQL = """SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted
    AND pid=pg_backend_pid() AND classid::bigint=(hashtext('prod2_accepted_executor')::bigint & 4294967295)
    AND objid=0 AND objsubid=2) AS held"""

WORK_SQL = """SELECT 1 FROM manual_grading_requests WHERE source_key = 'seace_prod2' AND mode = 'queued'
  AND status IN ('pending', 'reading', 'evaluating') LIMIT 1"""
CLAIM_SQL = """SELECT id, profile_id, source_id FROM manual_grading_requests
  WHERE source_key = 'seace_prod2' AND mode = 'queued' AND status = 'pending'
  ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"""
ACTIVE_SQL = """SELECT id, profile_id, source_id, status FROM manual_grading_requests
  WHERE source_key = 'seace_prod2' AND mode = 'queued' AND status IN ('reading', 'evaluating')
  ORDER BY updated_at, id"""
# A request stuck in reading/evaluating never blocks a new one (the active-pair index) for more than a day.
EXPIRE_SQL = """UPDATE manual_grading_requests SET status = 'failed', reason = 'reading_timeout', updated_at = now()
  WHERE source_key = 'seace_prod2' AND mode = 'queued' AND status IN ('reading', 'evaluating')
    AND updated_at < now() - make_interval(hours => %s) RETURNING id"""
# done and failed are final: a holder of a stale copy cannot reopen or rewrite them.
MOVE_SQL = """UPDATE manual_grading_requests SET status = %s, reason = %s, updated_at = now()
  WHERE id = %s AND source_key = 'seace_prod2' AND mode = 'queued' AND status = %s
    AND status IN ('pending', 'reading', 'evaluating')"""
FACTS_SQL = """SELECT disc.id_convocatoria IS NOT NULL AS found, cp.id IS NOT NULL AS profile_active,
  d.proposals_closes_at, jev.affinity
FROM (SELECT 1) one
LEFT JOIN prod2_discoveries disc ON disc.id_convocatoria = %(convocation)s
LEFT JOIN company_profiles cp ON cp.id = %(profile)s::uuid AND cp.is_active
LEFT JOIN prod2_details d ON d.id_convocatoria = disc.id_convocatoria
LEFT JOIN LATERAL (
  SELECT CASE WHEN jsonb_typeof(ev.result_json->'affinity_index') = 'number'
    THEN (ev.result_json->>'affinity_index')::numeric END AS affinity
  FROM prod2_evaluations ev
  WHERE ev.id_convocatoria = disc.id_convocatoria AND ev.kind = 'jev_affinity' AND ev.status = 'done'
    AND ev.policy_version = %(jev_policy)s
  ORDER BY ev.completed_at DESC NULLS LAST, ev.id DESC LIMIT 1
) jev ON true"""
# The listed document of the current reading, whatever its quality or review flag: the evaluator decides.
CURRENT_SQL = """SELECT e.id FROM prod2_document_extractions e JOIN prod2_documents d
  ON d.id_convocatoria = e.id_convocatoria AND d.codigo_alfresco = e.codigo_alfresco AND d.is_listed
  WHERE e.id_convocatoria = %s AND e.is_current LIMIT 1"""
ITEM_SQL = """SELECT m.row_manifest, m.query_manifest, m.observed_at, disc.published_at
  FROM prod2_discoveries disc JOIN prod2_accepted_run_members m ON m.convocation_id = disc.id_convocatoria
  WHERE disc.id_convocatoria = %s AND m.accepted AND m.row_manifest IS NOT NULL AND m.query_manifest IS NOT NULL
  ORDER BY m.updated_at DESC, m.created_at DESC LIMIT 1"""
# Paid attempts of ANY earlier run on this convocatoria that nobody resolved (as handoff_guards.preserve_by_id).
UNRESOLVED_SQL = """SELECT 'llm1' AS stage, status FROM prod2_evaluations
    WHERE id_convocatoria = %(c)s AND kind = 'gemini_extraction' AND status IN ('running', 'uncertain')
  UNION ALL SELECT 'selector', status FROM prod2_document_selection_cache
    WHERE convocation_id = %(c)s AND status IN ('running', 'uncertain')
  UNION ALL SELECT DISTINCT 'embedding', a.status FROM prod2_embedding_corpora c
    JOIN prod2_embedding_chunks ch USING(corpus_hash)
    JOIN prod2_embedding_vectors v ON v.text_sha256 = ch.text_sha256
      AND v.model = c.vector_model AND v.dimensions = c.vector_dimensions
    JOIN prod2_embedding_attempts a USING(attempt_id)
    WHERE c.convocation_id = %(c)s AND a.status IN ('running', 'uncertain', 'partial_known')"""


class Fail(Exception):
    """The request ends ``failed`` with this code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Defer(Exception):
    """Nothing was lost: the request stays where it is and a later pass takes it. ``stop`` ends this pass."""

    def __init__(self, why: str, *, stop: bool = True):
        super().__init__(why)
        self.why, self.stop = why, stop


class Uncertain(Exception):
    """A paid attempt of this consumer has no durable answer: fail the request and open the guard."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)


# --- the requests table ---------------------------------------------------------------------------------------

class Requests:
    """SQL over one autocommit connection (``dict_row``). Every status change is a compare-and-set."""

    def __init__(self, conn):
        self.conn = conn

    def _tolerant(self, statement: Callable[[], Any], default: Any) -> Any:
        try:
            return statement()
        except psycopg.errors.UndefinedTable:  # A database before migration 0050 has nothing to serve.
            return default

    def expire(self) -> int:
        return self._tolerant(lambda: len(self.conn.execute(EXPIRE_SQL, (STUCK_HOURS,)).fetchall()), 0)

    def has_work(self) -> bool:
        return self._tolerant(lambda: self.conn.execute(WORK_SQL).fetchone() is not None, False)

    def claim(self) -> dict | None:
        """The oldest pending request, now ``reading``; two consumers can never take the same one."""
        with self.conn.transaction():
            row = self.conn.execute(CLAIM_SQL).fetchone()
            if row is None:
                return None
            request = {**row, "status": "pending"}
            return request if self.move(request, "reading") else None

    def active(self) -> list[dict]:
        """Started requests left by a pass that stopped or crashed (the executor lock makes them ours)."""
        return list(self.conn.execute(ACTIVE_SQL).fetchall())

    def move(self, request: dict, status: str, reason: str | None = None) -> bool:
        """Compare-and-set on the status the caller holds; ``request['status']`` follows on success."""
        changed = self.conn.execute(MOVE_SQL, (status, reason, request["id"], request["status"])).rowcount
        if changed == 1:
            request["status"] = status
        return changed == 1

    def facts(self, convocation: int, profile: str) -> dict:
        return self.conn.execute(FACTS_SQL, {"convocation": convocation, "profile": profile,
                                             "jev_policy": evaluation.JEV_POLICY}).fetchone()

    def reading(self, convocation: int) -> tuple[bool, list[str]]:
        """(a reading is current, statuses of the earlier paid attempts when it is not)."""
        if self.conn.execute(CURRENT_SQL, (convocation,)).fetchone():
            return True, []
        prior = preserve_prior_reading(self.conn, convocation_id=convocation, profile_id=PROFILE,
                                       policy=PRIOR_POLICY)
        evidence = (prior or {}).get("prior_reading_preserved", {})
        return prior is not None and prior["status"] == "llm1_complete", [
            row["status"] for row in evidence.get("prior_reservations", [])]

    def item(self, convocation: int) -> dict | None:
        """The accepted source observation the run that promoted it froze (row, query, when)."""
        row = self.conn.execute(ITEM_SQL, (convocation,)).fetchone()
        if row is None or row["row_manifest"].get("convocation_id") != convocation:
            return None
        published = row["published_at"].astimezone(LIMA).date() if row["published_at"] else None
        return {"row": row["row_manifest"], "query": row["query_manifest"], "published_day": published,
                "observed_at": row["observed_at"].isoformat() if row["observed_at"] else None}

    def unresolved(self, convocation: int) -> list[dict]:
        return list(self.conn.execute(UNRESOLVED_SQL, {"c": convocation}).fetchall())


# --- when a pass may run ----------------------------------------------------------------------------------------

@dataclass
class Window:
    claim_until: datetime | None  # No new work after this moment.
    hard_stop: datetime | None  # A running read is stopped at this moment.


def service_gate(state_dir: Path, now: datetime, safety: timedelta) -> tuple[str | None, Window]:
    """(why the pass must not run, its window) from the daily service's state file (read only)."""
    path = Path(state_dir) / "state.private.json"
    window = Window(None, None)
    if not path.exists():
        return None, window
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        status, upcoming = state["status"], state.get("next_cycle_at")
        upcoming = datetime.fromisoformat(upcoming) if upcoming is not None else None
        if upcoming is not None and (upcoming.tzinfo is None or upcoming.utcoffset() is None):
            raise ValueError("naive next_cycle_at")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "service_state_unreadable", window
    if status != "waiting":
        return f"service_{status}", window
    if upcoming is None:
        return None, window
    window = Window(upcoming - safety, upcoming - min(safety, HARD_STOP))
    return ("cycle_imminent", window) if now >= window.claim_until else (None, window)


def holds_executor_lock(owner) -> bool:
    if getattr(owner, "closed", False) or getattr(owner, "broken", False):
        return False
    return owner.execute(OWNS_LOCK_SQL).fetchone()["held"] is True


class Watchdog:
    """Stops a running read before the cycle needs the executor, or when the executor lock is lost."""

    def __init__(self, rt: "Runtime", owner, hard_stop: datetime | None):
        self.rt, self.owner, self.hard_stop = rt, owner, hard_stop
        self.reason: str | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def check(self) -> str | None:
        if self.reason is None:
            if self.hard_stop is not None and self.rt.now() >= self.hard_stop:
                self.trip("cycle_imminent")
            else:
                try:
                    held = holds_executor_lock(self.owner)
                except psycopg.Error:
                    held = False
                if not held:
                    self.trip("lock_lost")
        return self.reason

    def trip(self, reason: str) -> None:
        self.reason = reason
        self.rt.abort()

    def _run(self) -> None:
        while not self._done.wait(self.rt.watch_seconds):
            self.check()

    def __enter__(self):
        if self.rt.watch_seconds:
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._done.set()
        if self._thread.is_alive():
            self._thread.join()


# --- the runtime ------------------------------------------------------------------------------------------------

@dataclass
class Runtime:
    state_dir: Path
    service_state_dir: Path
    service_config: Path
    project_root: Path
    settings: Any
    connect: Callable[[], Any]  # A context manager yielding a new autocommit connection with dict rows.
    max_requests: int = 5
    safety: timedelta = timedelta(minutes=60)
    stop: threading.Event = field(default_factory=threading.Event)
    now: Callable[[], datetime] = utcnow
    check_egress: Callable[[], None] = check_egress
    shared_block: Callable[[], dict | None] = shared_gate.blocked
    notify: Callable[[str], bool] = notify_ops
    emit: Callable[[dict], None] = emit
    requests: Callable[[Any], Any] = Requests
    load_config: Callable[[Path], dict] = service._config
    evaluate: Callable[..., dict] = evaluation.evaluate_manual
    jev_key: Callable[[], str] = lambda: os.environ.get("TYPESAFE_API_KEY", "")
    watch_seconds: float = WATCH_SECONDS
    cooldown_until: datetime | None = None
    alerted: set = field(default_factory=set)
    aborted: bool = False
    loaded: Any = None
    watchdog: Any = None
    # Replaceable steps of a read; the defaults are below.
    selected: Callable[["Runtime"], Any] = None
    plan_manifest: Callable[..., dict] = None
    fresh_ficha: Callable[..., dict] = None
    read: Callable[..., None] = None

    def __post_init__(self):
        self.state_dir = Path(self.state_dir)
        self.selected = self.selected or load_selected
        self.plan_manifest = self.plan_manifest or build_manifest
        self.fresh_ficha = self.fresh_ficha or locate_ficha
        self.read = self.read or read_convocatoria

    def guard_open(self) -> bool:
        return any((self.state_dir / name).exists() for name in (GUARD, Path("evaluations") / GUARD_FILENAME))

    def open_guard(self, request_id: int, reason: str) -> None:
        """Never replaces an earlier unresolved incident (private_write links atomically and refuses)."""
        try:
            private_write(self.state_dir / GUARD, {"version": 1, "status": "open", "request_id": request_id,
                                                   "reason": reason, "opened_at": self.now().isoformat()})
        except FileExistsError:
            pass

    def abort(self) -> None:
        """Ask a running read to stop at its next safe boundary."""
        self.aborted = True
        if self.loaded is not None:
            self.loaded.STOP.set()

    def alert(self, key: str, text: str) -> None:
        """One alert per kind of incident for the life of the process; never raises."""
        if key not in self.alerted:
            self.alerted.add(key)
            self.notify(text)


def load_selected(rt: Runtime):
    rt.loaded = rt.loaded or service._selected(Path(rt.project_root))
    return rt.loaded


# --- reading one convocatoria -----------------------------------------------------------------------------------

def own_payment_doubt(run_folder: Path) -> str | None:
    """Evidence in this request's own folder that a paid attempt has no durable answer. Fails closed."""
    try:
        marker = run_folder / "handoff-guards" / "dispatch-stop.private.json"
        if marker.exists() and read_private(marker).get("reason") == DISPATCH_STOP:
            return "dispatch_stop"
        costs = audit_summary(run_folder)
        if any(costs[key] for key in UNKNOWN_COSTS):
            return "usage_unknown"
        if _payment_uncertain(run_folder, sorted(run_folder.rglob("*.private.json"))):
            return "receipt_without_answer"
    except Exception:
        return "receipts_unreadable"
    return None


def narrowed(query: dict, day: date | None) -> list[tuple[dict, int]]:
    """The original query limited to the days around the publication: a few pages instead of many."""
    if day is None:
        return []
    start, end = date.fromisoformat(query["published_from"]), date.fromisoformat(query["published_to"])
    low, high = max(day - timedelta(days=1), start), min(day + timedelta(days=1), end)
    if low > high or (low, high) == (start, end):
        return []
    return [({**query, "published_from": low.isoformat(), "published_to": high.isoformat()}, NARROW_PAGES)]


def locate_ficha(rt: Runtime, selected, item: dict, folder: Path, pacer) -> dict:
    """A fresh ficha of the accepted convocatoria, found by re-running its query (bounded pages)."""
    row, query = item["row"], item["query"]
    with seace_http_client(timeout=60, verify=True, event_hooks=selected.source_hooks(pacer)) as http:
        source = Prod2HttpClient(http)
        for attempt, pages in [*narrowed(query, item.get("published_day")), (query, SEARCH_PAGES)]:
            source.search(attempt["keyword"], **native_arguments(attempt))
            for page in range(1, min(source.search_snapshot["total_pages"], pages) + 1):
                if source.counter[3] != page:
                    source.search_page(page)
                index = next((i for i, found in enumerate(source.rows)
                              if found["convocatoria_id"] == row["convocation_id"]
                              and found["procedure_id"] == row["procedure_id"]), None)
                if index is not None:
                    break
            else:
                continue
            if source.rows[index]["description"] != row["description"]:
                raise Fail("ficha_not_found")  # The accepted JEV decision belongs to another text.
            detail = source.detail(index, all_documents=True)
            if (detail.get("convocatoria_id") != row["convocation_id"] or detail.get("procedure_id") != row["procedure_id"]
                    or ficha_reading_description(detail.get("description")) != ficha_reading_description(row["description"])):
                raise Fail("ficha_not_found")
            capture = {"observed_at": datetime.now(timezone.utc).isoformat(),
                       "detail": {k: v for k, v in detail.items() if k not in {"http_status", "redirect_statuses"}}}
            private_write(folder / "ficha-observations" / str(time.time_ns()) / "detail.private.json", capture)
            return capture
    raise Fail("ficha_not_found")


def build_manifest(rt: Runtime, item: dict, request_id: int) -> dict:
    """The manifest a cycle would freeze, planned by the service itself from its own config."""
    try:
        config = rt.load_config(rt.service_config)
        if "base_argv" not in config:
            raise service.ServiceError("service_not_enabled")
        now = rt.now()
        requested = service._plan(config, rt.state_dir / "plan", MANUAL_EPOCH, now, Path(rt.project_root))["requested"]
    except (service.ServiceError, OSError, ValueError) as error:
        raise Defer("plan_rejected:" + (str(error) if isinstance(error, service.ServiceError) else type(error).__name__))
    query = item["query"]
    return {**requested, "requested": requested, "run_id": f"prod2-manual-{request_id}",
            "resolved_years": [query["year"]], "published_from": query["published_from"],
            "published_to": query["published_to"], "profile_id": PROFILE,
            "gemini_model": rt.settings.gemini_model, "frozen_at": now.isoformat()}


def settle_reading(record: dict) -> None:
    """Raise unless the reading's outcome stored an extraction."""
    status = record.get("status")
    if status != "llm1_complete":
        raise Fail(UNREAD.get(status, "reading_failed"))


def classify_failure(rt: Runtime, selected, store, error: BaseException, run_folder: Path, convocation: int):
    """Turn an exception of the paid chain into the control exception that settles the request.

    Order matters: a payment doubt outranks a source block, and both outrank a plain failure.
    """
    if rt.stop.is_set():
        raise Defer("shutdown") from error
    if rt.aborted and rt.watchdog is not None and rt.watchdog.reason:
        raise Defer(rt.watchdog.reason) from error
    if selected.EGRESS_DOWN.is_set() or selected.egress_failure(error):
        raise Defer("egress_unavailable") from error
    if selected.stop_after_uncertain_embedding_error(error) is not None:
        raise Uncertain("embedding_sent_without_answer") from error
    doubt = own_payment_doubt(run_folder)
    if doubt:
        raise Uncertain(doubt) from error
    try:
        blocked = rt.shared_block() is not None
    except EgressUnavailable:
        raise Defer("egress_unavailable") from error
    if blocked or selected.STOP.is_set():  # STOP with no other cause is a 403/429 (the shared gate may be off).
        rt.cooldown_until = rt.now() + SOURCE_COOLDOWN
        raise Defer("source_blocked") from error
    if store.unresolved(convocation):
        raise Fail("payment_uncertain") from error
    if isinstance(error, EmbeddingPreparationReview):
        raise Fail("reading_requires_review") from error
    raise Fail("reading_failed") from error


def read_convocatoria(rt: Runtime, store, owner, request: dict, convocation: int) -> None:
    """Read the documents of one accepted convocatoria like a cycle does; raises unless an extraction was stored."""
    selected = rt.selected(rt)
    request_id = int(request["id"])
    run_folder = rt.state_dir / "cases" / str(request_id)
    private = run_folder / str(convocation)
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    outcome_path = private / "outcome.private.json"
    if outcome_path.exists():  # A previous attempt settled this reading; nothing is repeated.
        return settle_reading(read_private(outcome_path))
    doubt = own_payment_doubt(run_folder)
    if doubt:
        raise Uncertain(doubt)
    item = store.item(convocation)
    if item is None:
        raise Fail("ficha_not_found")
    marker = run_folder / "manifest.private.json"
    if not marker.exists():
        private_write(marker, rt.plan_manifest(rt, item, request_id))
    manifest = read_private(marker)
    if manifest["gemini_model"] != rt.settings.gemini_model:
        raise Fail("reading_failed")  # Frozen run model changed: a new request freezes its own.
    jev_key = rt.jev_key()
    if not jev_key:
        raise Defer("missing_credentials")
    selected.STOP.clear()
    selected.EGRESS_DOWN.clear()
    selected.EMBEDDING_DISPATCH_STOP.clear()
    if rt.stop.is_set() or (rt.watchdog is not None and rt.watchdog.reason):  # Asked to stop while preparing.
        raise Defer("shutdown" if rt.stop.is_set() else rt.watchdog.reason)
    guard = HandoffGuards(run_folder, stop=selected.STOP, max_candidates=1, max_new_documents=MAX_NEW_DOCUMENTS,
                          binding={"version": VERSION, "request_id": request_id, "convocation_id": convocation})
    costs = audit_summary(run_folder)
    limits = RunLimits(max_accepted=1, max_candidates=1, max_jev_calls=MAX_SELECTOR_CALLS, max_llm1_calls=MAX_LLM1_CALLS,
                       model_workers=1, accepted_ids=[convocation], seen=1, jev_calls=costs["jev_calls"],
                       llm1_calls=costs["llm1_calls"], max_embedding_calls=MAX_EMBEDDING_CALLS,
                       embedding_calls=costs.get("embedding_calls", 0))
    limits.handoff_guard = guard
    pacer = selected.SharedPacer(manifest["request_interval"])
    record = {"convocation_id": convocation, "description": item["row"]["description"],
              "object_type": item["row"]["object_type"], "accepted": True, "status": "needs_review",
              "manual_request_id": request_id}
    try:
        guard.admit_candidate(convocation)
        capture = rt.fresh_ficha(rt, selected, item, run_folder, pacer)
        # Fresh ficha first: a closed or unconfirmed convocatoria returns its gate record before any paid call.
        record.update(selected.enrich(store.conn, rt.settings, private, item, manifest, pacer, limits, jev_key,
                                      capture=capture, defer_reading=False))
    except (Fail, Defer, Uncertain):
        raise
    except Exception as error:
        classify_failure(rt, selected, store, error, run_folder, convocation)
    private_write(outcome_path, record)
    try:  # The reading is stored; a failed cleanup keeps the files and never fails the request.
        selected.cleanup_finished_case(store.conn, private, record, manifest)
    except Exception as error:
        rt.emit({"event": "cleanup_error", "request_id": request_id, "error_type": type(error).__name__})
    settle_reading(record)


# --- one request ------------------------------------------------------------------------------------------------

def precheck(store, now: datetime, convocation: int, profile: str) -> None:
    """Free checks before any reading is paid for (the web checked most already; things change)."""
    facts = store.facts(convocation, profile)
    if not facts["found"]:
        raise Fail("contract_not_found")
    if not facts["profile_active"]:
        raise Fail("profile_inactive")
    if profile not in MEMBER_IDS:
        raise Fail("profile_not_supported")
    if facts["affinity"] is None or float(facts["affinity"]) < 60:
        raise Fail("jev_below_60")
    if facts["proposals_closes_at"] is not None and facts["proposals_closes_at"] <= now:
        raise Fail("convocatoria_cerrada")


def needs_reading(rt: Runtime, store, request: dict, convocation: int) -> bool:
    """Whether a read must be paid. Earlier paid attempts without a current reading are never paid again."""
    current, statuses = store.reading(convocation)
    if current:
        return False
    ours = any((rt.state_dir / "cases" / str(request["id"])).rglob("gemini_extraction-*.claim.private.json"))
    if ("running" in statuses or "uncertain" in statuses) and not ours:
        raise Fail("payment_uncertain")
    if "failed" in statuses:
        raise Fail("reading_failed")
    if "done" in statuses and not ours:
        raise Fail("reading_requires_review")
    return True


def finish(rt: Runtime, store, request: dict, result: dict) -> None:
    """Settle the request from the evaluator's outcome."""
    outcome, reason = result["outcome"], result.get("reason")
    if outcome in {"evaluated", "replayed"} or (outcome == "skipped" and reason == "match_current"):
        store.move(request, "done")
    elif outcome == "skipped" and reason in SKIPPED:
        raise Fail(SKIPPED[reason])
    elif outcome == "skipped":  # Reservation busy elsewhere (the automatic evaluator) or a profile edit just now.
        raise Defer(reason or "skipped", stop=False)
    elif outcome == "model_output_invalid":
        raise Fail("invalid_model_output")
    elif outcome == "provider_rejected":
        rt.cooldown_until = rt.now() + PROVIDER_COOLDOWN
        raise Defer("provider_rejected")
    elif outcome in {"payment_uncertain", "receipts_blocked"}:
        raise Uncertain(outcome)
    else:
        raise Fail("reading_failed")


def process(rt: Runtime, store, owner, request: dict) -> tuple[str, str | None]:
    """Take one claimed request as far as it goes. Returns (outcome, why the pass must stop or None)."""
    request_id, convocation, profile = int(request["id"]), int(request["source_id"]), str(request["profile_id"])
    try:
        if request["status"] == "reading":
            precheck(store, rt.now(), convocation, profile)
            if needs_reading(rt, store, request, convocation):
                rt.read(rt, store, owner, request, convocation)
                if not store.reading(convocation)[0]:
                    raise Fail("reading_failed")
            if not store.move(request, "evaluating"):
                return "lost", None
        result = rt.evaluate(store.conn, rt.settings, convocation, profile, request_id=request_id,
                             receipt_dir=rt.state_dir / "evaluations")
        finish(rt, store, request, result)
        return "done", None
    except Defer as defer:
        return "deferred", defer.why if defer.stop else None
    except Uncertain as doubt:
        rt.open_guard(request_id, doubt.why)
        store.move(request, "failed", "payment_uncertain")
        rt.alert("guard", f"BuenaPro PROD2: calificación manual detenida por una duda de cobro "
                          f"(solicitud {request_id}: {doubt.why}); no se envía nada hasta que un operador lo revise.")
        return "failed:payment_uncertain", "payment_uncertain"
    except Fail as fail:
        store.move(request, "failed", fail.reason)
        return "failed:" + fail.reason, None
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise
    except Exception as error:
        rt.emit({"event": "request_error", "request_id": request_id, "error_type": type(error).__name__})
        store.move(request, "failed", "reading_failed")
        return "failed:reading_failed", None


# --- a pass and the loop ----------------------------------------------------------------------------------------

def gate(rt: Runtime, now: datetime) -> tuple[str | None, Window]:
    """(why the pass must not run, its window): guard, provider pause, service state, then SEACE egress."""
    if rt.guard_open():
        return "guard_open", Window(None, None)
    if rt.cooldown_until is not None and now < rt.cooldown_until:
        return "cooldown", Window(None, None)
    reason, window = service_gate(rt.service_state_dir, now, rt.safety)
    if reason:
        return reason, window
    try:
        rt.check_egress()
        block = rt.shared_block()
    except EgressUnavailable:
        return "egress_unavailable", window
    return ("source_blocked", window) if block is not None else (None, window)


def stop_reason(rt: Runtime, window: Window, watchdog: Watchdog) -> str | None:
    now = rt.now()
    if rt.stop.is_set():
        return "shutdown"
    if rt.guard_open():
        return "guard_open"
    if watchdog.check():
        return watchdog.reason
    if window.claim_until is not None and now >= window.claim_until:
        return "cycle_imminent"
    if rt.cooldown_until is not None and now < rt.cooldown_until:
        return "cooldown"
    return None


def work(rt: Runtime, store, owner, window: Window, summary: dict) -> None:
    counts: Counter[str] = Counter()
    handled = 0
    resumed = list(store.active())
    with Watchdog(rt, owner, window.hard_stop) as watchdog:
        rt.watchdog, rt.aborted = watchdog, False
        try:
            while handled < rt.max_requests:
                stop = stop_reason(rt, window, watchdog)
                if stop:
                    summary["stopped"] = stop
                    break
                request = resumed.pop(0) if resumed else store.claim()
                if request is None:
                    break
                handled += 1
                outcome, stop = process(rt, store, owner, request)
                counts[outcome] += 1
                if stop:
                    summary["stopped"] = stop
                    break
        finally:
            rt.watchdog = None
    summary["handled"] = dict(counts)


def run_pass(rt: Runtime) -> dict:
    """One pass; returns its compact summary. Skipped passes cost no lock and no paid call."""
    now = rt.now()
    summary: dict[str, Any] = {"event": "pass", "at": now.isoformat(timespec="seconds")}
    with rt.connect() as conn:
        store = rt.requests(conn)
        summary["expired"] = store.expire()
        if not store.has_work():
            summary["skipped"] = "idle"
            return summary
        reason, window = gate(rt, now)
        if reason:
            summary["skipped"] = reason
            return summary
        with rt.connect() as owner:
            if not owner.execute(LOCK_SQL).fetchone()["locked"]:
                summary["skipped"] = "executor_lock_busy"
                return summary
            try:
                work(rt, store, owner, window, summary)
            finally:
                if not getattr(owner, "closed", False) and not getattr(owner, "broken", False):
                    owner.execute(UNLOCK_SQL)
    return summary


def loop(rt: Runtime, *, poll_seconds: float, once: bool = False) -> int:
    """Run passes until stopped. A failing pass is reported and the loop goes on. Exit 2: a guard is open."""
    status = 0
    while not rt.stop.is_set():
        try:
            summary = run_pass(rt)
        except Exception as error:
            summary = {"event": "pass_error", "at": rt.now().isoformat(timespec="seconds"),
                       "error_type": type(error).__name__}
            status = 1
            rt.alert("pass:" + type(error).__name__, "BuenaPro PROD2: error en la calificación manual "
                     f"({type(error).__name__}); el proceso sigue y reintenta.")
        else:
            status = 2 if summary.get("skipped") == "guard_open" or summary.get("stopped") == "payment_uncertain" else 0
        rt.emit(summary)
        if once or rt.stop.wait(poll_seconds):
            break
    return status


@contextmanager
def database(url: str, name: str = "buenapro-prod2-manual"):
    from psycopg.rows import dict_row
    with psycopg.connect(url, autocommit=True, row_factory=dict_row, application_name=name, connect_timeout=15,
                         keepalives_idle=30, keepalives_interval=10, keepalives_count=3) as conn:
        yield conn
