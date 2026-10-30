"""Manual grading («Calificar») of PROD2 mayores: when a pass may run, claiming and timeouts, the payment
guards, the one-pair evaluation and its overrides. Offline: the requests table, the reader and the evaluator
are fakes and the paid chain never runs. The PostgreSQL tests run only with BUENAPRO_TEST_DATABASE_URL (a
database with db/migrations applied) and roll back, except the two that need a second session."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import evaluation, manual_grading as mg
from buenapro_worker.prod2.batch_plan import MEMBER_IDS
from buenapro_worker.prod2.handoff_guards import HandoffGuards
from buenapro_worker.prod6.reading import GuardedReadingError, ProviderRejectedError
from test_prod2_evaluation import (  # noqa: F401  (fakes and the ledger fixture shared with the automatic evaluation)
    DIN, VEYON, Analyzer, Conn, ledger, row, settings)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)
DATABASE_URL = os.environ.get("BUENAPRO_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="BUENAPRO_TEST_DATABASE_URL not set")
CONVOCATION = 1051566


# --- fakes -------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.current = NOW

    def now(self):
        return self.current

    def advance(self, **delta):
        self.current += timedelta(**delta)


class Store:
    """In-memory twin of ``mg.Requests``: the same contract, plus a log of every transition."""

    def __init__(self, *requests):
        self.rows = {r["id"]: {"profile_id": VEYON, "source_id": CONVOCATION, "reason": None, **r} for r in requests}
        self.log, self.claims, self.expired = [], 0, 0
        self.conn = SimpleNamespace(name="work")
        self.facts_row = {"found": True, "profile_active": True, "proposals_closes_at": NOW + timedelta(hours=48),
                          "affinity": 96}
        self.current, self.statuses, self.unresolved_rows, self.item_row = False, [], [], {
            "row": {"convocation_id": CONVOCATION, "procedure_id": 2, "description": "Licencias",
                    "object_type": "service"},
            "query": {"keyword": "software", "object_type": "service", "year": 2026,
                      "published_from": "2026-10-28", "published_to": "2026-10-30"},
            "published_day": None, "observed_at": NOW.isoformat()}

    def expire(self):
        return self.expired

    def has_work(self):
        return any(r["status"] in {"pending", "reading", "evaluating"} for r in self.rows.values())

    def claim(self):
        pending = sorted((r for r in self.rows.values() if r["status"] == "pending"), key=lambda r: r["id"])
        self.claims += bool(pending)
        if not pending:
            return None
        request = {**pending[0]}
        return request if self.move(request, "reading") else None

    def active(self):
        return [{**r} for r in self.rows.values() if r["status"] in {"reading", "evaluating"}]

    def move(self, request, status, reason=None):
        stored = self.rows[request["id"]]
        if stored["status"] != request["status"]:
            return False
        self.log.append((request["id"], stored["status"], status, reason))
        stored.update(status=status, reason=reason)
        request["status"] = status
        return True

    def facts(self, convocation, profile):
        return self.facts_row

    def reading(self, convocation):
        return self.current, self.statuses

    def item(self, convocation):
        return self.item_row

    def unresolved(self, convocation):
        return self.unresolved_rows

    def status(self, request_id):
        return self.rows[request_id]["status"], self.rows[request_id]["reason"]


class Link:
    """A connection for ``run_pass``: only the executor lock is spoken here, the store is a fake."""

    def __init__(self):
        self.busy, self.held, self.unlocked = False, False, 0
        self.closed = self.broken = False

    def execute(self, sql, params=None):
        if sql == mg.LOCK_SQL:
            self.held = not self.busy
            return SimpleNamespace(fetchone=lambda: {"locked": self.held})
        if sql == mg.UNLOCK_SQL:
            self.held, self.unlocked = False, self.unlocked + 1
            return SimpleNamespace(fetchone=lambda: {"unlocked": True})
        if sql == mg.OWNS_LOCK_SQL:
            return SimpleNamespace(fetchone=lambda: {"held": self.held})
        raise AssertionError(sql)


def request(request_id=1, **fields):
    return {"id": request_id, "status": "pending", "profile_id": VEYON, "source_id": CONVOCATION, **fields}


def make(tmp_path, store=None, *, service=None, **fields):
    """A runtime wired to fakes, with the clock, alerts and log lines exposed."""
    store = store or Store(request())
    state, service_dir = tmp_path / "state", tmp_path / "service"
    state.mkdir(mode=0o700, exist_ok=True)
    service_dir.mkdir(exist_ok=True)
    if service is not None:
        (service_dir / "state.private.json").write_text(json.dumps(service))
    clock, link, alerts, lines, calls = Clock(), Link(), [], [], SimpleNamespace(read=[], evaluate=[])

    def read(rt, store_, owner, req, convocation):
        calls.read.append(req["id"])
        store.current = True

    def evaluate(conn, settings_, convocation, profile, **kwargs):
        calls.evaluate.append((convocation, profile, kwargs["request_id"], kwargs["receipt_dir"]))
        return {"outcome": "evaluated"}

    @contextmanager
    def connect():
        yield link

    values = dict(
        state_dir=state, service_state_dir=service_dir, service_config=tmp_path / "service.json",
        project_root=ROOT, settings=SimpleNamespace(gemini_model="gemini-test"), connect=connect, now=clock.now,
        check_egress=lambda: None, shared_block=lambda: None, notify=alerts.append, emit=lines.append,
        requests=lambda conn: store, read=read, evaluate=evaluate, watch_seconds=0, jev_key=lambda: "key")
    runtime = mg.Runtime(**{**values, **fields})
    return SimpleNamespace(rt=runtime, store=store, clock=clock, link=link, alerts=alerts, lines=lines,
                           calls=calls, state=state, service_dir=service_dir)


def waiting(next_cycle_in_hours=8):
    return {"status": "waiting", "next_cycle_at": (NOW + timedelta(hours=next_cycle_in_hours)).isoformat()}


# --- when a pass may run -------------------------------------------------------------------------------------------

def test_without_a_service_state_file_a_pass_may_run(tmp_path):
    assert mg.service_gate(tmp_path, NOW, timedelta(minutes=60)) == (None, mg.Window(None, None))


@pytest.mark.parametrize("state,reason", [
    ({"status": "running"}, "service_running"), ({"status": "starting"}, "service_starting"),
    ({"status": "source_paused"}, "service_source_paused"), ({"status": "retry_wait"}, "service_retry_wait"),
    ({"status": "egress_unavailable"}, "service_egress_unavailable"),
    ({"status": "attention_required"}, "service_attention_required"), ({"status": "complete"}, "service_complete"),
    ({"status": "waiting", "next_cycle_at": (NOW + timedelta(minutes=59)).isoformat()}, "cycle_imminent"),
    ({"status": "waiting", "next_cycle_at": (NOW - timedelta(hours=1)).isoformat()}, "cycle_imminent"),
    ({"status": "waiting", "next_cycle_at": "tomorrow"}, "service_state_unreadable"),
    ({"status": "waiting", "next_cycle_at": "2026-10-31T01:00:00"}, "service_state_unreadable"),
    ({"next_cycle_at": (NOW + timedelta(hours=9)).isoformat()}, "service_state_unreadable"),
    ([], "service_state_unreadable")])
def test_the_service_must_be_waiting_and_far_from_its_next_cycle(tmp_path, state, reason):
    (tmp_path / "state.private.json").write_text(json.dumps(state))
    assert mg.service_gate(tmp_path, NOW, timedelta(minutes=60))[0] == reason


def test_a_corrupt_service_state_file_blocks(tmp_path):
    (tmp_path / "state.private.json").write_text("{not json")
    assert mg.service_gate(tmp_path, NOW, timedelta(minutes=60))[0] == "service_state_unreadable"


def test_a_waiting_service_gives_the_window_of_the_pass(tmp_path):
    upcoming = NOW + timedelta(hours=8)
    (tmp_path / "state.private.json").write_text(json.dumps(waiting(8)))
    reason, window = mg.service_gate(tmp_path, NOW, timedelta(minutes=60))
    assert reason is None
    assert window == mg.Window(claim_until=upcoming - timedelta(minutes=60), hard_stop=upcoming - mg.HARD_STOP)
    (tmp_path / "state.private.json").write_text(json.dumps({"status": "waiting"}))
    assert mg.service_gate(tmp_path, NOW, timedelta(minutes=60)) == (None, mg.Window(None, None))


def test_a_running_service_means_no_lock_no_claim_no_paid_call(tmp_path):
    t = make(tmp_path, service={"status": "running"})
    summary = mg.run_pass(t.rt)
    assert summary["skipped"] == "service_running"
    assert t.store.claims == 0 and t.link.held is False and t.link.unlocked == 0
    assert t.calls.read == [] and t.calls.evaluate == [] and t.store.status(1) == ("pending", None)


def test_a_cycle_about_to_start_means_no_claim(tmp_path):
    t = make(tmp_path, service=waiting(0.5))
    assert mg.run_pass(t.rt)["skipped"] == "cycle_imminent"
    assert t.store.claims == 0 and t.calls.read == []


def test_a_blocked_shared_gate_means_no_claim(tmp_path):
    t = make(tmp_path, service=waiting(), shared_block=lambda: {"status": 403, "source": "prod6", "remaining_seconds": 900})
    assert mg.run_pass(t.rt)["skipped"] == "source_blocked"
    assert t.store.claims == 0 and t.link.held is False


def test_a_failed_egress_means_no_claim(tmp_path):
    def down():
        raise mg.EgressUnavailable("proxy unreachable")
    t = make(tmp_path, service=waiting(), check_egress=down)
    assert mg.run_pass(t.rt)["skipped"] == "egress_unavailable"
    assert t.store.claims == 0


def test_an_executor_lock_held_elsewhere_skips_the_pass_without_claiming(tmp_path):
    t = make(tmp_path, service=waiting())
    t.link.busy = True
    assert mg.run_pass(t.rt)["skipped"] == "executor_lock_busy"
    assert t.store.claims == 0 and t.calls.read == [] and t.link.unlocked == 0


def test_an_idle_pass_costs_neither_the_gate_nor_the_lock(tmp_path):
    def forbidden():
        raise AssertionError("an idle pass must not touch Redis or the executor lock")
    t = make(tmp_path, Store(request(status="done")), shared_block=forbidden, check_egress=forbidden)
    assert mg.run_pass(t.rt)["skipped"] == "idle" and t.link.held is False


def test_an_open_guard_blocks_every_pass(tmp_path):
    t = make(tmp_path, service=waiting())
    (t.state / mg.GUARD).write_text("{}")
    assert mg.run_pass(t.rt)["skipped"] == "guard_open" and t.store.claims == 0
    (t.state / mg.GUARD).unlink()
    (t.state / "evaluations").mkdir()
    (t.state / "evaluations" / "guard.json").write_text("{}")  # The evaluation receipts' own guard.
    assert mg.run_pass(t.rt)["skipped"] == "guard_open"


def test_a_clean_pass_claims_works_and_hands_the_lock_back(tmp_path):
    t = make(tmp_path, service=waiting())
    summary = mg.run_pass(t.rt)
    assert summary["handled"] == {"done": 1} and "skipped" not in summary
    assert t.store.status(1) == ("done", None)
    assert t.link.unlocked == 1 and t.link.held is False
    assert t.calls.evaluate == [(CONVOCATION, VEYON, 1, t.state / "evaluations")]  # Its own receipts directory.


def test_the_lock_is_handed_back_even_when_the_work_raises(tmp_path):
    t = make(tmp_path, service=waiting(), evaluate=lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("gone")))
    with pytest.raises(psycopg.OperationalError):
        mg.run_pass(t.rt)
    assert t.link.unlocked == 1


def test_new_work_stops_when_the_deadline_of_the_pass_arrives(tmp_path):
    store = Store(request(1), request(2, source_id=2), request(3, source_id=3))
    t = make(tmp_path, store, service=waiting(2))  # Claims stop at 60 minutes before the cycle: in 1 hour.

    def slow(conn, settings_, convocation, profile, **kwargs):
        t.clock.advance(minutes=45)
        return {"outcome": "evaluated"}
    t.rt.evaluate = slow
    summary = mg.run_pass(t.rt)
    assert summary["handled"] == {"done": 2} and summary["stopped"] == "cycle_imminent"
    assert store.status(3) == ("pending", None)


def test_at_most_max_requests_per_pass(tmp_path):
    store = Store(*(request(n, source_id=n) for n in range(1, 8)))
    t = make(tmp_path, store, service=waiting(), max_requests=5)
    assert mg.run_pass(t.rt)["handled"] == {"done": 5}
    assert sum(r["status"] == "pending" for r in store.rows.values()) == 2


def test_the_watchdog_stops_a_read_before_the_cycle_and_when_the_lock_is_lost(tmp_path):
    t = make(tmp_path, service=waiting())
    t.rt.loaded = SimpleNamespace(STOP=threading.Event())
    owner = Link()
    owner.held = True
    dog = mg.Watchdog(t.rt, owner, NOW + timedelta(minutes=5))
    assert dog.check() is None and not t.rt.loaded.STOP.is_set()
    t.clock.advance(minutes=6)
    assert dog.check() == "cycle_imminent" and t.rt.loaded.STOP.is_set() and t.rt.aborted
    lost = mg.Watchdog(t.rt, Link(), None)  # Never acquired: the lock is not ours.
    assert lost.check() == "lock_lost"


# --- claiming, compare-and-set and the loop -------------------------------------------------------------------------

def test_a_request_moves_through_reading_and_evaluating_to_done(tmp_path):
    t = make(tmp_path, service=waiting())
    mg.run_pass(t.rt)
    assert t.store.log == [(1, "pending", "reading", None), (1, "reading", "evaluating", None),
                           (1, "evaluating", "done", None)]


def test_a_stale_compare_and_set_changes_nothing(tmp_path):
    store = Store(request(status="reading"))
    stale = request(status="pending")  # Someone else already took it.
    assert store.move(stale, "reading") is False and store.log == [] and stale["status"] == "pending"


def test_requests_are_taken_oldest_first_and_only_once(tmp_path):
    store = Store(request(5, source_id=5), request(2, source_id=2), request(9, source_id=9))
    taken = [store.claim() for _ in range(4)]
    assert [t and t["id"] for t in taken] == [2, 5, 9, None] and all(t["status"] == "reading" for t in taken[:3])


def test_a_request_left_in_flight_is_resumed_before_new_ones(tmp_path):
    store = Store(request(1, status="evaluating"), request(2, source_id=2))
    t = make(tmp_path, store, service=waiting())
    mg.run_pass(t.rt)
    assert t.calls.read == [2]  # The resumed request was already read; it only needs its evaluation.
    assert [call[2] for call in t.calls.evaluate] == [1, 2]


def test_the_pass_reports_what_expired(tmp_path):
    store = Store(request(1, status="reading"))
    store.expired = 3
    t = make(tmp_path, store, service={"status": "running"})
    assert mg.run_pass(t.rt)["expired"] == 3


def test_the_loop_survives_an_exception_and_alerts_once_per_kind(tmp_path):
    t = make(tmp_path, service=waiting())
    passes = iter([RuntimeError("db"), RuntimeError("db"), None])

    def run(rt):
        problem = next(passes)
        if problem:
            raise problem
        rt.stop.set()
        return {"event": "pass", "skipped": "idle"}
    original, mg.run_pass = mg.run_pass, run
    try:
        assert mg.loop(t.rt, poll_seconds=0) in (0, 1)
    finally:
        mg.run_pass = original
    assert [line["event"] for line in t.lines] == ["pass_error", "pass_error", "pass"]
    assert t.lines[0]["error_type"] == "RuntimeError" and len(t.alerts) == 1


def test_a_request_that_raises_is_failed_and_the_loop_goes_on(tmp_path):
    store = Store(request(1), request(2, source_id=2))
    t = make(tmp_path, store, service=waiting())
    t.rt.read = lambda rt, s, o, req, convocation: (_ for _ in ()).throw(ValueError("boom")) if req["id"] == 1 else None
    store.current = True  # The second one already has a reading; the first is made to need one.
    original = mg.needs_reading
    mg.needs_reading = lambda rt, s, req, convocation: req["id"] == 1
    try:
        summary = mg.run_pass(t.rt)
    finally:
        mg.needs_reading = original
    assert summary["handled"] == {"failed:reading_failed": 1, "done": 1}
    assert store.status(1) == ("failed", "reading_failed") and store.status(2) == ("done", None)
    assert any(line.get("event") == "request_error" and line["error_type"] == "ValueError" for line in t.lines)


def test_a_database_outage_is_not_a_request_failure_and_ends_the_pass(tmp_path):
    t = make(tmp_path, service=waiting())
    t.rt.evaluate = lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("gone"))
    with pytest.raises(psycopg.OperationalError):
        mg.run_pass(t.rt)
    assert t.store.status(1) == ("evaluating", None)  # Left as it was; a later pass resumes it.


# --- the free checks and the existing reading --------------------------------------------------------------------

def test_a_current_reading_goes_straight_to_the_evaluation(tmp_path):
    t = make(tmp_path, service=waiting())
    t.store.current = True
    assert mg.run_pass(t.rt)["handled"] == {"done": 1}
    assert t.calls.read == [] and len(t.calls.evaluate) == 1


@pytest.mark.parametrize("facts,reason", [
    ({"found": False}, "contract_not_found"), ({"profile_active": False}, "profile_inactive"),
    ({"affinity": 59.9}, "jev_below_60"), ({"affinity": None}, "jev_below_60"),
    ({"proposals_closes_at": NOW - timedelta(minutes=1)}, "convocatoria_cerrada"),
    ({"proposals_closes_at": NOW}, "convocatoria_cerrada")])
def test_a_request_that_fails_a_free_check_never_reads_or_evaluates(tmp_path, facts, reason):
    t = make(tmp_path, service=waiting())
    t.store.facts_row = {**t.store.facts_row, **facts}
    mg.run_pass(t.rt)
    assert t.store.status(1) == ("failed", reason)
    assert t.calls.read == [] and t.calls.evaluate == []


def test_a_profile_the_evaluator_does_not_serve_is_not_supported(tmp_path):
    t = make(tmp_path, Store(request(profile_id=str(uuid4()))), service=waiting())
    mg.run_pass(t.rt)
    assert t.store.status(1) == ("failed", "profile_not_supported") and t.calls.read == []


@pytest.mark.parametrize("statuses,ours,expected", [
    ([], False, True), (["pending"], False, True), (["running"], False, "payment_uncertain"),
    (["uncertain"], False, "payment_uncertain"), (["failed"], False, "reading_failed"),
    (["done"], False, "reading_requires_review"),
    (["running"], True, True), (["done"], True, True)])
def test_earlier_paid_attempts_without_a_current_reading_are_never_paid_again(tmp_path, statuses, ours, expected):
    t = make(tmp_path)
    t.store.statuses = statuses
    if ours:
        case = t.state / "cases" / "1" / str(CONVOCATION)
        case.mkdir(parents=True)
        (case / "gemini_extraction-abc.claim.private.json").write_text("{}")
    if expected is True:
        assert mg.needs_reading(t.rt, t.store, request(), CONVOCATION) is True
    else:
        with pytest.raises(mg.Fail) as failure:
            mg.needs_reading(t.rt, t.store, request(), CONVOCATION)
        assert failure.value.reason == expected


@pytest.mark.parametrize("result,reason", [
    ({"outcome": "skipped", "reason": "closed"}, "convocatoria_cerrada"),
    ({"outcome": "skipped", "reason": "reading_failed"}, "reading_failed"),
    ({"outcome": "skipped", "reason": "no_requirements"}, "no_requirements"),
    ({"outcome": "skipped", "reason": "profile_inactive"}, "profile_inactive"),
    ({"outcome": "skipped", "reason": "profile_not_supported"}, "profile_not_supported"),
    ({"outcome": "skipped", "reason": "jev_missing"}, "jev_below_60"),
    ({"outcome": "skipped", "reason": "jev_rejected"}, "jev_below_60"),
    ({"outcome": "skipped", "reason": "no_current_reading"}, "reading_requires_review"),
    ({"outcome": "skipped", "reason": "reservation_failed"}, "invalid_model_output"),
    ({"outcome": "model_output_invalid", "reason": "requirement_coverage_changed"}, "invalid_model_output")])
def test_how_the_evaluation_outcome_fails_the_request(tmp_path, result, reason):
    t = make(tmp_path, service=waiting(), evaluate=lambda *a, **k: result)
    t.store.current = True
    mg.run_pass(t.rt)
    assert t.store.status(1) == ("failed", reason)


@pytest.mark.parametrize("result", [{"outcome": "replayed"}, {"outcome": "skipped", "reason": "match_current"}])
def test_a_current_or_replayed_match_is_done_without_paying(tmp_path, result):
    t = make(tmp_path, service=waiting(), evaluate=lambda *a, **k: result)
    t.store.current = True
    mg.run_pass(t.rt)
    assert t.store.status(1) == ("done", None)


@pytest.mark.parametrize("reason", ["reservation_busy", "profile_changed"])
def test_a_pair_busy_elsewhere_stays_evaluating_and_the_pass_goes_on(tmp_path, reason):
    store = Store(request(1), request(2, source_id=2))
    t = make(tmp_path, store, service=waiting(),
             evaluate=lambda c, s, convocation, *a, **k: {"outcome": "skipped", "reason": reason}
             if convocation == CONVOCATION else {"outcome": "evaluated"})
    store.current = True
    summary = mg.run_pass(t.rt)
    assert store.status(1) == ("evaluating", None) and store.status(2) == ("done", None)
    assert summary["handled"] == {"deferred": 1, "done": 1}


def test_a_provider_rejection_pauses_sending_without_failing_anything(tmp_path):
    store = Store(request(1), request(2, source_id=2))
    t = make(tmp_path, store, service=waiting(), evaluate=lambda *a, **k: {"outcome": "provider_rejected"})
    store.current = True
    summary = mg.run_pass(t.rt)
    assert summary["stopped"] == "provider_rejected" and store.status(1) == ("evaluating", None)
    assert store.status(2) == ("pending", None)
    assert mg.run_pass(t.rt)["skipped"] == "cooldown"
    t.clock.advance(minutes=16)
    assert "skipped" not in mg.run_pass(t.rt)


# --- payment safety ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["read", "evaluation"])
def test_an_uncertain_payment_fails_the_request_opens_the_guard_and_stops_all_sending(tmp_path, source):
    store = Store(request(1), request(2, source_id=2))
    t = make(tmp_path, store, service=waiting())
    if source == "read":
        t.rt.read = lambda *a: (_ for _ in ()).throw(mg.Uncertain("usage_unknown"))
    else:
        store.current = True
        t.rt.evaluate = lambda *a, **k: {"outcome": "payment_uncertain", "reason": "guard_opened"}
    summary = mg.run_pass(t.rt)
    assert store.status(1) == ("failed", "payment_uncertain") and summary["stopped"] == "payment_uncertain"
    assert store.status(2) == ("pending", None)  # Nothing else was claimed.
    guard = json.loads((t.state / mg.GUARD).read_text())
    assert guard["status"] == "open" and guard["request_id"] == 1
    assert len(t.alerts) == 1
    sent = len(t.calls.evaluate) + len(t.calls.read)
    for _ in range(3):  # Later passes send nothing until an operator removes the guard.
        assert mg.run_pass(t.rt)["skipped"] == "guard_open"
    assert len(t.calls.evaluate) + len(t.calls.read) == sent and len(t.alerts) == 1
    assert mg.loop(t.rt, poll_seconds=0, once=True) == 2


def test_blocked_receipts_count_as_a_payment_doubt(tmp_path):
    t = make(tmp_path, service=waiting(), evaluate=lambda *a, **k: {"outcome": "receipts_blocked"})
    t.store.current = True
    mg.run_pass(t.rt)
    assert t.store.status(1) == ("failed", "payment_uncertain") and (t.state / mg.GUARD).exists()


def test_the_guard_never_replaces_an_earlier_incident(tmp_path):
    t = make(tmp_path)
    t.rt.open_guard(7, "first")
    t.rt.open_guard(8, "second")
    assert json.loads((t.state / mg.GUARD).read_text())["request_id"] == 7


def test_the_manual_receipts_are_not_the_services(tmp_path):
    t = make(tmp_path, service=waiting())
    t.store.current = True
    mg.run_pass(t.rt)
    assert t.calls.evaluate[0][3] == t.state / "evaluations" and t.state != t.service_dir


def test_a_historical_unresolved_reservation_fails_only_its_request(tmp_path):
    t = make(tmp_path, service=waiting())
    t.store.statuses = ["uncertain"]
    mg.run_pass(t.rt)
    assert t.store.status(1) == ("failed", "payment_uncertain") and not (t.state / mg.GUARD).exists()


# --- reading one convocatoria with the selected runner (faked) ------------------------------------------------------

class Selected:
    """The parts of ``run_prod2_selected`` that ``read_convocatoria`` touches."""

    def __init__(self, enrich=None):
        self.STOP, self.EGRESS_DOWN, self.EMBEDDING_DISPATCH_STOP = threading.Event(), threading.Event(), threading.Event()
        self.behaviour = enrich or (lambda **call: {"status": "llm1_complete"})
        self.enriched, self.cleaned = [], []

    def SharedPacer(self, interval):
        return ("pacer", interval)

    def enrich(self, conn, settings_, folder, item, manifest, pacer, limits, jev_key, *, capture=None,
               defer_reading=False):
        call = dict(conn=conn, folder=folder, item=item, manifest=manifest, pacer=pacer, limits=limits,
                    jev_key=jev_key, capture=capture, defer_reading=defer_reading)
        self.enriched.append(call)
        return self.behaviour(**call)

    def cleanup_finished_case(self, conn, private, record, manifest):
        self.cleaned.append(record)

    def egress_failure(self, error):
        return False

    def stop_after_uncertain_embedding_error(self, error):
        return None


def reader(tmp_path, selected, **fields):
    manifests = []

    def plan(rt, item, request_id):
        manifests.append(request_id)
        return {"gemini_model": "gemini-test", "request_interval": 2, "run_id": f"prod2-manual-{request_id}"}
    t = make(tmp_path, service=waiting(), selected=lambda rt: selected, plan_manifest=plan,
             fresh_ficha=lambda rt, sel, item, folder, pacer: {"observed_at": NOW.isoformat(), "detail": {}}, **fields)
    t.manifests = manifests
    return t


def case_of(t, request_id=1):
    return t.state / "cases" / str(request_id)


def test_a_read_runs_enrich_in_handoff_mode_with_tight_limits_and_a_fresh_ficha(tmp_path):
    selected = Selected()
    t = reader(tmp_path, selected)
    mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    call, = selected.enriched
    limits = call["limits"]
    assert limits.maximum == {"jev": mg.MAX_SELECTOR_CALLS, "llm1": mg.MAX_LLM1_CALLS, "embedding": mg.MAX_EMBEDDING_CALLS}
    assert (limits.max_accepted, limits.max_candidates) == (1, 1)
    assert isinstance(limits.handoff_guard, HandoffGuards) and limits.handoff_guard.max_candidates == 1
    assert call["defer_reading"] is False and call["capture"]["observed_at"] == NOW.isoformat()
    assert call["folder"] == case_of(t) / str(CONVOCATION) and call["jev_key"] == "key" and call["pacer"] == ("pacer", 2)
    record, = selected.cleaned
    assert record["accepted"] is True and record["manual_request_id"] == 1 and record["convocation_id"] == CONVOCATION
    stored = json.loads((case_of(t) / str(CONVOCATION) / "outcome.private.json").read_text())
    assert stored["status"] == "llm1_complete"


def test_the_manifest_is_frozen_once_and_a_resume_replays_it(tmp_path):
    blocked = [{"status": 403, "source": "prod2", "remaining_seconds": 60}]
    selected = Selected(lambda **call: (_ for _ in ()).throw(RuntimeError("PROD2 HTTP status 403")) if blocked
                        else {"status": "llm1_complete"})
    t = reader(tmp_path, selected, shared_block=lambda: blocked[0] if blocked else None)
    with pytest.raises(mg.Defer):
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    blocked.clear()
    mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert t.manifests == [1] and len(selected.enriched) == 2
    assert selected.enriched[0]["manifest"] == selected.enriched[1]["manifest"]


@pytest.mark.parametrize("status,reason", [
    ("closed_without_reading", "convocatoria_cerrada"), ("opening_not_confirmed", "convocatoria_cerrada"),
    ("documents_missing", "reading_requires_review"), ("selection_needs_review", "reading_requires_review"),
    ("needs_review", "reading_requires_review"), ("selection_complete", "reading_failed")])
def test_a_reading_that_stored_nothing_fails_with_its_reason_and_is_not_repeated(tmp_path, status, reason):
    selected = Selected(lambda **call: {"status": status})
    t = reader(tmp_path, selected)
    for _ in range(2):  # The second attempt reads the settled outcome: no second enrich, so no second payment.
        with pytest.raises(mg.Fail) as failure:
            mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
        assert failure.value.reason == reason
    assert len(selected.enriched) == 1


def test_an_unexpected_error_fails_the_reading_and_keeps_the_receipts(tmp_path):
    selected = Selected(lambda **call: (_ for _ in ()).throw(ValueError("Unsupported official source format")))
    t = reader(tmp_path, selected)
    with pytest.raises(mg.Fail) as failure:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert failure.value.reason == "reading_failed" and case_of(t).exists()


def test_a_review_of_the_embedding_preparation_is_reading_requires_review(tmp_path):
    from buenapro_worker.prod2.embedding_runtime import EmbeddingPreparationReview
    selected = Selected(lambda **call: (_ for _ in ()).throw(EmbeddingPreparationReview("review")))
    t = reader(tmp_path, selected)
    with pytest.raises(mg.Fail) as failure:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert failure.value.reason == "reading_requires_review"


def started_attempt(t, name="gemini-selected-1"):
    case = case_of(t)
    case.mkdir(parents=True, exist_ok=True)
    (case / f"{name}.started.private.json").write_text(json.dumps({"attempt_id": name, "model_call_started": True}))


def test_an_attempt_sent_without_an_answer_is_a_payment_doubt_not_a_plain_failure(tmp_path):
    def sent_then_failed(**call):
        started_attempt(t)
        raise TimeoutError("the connection dropped")
    selected = Selected(sent_then_failed)
    t = reader(tmp_path, selected)
    with pytest.raises(mg.Uncertain) as doubt:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert doubt.value.why == "usage_unknown"


def test_a_crashed_attempt_is_a_payment_doubt_before_anything_is_resumed(tmp_path):
    selected = Selected()
    t = reader(tmp_path, selected)
    started_attempt(t)  # What a crash between "request sent" and "answer saved" leaves behind.
    with pytest.raises(mg.Uncertain):
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert selected.enriched == []  # Nothing was sent again.


def test_a_claim_without_its_response_is_a_payment_doubt(tmp_path):
    selected = Selected()
    t = reader(tmp_path, selected)
    case_of(t).mkdir(parents=True)
    (case_of(t) / "gemini_extraction-abc.claim.private.json").write_text(json.dumps({"id": 3}))
    with pytest.raises(mg.Uncertain):
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert selected.enriched == []


def test_a_claim_with_its_response_resumes_without_a_doubt(tmp_path):
    selected = Selected()
    t = reader(tmp_path, selected)
    case_of(t).mkdir(parents=True)
    (case_of(t) / "gemini_extraction-abc.claim.private.json").write_text(json.dumps({"id": 3}))
    (case_of(t) / "gemini_extraction-abc.response.private.json").write_text(json.dumps({"ok": True}))
    mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert len(selected.enriched) == 1


def test_a_dispatch_stop_marker_of_a_sent_attempt_is_a_payment_doubt(tmp_path):
    selected = Selected()
    t = reader(tmp_path, selected)
    marker = case_of(t) / "handoff-guards" / "dispatch-stop.private.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"version": "prod2_handoff_guards_v1", "reason": mg.DISPATCH_STOP}))
    with pytest.raises(mg.Uncertain):
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert selected.enriched == []


def test_an_unresolved_earlier_reservation_fails_a_failed_read_as_payment_uncertain(tmp_path):
    selected = Selected(lambda **call: (_ for _ in ()).throw(RuntimeError("Selector needs reconciliation")))
    t = reader(tmp_path, selected)
    t.store.unresolved_rows = [{"stage": "selector", "status": "uncertain"}]
    with pytest.raises(mg.Fail) as failure:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert failure.value.reason == "payment_uncertain"


@pytest.mark.parametrize("trigger,why", [("block", "source_blocked"), ("egress", "egress_unavailable"),
                                         ("stop", "shutdown")])
def test_a_source_block_an_egress_failure_or_a_shutdown_loses_nothing(tmp_path, trigger, why):
    selected = Selected(lambda **call: (_ for _ in ()).throw(RuntimeError("PROD2 HTTP status 403")))
    fields = {"shared_block": lambda: {"status": 403, "source": "prod2", "remaining_seconds": 60}} if trigger == "block" else {}
    t = reader(tmp_path, selected, **fields)
    if trigger == "egress":
        selected.EGRESS_DOWN.set()
        selected.behaviour = lambda **call: (selected.EGRESS_DOWN.set(), (_ for _ in ()).throw(RuntimeError("proxy")))[1]
    if trigger == "stop":
        t.rt.stop.set()
    with pytest.raises(mg.Defer) as defer:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert defer.value.why == why and defer.value.stop is True
    assert not (case_of(t) / str(CONVOCATION) / "outcome.private.json").exists()  # Resumable.


def test_a_403_with_the_shared_gate_off_still_pauses_the_consumer(tmp_path):
    def forbidden(**call):
        selected.STOP.set()  # What source_hooks do on a 403/429 before raising.
        raise RuntimeError("PROD2 HTTP status 403")
    selected = Selected(forbidden)
    t = reader(tmp_path, selected)
    with pytest.raises(mg.Defer) as defer:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert defer.value.why == "source_blocked" and t.rt.cooldown_until == NOW + mg.SOURCE_COOLDOWN
    assert mg.gate(t.rt, NOW + timedelta(hours=1))[0] == "cooldown"
    assert mg.gate(t.rt, NOW + timedelta(hours=4))[0] is None


def test_a_payment_doubt_outranks_a_source_block(tmp_path):
    def blocked_after_sending(**call):
        started_attempt(t)
        selected.STOP.set()
        raise RuntimeError("PROD2 HTTP status 403")
    selected = Selected(blocked_after_sending)
    t = reader(tmp_path, selected, shared_block=lambda: {"status": 403, "source": "prod2", "remaining_seconds": 60})
    with pytest.raises(mg.Uncertain):
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)


def test_a_shutdown_requested_while_preparing_starts_no_read(tmp_path):
    selected = Selected()
    t = reader(tmp_path, selected)
    t.rt.stop.set()
    with pytest.raises(mg.Defer) as defer:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert defer.value.why == "shutdown" and selected.enriched == []


def test_a_failed_cleanup_never_fails_a_stored_reading(tmp_path):
    selected = Selected()
    selected.cleanup_finished_case = lambda *a: (_ for _ in ()).throw(OSError("disk"))
    t = reader(tmp_path, selected)
    mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert [line["event"] for line in t.lines] == ["cleanup_error"]


def test_a_read_stopped_by_the_watchdog_waits_for_the_next_window(tmp_path):
    from buenapro_worker.prod2.selected_control import LimitReached
    selected = Selected(lambda **call: (_ for _ in ()).throw(LimitReached("Source stop requested")))
    t = reader(tmp_path, selected)
    t.rt.watchdog = SimpleNamespace(reason="cycle_imminent")
    t.rt.aborted = True
    with pytest.raises(mg.Defer) as defer:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert defer.value.why == "cycle_imminent"


def test_a_convocatoria_without_a_frozen_source_observation_has_no_ficha(tmp_path):
    t = reader(tmp_path, Selected())
    t.store.item_row = None
    with pytest.raises(mg.Fail) as failure:
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert failure.value.reason == "ficha_not_found"


def test_a_missing_provider_key_waits_instead_of_failing(tmp_path):
    selected = Selected()
    t = reader(tmp_path, selected, jev_key=lambda: "")
    with pytest.raises(mg.Defer):
        mg.read_convocatoria(t.rt, t.store, None, request(), CONVOCATION)
    assert selected.enriched == []


def test_a_closed_convocatoria_found_by_the_fresh_ficha_fails_before_any_paid_call(tmp_path):
    t = make(tmp_path, service=waiting())
    t.rt.read = lambda rt, store, owner, req, convocation: mg.settle_reading({"status": "closed_without_reading"})
    mg.run_pass(t.rt)
    assert t.store.status(1) == ("failed", "convocatoria_cerrada") and t.calls.evaluate == []


# --- locating the ficha ---------------------------------------------------------------------------------------------

def test_the_ficha_is_searched_around_its_publication_day_before_the_whole_window():
    query = {"keyword": "software", "object_type": "service", "year": 2026, "seace_version": "3",
             "published_from": "2026-10-01", "published_to": "2026-10-30"}
    narrow, = mg.narrowed(query, datetime(2026, 10, 20).date())
    assert (narrow[0]["published_from"], narrow[0]["published_to"]) == ("2026-10-19", "2026-10-21")
    edge, = mg.narrowed(query, datetime(2026, 10, 30).date())
    assert edge[0]["published_to"] == "2026-10-30"  # Never beyond the original window.
    assert mg.narrowed({**query, "published_from": "2026-10-20", "published_to": "2026-10-21"},
                       datetime(2026, 10, 20).date()) == []  # Already as narrow.
    assert mg.narrowed(query, None) == []


def service_config(tmp_path, **changes):
    from buenapro_worker.prod2 import service
    value = {"version": 1, "enabled": True, "first_epoch_id": "synthetic-final", "interval_seconds": 900,
             "max_cycles": 10000, "start_time": "01:00",
             "window": {"policy": "rolling_days", "days": 3, "first_to": "2026-10-30", "zone": "America/Lima"},
             "base_argv": ["--local", "--preserve-prior-readings", "--business-scope", "todas",
                           "--document-storage", "official", "--llm1-input", "embeddings", "--llm1-scope", "qualification",
                           "--llm1-model-routing", "fixed", "--llm1-fallback", "none", "--llm1-min-affinity", "85",
                           "--embedding-config", str(ROOT / "docs/sources/prod2-embedding-reading.runtime.v6.json"),
                           "--years", "window", "--max-accepted", "400", "--max-candidates", "2000",
                           "--max-jev-calls", "2000", "--max-llm1-calls", "120", "--max-embedding-calls", "1000",
                           "--search-workers", "1", "--processing-workers", "1", "--model-workers", "1",
                           "--request-interval", "10"], **changes}
    path = tmp_path / "service.private.json"
    path.write_bytes(service._canonical(value))
    path.chmod(0o600)
    return path


def test_the_manifest_is_planned_by_the_service_from_its_own_config(tmp_path):
    from buenapro_worker.prod2 import service
    t = make(tmp_path, service_config=service_config(tmp_path), project_root=ROOT, load_config=service._config)
    item = Store().item_row
    manifest = mg.build_manifest(t.rt, item, 7)
    assert manifest["run_id"] == "prod2-manual-7" and manifest["profile_id"] == MEMBER_IDS[0]
    assert manifest["resolved_years"] == [2026] and manifest["gemini_model"] == "gemini-test"
    assert (manifest["published_from"], manifest["published_to"]) == ("2026-10-28", "2026-10-30")
    # The paid reading policy is the service's own, not a copy of it.
    assert manifest["llm1_input_policy"] == "prod2_embedding_retrieved_text_v1"
    assert manifest["document_storage_policy"] == "prod2_official_links_temporary_v1"
    assert manifest["request_interval"] == 10 and manifest["requested"]["llm1_min_affinity"] == 85
    assert manifest["embedding_reading"]["config_sha256"] and "llm1_luna_fallback" not in manifest
    assert not (t.state / "plan").exists()  # Planning writes nothing.


@pytest.mark.parametrize("changes", [{"enabled": False}, {"operator_hold": "source_403"}])
def test_a_disabled_service_config_defers_the_read(tmp_path, changes):
    from buenapro_worker.prod2 import service
    path = tmp_path / "service.private.json"
    path.write_bytes(service._canonical({"version": 1, **changes}))
    path.chmod(0o600)
    t = make(tmp_path, service_config=path, project_root=ROOT, load_config=service._config)
    with pytest.raises(mg.Defer):
        mg.build_manifest(t.rt, Store().item_row, 7)


class Source:
    """A scripted ``Prod2HttpClient``: pages by publication window, rows as the native search returns them."""
    searches = []

    def __init__(self, http):
        self.rows, self.counter, self.search_snapshot, self.window, self.pages = [], (), None, None, []

    def search(self, keyword, *, year, object_type, published_from="", published_to="", **_):
        Source.searches.append((published_from, published_to))
        self.window = (published_from, published_to)
        self.pages = Source.pages.get(self.window, [[]])
        self.search_snapshot = {"total_pages": len(self.pages)}
        self.rows, self.counter = self.pages[0], (1, 1, 1, 1, len(self.pages))

    def search_page(self, number):
        self.rows, self.counter = self.pages[number - 1], (1, 1, 1, number, len(self.pages))

    def detail(self, index, *, all_documents=False):
        found = self.rows[index]
        return {"convocatoria_id": found["convocatoria_id"], "procedure_id": found["procedure_id"],
                "description": Source.description, "documents": [], "http_status": 200, "redirect_statuses": []}


@contextmanager
def no_http(**kwargs):
    yield object()


def listed(convocation=CONVOCATION, procedure=2, description="Licencias"):
    return {"convocatoria_id": convocation, "procedure_id": procedure, "description": description}


@pytest.fixture
def source(monkeypatch):
    Source.searches, Source.pages, Source.description = [], {}, "Licencias"
    monkeypatch.setattr(mg, "Prod2HttpClient", Source)
    monkeypatch.setattr(mg, "seace_http_client", no_http)
    return Source


def locate(tmp_path, item, case="case"):
    selected = SimpleNamespace(source_hooks=lambda pacer: {})
    return mg.locate_ficha(make(tmp_path).rt, selected, item, tmp_path / case, None)


def wide(published_day):
    """The accepted item whose query covered the whole month, published on ``published_day``."""
    base = Store().item_row
    return {**base, "query": {**base["query"], "published_from": "2026-10-01", "published_to": "2026-10-30"},
            "published_day": published_day}


def test_the_ficha_is_found_in_the_narrow_window_first_and_saved(tmp_path, source):
    source.pages = {("28/10/2026", "30/10/2026"): [[listed(1)], [listed(), listed(3)]]}
    capture = locate(tmp_path, wide(datetime(2026, 10, 29).date()))
    assert capture["detail"]["convocatoria_id"] == CONVOCATION and "http_status" not in capture["detail"]
    assert source.searches == [("28/10/2026", "30/10/2026")]  # The whole month was not searched.
    assert list((tmp_path / "case" / "ficha-observations").glob("*/detail.private.json"))


def test_the_ficha_falls_back_to_the_whole_window_and_is_bounded(tmp_path, source):
    source.pages = {("28/10/2026", "30/10/2026"): [[listed(1)]], ("01/10/2026", "30/10/2026"): [[listed(1)]] * 3 + [[listed()]]}
    assert locate(tmp_path, wide(datetime(2026, 10, 29).date()))["detail"]["procedure_id"] == 2
    assert source.searches == [("28/10/2026", "30/10/2026"), ("01/10/2026", "30/10/2026")]
    source.pages = {("01/10/2026", "30/10/2026"): [[listed(1)]] * 39 + [[listed()]]}  # Page 40: the last one read.
    assert locate(tmp_path, wide(None), "last")["detail"]["procedure_id"] == 2
    source.pages = {("01/10/2026", "30/10/2026"): [[listed(1)]] * 45 + [[listed()]]}
    with pytest.raises(mg.Fail) as failure:
        locate(tmp_path, wide(None), "bounded")
    assert failure.value.reason == "ficha_not_found"


@pytest.mark.parametrize("found", [listed(description="Otro texto"), listed(procedure=9)])
def test_a_ficha_that_changed_or_is_another_procedure_is_not_the_accepted_one(tmp_path, source, found):
    source.pages = {("01/10/2026", "30/10/2026"): [[found]]}
    with pytest.raises(mg.Fail) as failure:
        locate(tmp_path, wide(None))
    assert failure.value.reason == "ficha_not_found"


def test_the_detail_of_another_text_is_refused(tmp_path, source):
    source.pages = {("01/10/2026", "30/10/2026"): [[listed()]]}
    source.description = "Texto distinto"
    with pytest.raises(mg.Fail):
        locate(tmp_path, wide(None))


# --- the one-pair evaluation and its overrides ------------------------------------------------------------------

def pair_rows(**fields):
    return [row(profile_id=VEYON, **fields), row(profile_id=DIN, **fields)]


def test_the_manual_checks_skip_only_the_scheduling_rules():
    scheduled = {"domain": "industrial_electrica", "rule_enabled": False, "hours_left": 2.0, "min_hours": 24}
    assert evaluation.skip_reason(row(profile_id=VEYON, **scheduled)) == "domain_not_for_company"
    assert evaluation.skip_reason(row(profile_id=VEYON, **scheduled), manual=True) is None
    assert evaluation.skip_reason(row(rule_enabled=False)) == "rule_disabled"
    assert evaluation.skip_reason(row(rule_enabled=False), manual=True) is None
    assert evaluation.skip_reason(row(hours_left=2.0, min_hours=24)) == "too_close_to_close"
    assert evaluation.skip_reason(row(hours_left=2.0, min_hours=24), manual=True) is None


@pytest.mark.parametrize("fields,reason", [
    ({"open": False}, "closed"), ({"quality": "failed"}, "reading_failed"), ({"facet_count": 0}, "no_requirements"),
    ({"profile_active": False}, "profile_inactive"), ({"domain": None, "affinity": None}, "jev_missing"),
    ({"affinity": 59.9}, "jev_rejected"), ({"profile_hash": None}, "profile_hash_missing"),
    ({"match_current": True}, "match_current")])
def test_the_manual_checks_keep_everything_that_would_waste_a_payment(fields, reason):
    assert evaluation.skip_reason(row(**fields), manual=True) == reason


def test_a_manual_pair_evaluates_only_the_requesting_company(ledger):
    analyzer = Analyzer()
    outcome = evaluation.evaluate_manual(Conn(pair_rows(max_daily=1), today=99), settings(), CONVOCATION, DIN,
                                         request_id=9, analyzer=analyzer)
    assert outcome["outcome"] == "evaluated" and len(analyzer.calls) == 1
    (convocation, claim), = ledger.claims
    assert convocation == CONVOCATION and claim["profile_id"] == DIN and claim["kind"] == "gemini_match"


def test_a_manual_pair_ignores_the_daily_limit_the_rule_the_domain_and_the_hours_left(ledger):
    analyzer = Analyzer()
    rows = [row(profile_id=VEYON, domain="industrial_electrica", rule_enabled=False, max_daily=1, min_hours=24,
                hours_left=1.0)]
    outcome = evaluation.evaluate_manual(Conn(rows, today=50, menores=50), settings(), CONVOCATION, VEYON,
                                         request_id=3, analyzer=analyzer)
    assert outcome["outcome"] == "evaluated" and len(analyzer.calls) == 1


def test_a_manual_pair_for_an_unserved_company_costs_nothing(ledger):
    analyzer = Analyzer()
    conn = Conn(pair_rows())
    outcome = evaluation.evaluate_manual(conn, settings(), CONVOCATION, str(uuid4()), request_id=3, analyzer=analyzer)
    assert outcome == {"outcome": "skipped", "reason": "profile_not_supported"} and analyzer.calls == []
    assert ledger.claims == []


@pytest.mark.parametrize("fields,reason", [({"open": False}, "closed"), ({"match_current": True}, "match_current"),
                                           ({"facet_count": 0}, "no_requirements"), ({"quality": "failed"}, "reading_failed")])
def test_a_manual_pair_skipped_for_free_never_reserves_or_sends(ledger, fields, reason):
    analyzer = Analyzer()
    outcome = evaluation.evaluate_manual(Conn(pair_rows(**fields)), settings(), CONVOCATION, VEYON,
                                         request_id=3, analyzer=analyzer)
    assert outcome == {"outcome": "skipped", "reason": reason} and analyzer.calls == [] and ledger.claims == []


def test_a_company_without_a_reading_row_is_no_current_reading(ledger):
    outcome = evaluation.evaluate_manual(Conn([]), settings(), CONVOCATION, VEYON, request_id=3, analyzer=Analyzer())
    assert outcome == {"outcome": "skipped", "reason": "no_current_reading"}


def test_a_manual_match_is_tagged_with_its_origin_and_request(ledger):
    conn = Conn()
    evaluation.evaluate_pair(conn, settings(), Analyzer(), row(), manual_request_id=41)
    (_, stored, _), = ledger.completed
    meta = stored["breakdown"]["meta"]
    assert meta["execution"] == "manual" and meta["evaluation_origin"] == evaluation.MANUAL_ORIGIN == "prod2_manual_request_v1"
    assert meta["manual_request_id"] == 41 and "worker_auto" not in json.dumps(meta)
    sql, params = conn.updates[0]  # The reservation is tagged while the call is in flight.
    assert json.loads(params[0]) == {"evaluation_origin": evaluation.MANUAL_ORIGIN, "manual_request_id": 41}


def test_an_automatic_match_keeps_its_origin(ledger):
    conn = Conn()
    evaluation.evaluate_pair(conn, settings(), Analyzer(), row())
    meta = ledger.completed[0][1]["breakdown"]["meta"]
    assert meta["execution"] == "worker_auto" and meta["evaluation_origin"] == evaluation.AUTO_ORIGIN
    assert "manual_request_id" not in meta and conn.updates == []


@pytest.mark.parametrize("error,status", [(ProviderRejectedError(503, "UNAVAILABLE"), "pending"),
                                          (GuardedReadingError("uncertain"), "uncertain")])
def test_a_manual_reservation_that_ends_without_a_match_stays_tagged(ledger, error, status):
    conn = Conn()
    evaluation.evaluate_pair(conn, settings(), Analyzer(error), row(), manual_request_id=41)
    tag = {"evaluation_origin": evaluation.MANUAL_ORIGIN, "manual_request_id": 41}
    if status == "uncertain":
        assert json.loads(conn.updates[-1][1][1]) == {"reason": "sent_request_without_durable_response", **tag}
    else:
        assert json.loads(conn.updates[0][1][0]) == tag  # A released reservation keeps the tag it got at claim.


def test_manual_evaluations_do_not_count_toward_the_automatic_daily_limit():
    seen = []

    class Counting(Conn):
        def execute(self, sql, params=None):
            if "kind = 'gemini_match' AND policy_version" in sql:
                seen.append((sql, params))
            return super().execute(sql, params)
    rows = [row(id_convocatoria=n, profile_id=DIN, max_daily=3) for n in range(1, 5)]
    planned = evaluation.plan(Counting(rows, today=1), max_calls=10)
    assert len(planned["planned"]) == 2 and planned["skipped"] == {"daily_limited": 2}
    sql, params = seen[0]
    assert params == (evaluation.POLICY_VERSION, DIN, evaluation.MANUAL_ORIGIN)
    assert "IS DISTINCT FROM %s" in sql and "evaluation_origin" in sql


# --- the entry point ---------------------------------------------------------------------------------------------

def script():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import grade_prod2_manual
        return grade_prod2_manual
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def test_the_script_needs_a_mode_and_refuses_unsafe_starts(tmp_path, capsys):
    entry = script()
    config = tmp_path / "service.json"
    config.write_text("{}")
    base = ["--state-dir", str(tmp_path / "s"), "--service-state-dir", str(tmp_path), "--service-config", str(config)]
    with pytest.raises(SystemExit):
        entry.parser().parse_args(base)
    with pytest.raises(SystemExit):
        entry.parser().parse_args(["--loop", "--once", *base])
    assert entry.main(["--once", *base, "--safety-minutes", "5"]) == 2
    assert entry.main(["--once", "--state-dir", str(tmp_path / "s"), "--service-state-dir", str(tmp_path / "missing"),
                       "--service-config", str(config)]) == 2  # An unmounted state directory must not read as "idle".
    assert "service_paths_missing" in capsys.readouterr().out
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    assert entry.main(["--once", "--state-dir", str(loose), "--service-state-dir", str(tmp_path),
                       "--service-config", str(config)]) == 2


# --- PostgreSQL (opt in) -------------------------------------------------------------------------------------------

@pytest.fixture
def db():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        conn.execute("SET TIME ZONE 'UTC'")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def seed_profiles(conn):
    tenant = conn.execute("INSERT INTO tenants (name) VALUES ('manual-grading-test') RETURNING id").fetchone()["id"]
    for index, profile in enumerate(MEMBER_IDS):
        conn.execute("""INSERT INTO company_profiles (id, tenant_id, ruc, razon_social, identity_json, profile_hash, is_active)
            VALUES (%s, %s, %s, %s, '{"descripcion": "Software"}', %s, true)
            ON CONFLICT (id) DO UPDATE SET is_active = true""", (profile, tenant, f"2088{index}{uuid4().int % 10**6:06d}",
                                                                   f"EMPRESA {index}", "f" * 32))
    return tenant


def seed_request(conn, tenant, *, convocation=CONVOCATION, profile=VEYON, status="pending", source="seace_prod2",
                 mode="queued", age=timedelta(0), created=None):
    return conn.execute(
        """INSERT INTO manual_grading_requests (tenant_id, profile_id, source_key, source_id, mode, status,
             created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now() - %s::interval, now() - %s::interval)
           RETURNING id""", (tenant, profile, source, convocation, mode, status, created or age, age)).fetchone()["id"]


def seed_convocatoria(conn, convocation=CONVOCATION, *, affinity=96, closes_in_hours=48, member=True):
    opportunity = conn.execute("""INSERT INTO opportunities (record_kind, object_type, procurement_method, lifecycle_stage,
        participation_access, actionability, consortium_status, subcontracting_status)
        VALUES ('opportunity', 'service', 'selection_procedure', 'open', 'public', 'actionable', 'not_identified',
                'not_identified') RETURNING id""").fetchone()["id"]
    conn.execute("""INSERT INTO prod2_discoveries (id_convocatoria, id_procedimiento, opportunity_id, nomenclature,
        buyer_name, description, object_type, object_label, published_at, currency, seace_version, row_hash,
        first_seen_at, last_seen_at) VALUES (%s, %s, %s, 'AS-1', 'Entidad', 'Licencias', 'service', 'Servicio',
        '2026-10-29 15:00-05', 'Soles', '3', %s, now(), now())""", (convocation, convocation + 1, opportunity, "a" * 64))
    if closes_in_hours is not None:
        conn.execute("""INSERT INTO prod2_details (id_convocatoria, opportunity_id, detail_hash, detail_fetched_at,
            source_url, proposals_closes_at, raw_detail) VALUES (%s, %s, %s, now(), 'https://seace.invalid', now() + %s::interval,
            '{}')""", (convocation, opportunity, "b" * 64, timedelta(hours=closes_in_hours)))
    key = uuid4().hex + uuid4().hex
    conn.execute("""INSERT INTO prod2_jev_decision_cache (cache_key, convocation_id, model, policy_version, status,
        completed_at, result_json) VALUES (%s, %s, 'm', %s, 'done', now(), '{}')""", (key, convocation, evaluation.JEV_POLICY))
    conn.execute("""INSERT INTO prod2_evaluations (evaluation_key, id_convocatoria, kind, request_hash, model, policy_version,
        status, completed_at, result_json) VALUES (%s, %s, 'jev_affinity', %s, 'm', %s, 'done', now(), %s::jsonb)""",
                 (uuid4().hex + uuid4().hex, convocation, key, evaluation.JEV_POLICY,
                  json.dumps({"affinity_index": affinity, "domain": "informatica"})))
    if member:
        row_manifest = {"convocation_id": convocation, "procedure_id": convocation + 1, "description": "Licencias",
                        "object_type": "service"}
        query = {"keyword": "software", "object_type": "service", "year": 2026, "published_from": "2026-10-28",
                 "published_to": "2026-10-30", "seace_version": "3"}
        conn.execute("""INSERT INTO prod2_accepted_run_members (run_id, convocation_id, request_hash, accepted, result_json,
            opportunity_id, query_manifest, row_manifest, observed_at) VALUES ('run-1', %s, %s, true, '{}', %s, %s::jsonb,
            %s::jsonb, now())""", (convocation, key, opportunity, json.dumps(query), json.dumps(row_manifest)))
    return opportunity


@needs_db
def test_claim_takes_the_oldest_pending_once_and_bumps_updated_at(db):
    tenant = seed_profiles(db)
    newer = seed_request(db, tenant, convocation=1, age=timedelta(minutes=1))
    older = seed_request(db, tenant, convocation=2, age=timedelta(minutes=5))
    seed_request(db, tenant, convocation=3, source="seace_prod6", age=timedelta(hours=9))  # Not ours.
    seed_request(db, tenant, convocation=4, mode="direct", age=timedelta(hours=9))  # Evaluated by the web.
    before = db.execute("SELECT updated_at FROM manual_grading_requests WHERE id = %s", (older,)).fetchone()["updated_at"]
    store = mg.Requests(db)
    first, second = store.claim(), store.claim()
    assert (first["id"], first["status"]) == (older, "reading") and second["id"] == newer
    assert store.claim() is None
    after = db.execute("SELECT status, updated_at FROM manual_grading_requests WHERE id = %s", (older,)).fetchone()
    assert after["status"] == "reading" and after["updated_at"] > before
    others = db.execute("SELECT count(*) AS n FROM manual_grading_requests WHERE status = 'pending'").fetchone()["n"]
    assert others >= 2  # The prod6 and the direct rows stayed pending.


@needs_db
def test_every_transition_is_a_compare_and_set_that_bumps_updated_at(db):
    tenant = seed_profiles(db)
    seed_request(db, tenant, convocation=11)
    store = mg.Requests(db)
    claimed = store.claim()
    stale = {**claimed, "status": "pending"}
    assert store.move(stale, "reading") is False  # Already taken: no second owner.
    moved = db.execute("SELECT updated_at FROM manual_grading_requests WHERE id = %s", (claimed["id"],)).fetchone()["updated_at"]
    assert store.move(claimed, "evaluating") and claimed["status"] == "evaluating"
    assert store.move(claimed, "done") and not store.move(claimed, "failed", "reading_failed")
    row_ = db.execute("SELECT status, reason, updated_at FROM manual_grading_requests WHERE id = %s",
                      (claimed["id"],)).fetchone()
    assert (row_["status"], row_["reason"]) == ("done", None) and row_["updated_at"] >= moved
    seed_request(db, tenant, convocation=12)
    failed = store.claim()
    assert store.move(failed, "failed", "contract_not_found")
    assert db.execute("SELECT reason FROM manual_grading_requests WHERE id = %s", (failed["id"],)).fetchone()["reason"] == "contract_not_found"


@needs_db
def test_requests_stuck_for_a_day_expire_as_reading_timeout_and_nothing_else(db):
    tenant = seed_profiles(db)
    stuck = [seed_request(db, tenant, convocation=21, status="reading", age=timedelta(hours=25)),
             seed_request(db, tenant, convocation=22, status="evaluating", age=timedelta(hours=24, minutes=1))]
    kept = [seed_request(db, tenant, convocation=23, status="reading", age=timedelta(hours=23)),
            seed_request(db, tenant, convocation=24, status="pending", age=timedelta(hours=90)),
            seed_request(db, tenant, convocation=25, status="reading", age=timedelta(hours=90), source="seace_prod6"),
            seed_request(db, tenant, convocation=26, status="evaluating", age=timedelta(hours=90), mode="direct")]
    assert mg.Requests(db).expire() >= 2
    rows = {r["id"]: r for r in db.execute("SELECT id, status, reason FROM manual_grading_requests WHERE id = ANY(%s)",
                                           (stuck + kept,)).fetchall()}
    assert all((rows[i]["status"], rows[i]["reason"]) == ("failed", "reading_timeout") for i in stuck)
    assert [rows[i]["status"] for i in kept] == ["reading", "pending", "reading", "evaluating"]


@needs_db
def test_has_work_and_the_active_list(db):
    tenant = seed_profiles(db)
    store = mg.Requests(db)
    db.execute("UPDATE manual_grading_requests SET status = 'done' WHERE status IN ('pending', 'reading', 'evaluating')")
    assert store.has_work() is False
    pending = seed_request(db, tenant, convocation=31)
    resumed = seed_request(db, tenant, convocation=32, status="evaluating", age=timedelta(hours=1))
    assert store.has_work() is True
    assert [r["id"] for r in store.active()] == [resumed] and pending


@needs_db
def test_the_free_checks_read_the_real_schema(db):
    tenant = seed_profiles(db)
    seed_convocatoria(db, CONVOCATION, affinity=71.5, closes_in_hours=-2)
    store = mg.Requests(db)
    facts = store.facts(CONVOCATION, VEYON)
    assert facts["found"] and facts["profile_active"] and float(facts["affinity"]) == 71.5
    assert facts["proposals_closes_at"] < datetime.now(timezone.utc)
    assert store.facts(7, VEYON)["found"] is False and store.facts(7, VEYON)["affinity"] is None
    db.execute("UPDATE company_profiles SET is_active = false WHERE id = %s", (VEYON,))
    assert store.facts(CONVOCATION, VEYON)["profile_active"] is False
    item = store.item(CONVOCATION)
    assert item["row"]["convocation_id"] == CONVOCATION and item["query"]["keyword"] == "software"
    assert str(item["published_day"]) == "2026-10-29" and store.item(8) is None and tenant


@needs_db
def test_a_convocatoria_without_a_frozen_observation_has_no_item(db):
    seed_profiles(db)
    seed_convocatoria(db, 4401, member=False)
    assert mg.Requests(db).item(4401) is None


@needs_db
def test_the_reading_state_and_the_earlier_attempts(db):
    seed_profiles(db)
    opportunity = seed_convocatoria(db, 4402)
    store = mg.Requests(db)
    assert store.reading(4402) == (False, [])
    db.execute("""INSERT INTO prod2_evaluations (evaluation_key, id_convocatoria, kind, request_hash, model, policy_version,
        profile_id, status, result_json) VALUES (%s, 4402, 'gemini_extraction', %s, 'm', 'prod2_document_extraction_v1',
        %s, 'failed', '{}'::jsonb)""", (uuid4().hex + uuid4().hex, "c" * 64, MEMBER_IDS[0]))
    assert store.reading(4402) == (False, ["failed"])
    code = uuid4()
    db.execute("""INSERT INTO prod2_documents (id_convocatoria, codigo_alfresco, name, document_type, stage_name,
        published_at_text, source_url, first_seen_at, last_seen_at) VALUES (4402, %s, 'bases.pdf', 'Bases', 'Convocatoria',
        '29/10/2026', 'https://seace.invalid/x', now(), now())""", (code,))
    db.execute("""INSERT INTO prod2_document_downloads (id_convocatoria, codigo_alfresco, sha256_original, bytes,
        content_type, format) VALUES (4402, %s, %s, 10, 'application/pdf', 'pdf')""", (code, "d" * 64))
    evaluation_id = db.execute("""INSERT INTO prod2_evaluations (evaluation_key, id_convocatoria, kind, request_hash, model,
        policy_version, profile_id, status, completed_at, result_json) VALUES (%s, 4402, 'gemini_extraction', %s, 'm',
        'prod2_document_extraction_v1', %s, 'done', now(), '{}'::jsonb) RETURNING id""",
                              (uuid4().hex + uuid4().hex, "e" * 64, MEMBER_IDS[0])).fetchone()["id"]
    db.execute("""INSERT INTO prod2_document_extractions (id_convocatoria, opportunity_id, codigo_alfresco, sha256_original,
        evaluation_id, model, prompt_version, schema_version, raw_extraction_json, requires_human_review, quality)
        VALUES (4402, %s, %s, %s, %s, 'm', 'p', 's', '{}'::jsonb, true, 'auto')""", (opportunity, code, "d" * 64, evaluation_id))
    assert store.reading(4402) == (True, [])  # Under review and flagged: still a current reading for the evaluator.
    db.execute("UPDATE prod2_documents SET is_listed = false WHERE id_convocatoria = 4402")
    assert store.reading(4402)[0] is False
    assert store.unresolved(4402) == []
    db.execute("UPDATE prod2_evaluations SET status = 'running', claim_token = gen_random_uuid(), lease_until = now() "
               "WHERE id = %s", (evaluation_id,))
    assert store.unresolved(4402) == [{"stage": "llm1", "status": "running"}]


@needs_db
def test_manual_evaluations_are_not_the_automatic_budget_in_the_real_ledger(db):
    seed_profiles(db)
    seed_convocatoria(db, 4403)

    def evaluation_row(status, result):
        db.execute("""INSERT INTO prod2_evaluations (evaluation_key, id_convocatoria, kind, request_hash, model, policy_version,
            profile_id, status, completed_at, result_json) VALUES (%s, 4403, 'gemini_match', %s, 'm', %s, %s, %s,
            CASE WHEN %s = 'done' THEN now() END, %s::jsonb)""", (uuid4().hex + uuid4().hex, uuid4().hex + uuid4().hex,
                                                                    evaluation.POLICY_VERSION, DIN, status, status,
                                                                    json.dumps(result) if result is not None else None))
    before = db.execute(evaluation.TODAY_SQL, (evaluation.POLICY_VERSION, DIN, evaluation.MANUAL_ORIGIN)).fetchone()["total"]
    tag = {"evaluation_origin": evaluation.MANUAL_ORIGIN, "manual_request_id": 5}
    evaluation_row("done", {"breakdown": {"meta": tag}})  # A finished manual match.
    evaluation_row("failed", {"reason": "requirement_coverage_changed", **tag})  # Failed after being paid.
    evaluation_row("pending", tag)  # Released after a provider rejection.
    evaluation_row("done", {"breakdown": {"meta": {"evaluation_origin": evaluation.AUTO_ORIGIN}}})  # Automatic.
    evaluation_row("done", {"breakdown": {"meta": {}}})  # Older automatic rows without an origin.
    evaluation_row("failed", {"reason": "x"})
    evaluation_row("pending", None)
    after = db.execute(evaluation.TODAY_SQL, (evaluation.POLICY_VERSION, DIN, evaluation.MANUAL_ORIGIN)).fetchone()["total"]
    assert after - before == 4


def seed_reading(conn, convocation, opportunity, *, review=True):
    """A current reading of two requirements (equipment and an RNP annex) listed in the ficha."""
    code, sha = uuid4(), "d" * 64
    conn.execute("""INSERT INTO prod2_documents (id_convocatoria, codigo_alfresco, name, document_type, stage_name,
        published_at_text, source_url, first_seen_at, last_seen_at) VALUES (%s, %s, 'bases.pdf', 'Bases', 'Convocatoria',
        '29/10/2026', 'https://seace.invalid/x', now(), now())""", (convocation, code))
    conn.execute("""INSERT INTO prod2_document_downloads (id_convocatoria, codigo_alfresco, sha256_original, bytes,
        content_type, format) VALUES (%s, %s, %s, 10, 'application/pdf', 'pdf')""", (convocation, code, sha))
    reservation = conn.execute("""INSERT INTO prod2_evaluations (evaluation_key, id_convocatoria, kind, request_hash, model,
        policy_version, profile_id, status, completed_at, result_json) VALUES (%s, %s, 'gemini_extraction', %s, 'm',
        'prod2_document_extraction_v1', %s, 'done', now(), '{}'::jsonb) RETURNING id""",
                               (uuid4().hex + uuid4().hex, convocation, "e" * 64, MEMBER_IDS[0])).fetchone()["id"]
    extraction = conn.execute("""INSERT INTO prod2_document_extractions (id_convocatoria, opportunity_id, codigo_alfresco,
        sha256_original, evaluation_id, model, prompt_version, schema_version, raw_extraction_json, summary_json,
        requires_human_review, facet_count) VALUES (%s, %s, %s, %s, %s, 'm', 'p', 's', %s::jsonb, '{}'::jsonb, %s, 2)
        RETURNING id""", (convocation, opportunity, code, sha, reservation, json.dumps(
            {"requirements": {"equipment": [{"name": "Servidor", "required": True}]}}), review)).fetchone()["id"]
    for facet, label in (("equipment", "Servidor"), ("rnp", "RNP vigente")):
        conn.execute("""INSERT INTO prod2_requirement_facets (id_convocatoria, opportunity_id, extraction_id, facet, label,
            required, facet_hash) VALUES (%s, %s, %s, %s, %s, true, %s)""",
                     (convocation, opportunity, extraction, facet, label, uuid4().hex + uuid4().hex))
    return extraction


@needs_db
def test_a_manual_pair_runs_end_to_end_on_the_real_ledger_and_leaves_the_automatic_budget_alone(db):
    seed_profiles(db)
    opportunity = seed_convocatoria(db, 4405)
    seed_reading(db, 4405, opportunity)
    analyzer = Analyzer()
    outcome = evaluation.evaluate_manual(db, settings(), 4405, DIN, request_id=77, analyzer=analyzer)
    assert outcome["outcome"] == "evaluated" and len(analyzer.calls) == 1
    matches = db.execute("SELECT profile_id, breakdown_json->'meta' AS meta FROM opportunity_matches "
                         "WHERE opportunity_id = %s", (opportunity,)).fetchall()
    assert [str(m["profile_id"]) for m in matches] == [DIN]  # The requesting company only.
    meta = matches[0]["meta"]
    assert (meta["execution"], meta["evaluation_origin"], meta["manual_request_id"]) == ("manual", evaluation.MANUAL_ORIGIN, 77)
    assert meta["source_key"] == "seace_prod2"
    used = db.execute(evaluation.TODAY_SQL, (evaluation.POLICY_VERSION, DIN, evaluation.MANUAL_ORIGIN)).fetchone()["total"]
    assert used == 0  # The manual evaluation is not the automatic budget.
    again = evaluation.evaluate_manual(db, settings(), 4405, DIN, request_id=78, analyzer=analyzer)
    assert again == {"outcome": "skipped", "reason": "match_current"} and len(analyzer.calls) == 1  # Never paid twice.
    planned = evaluation.plan(db, convocations=[4405], max_calls=10)
    assert [r["profile_id"] for r in planned["planned"]] == [VEYON]  # Automatic rules still own the other company.
    assert planned["skipped"] == {"match_current": 1}


@needs_db
def test_a_manual_pair_for_a_closed_convocatoria_or_without_a_reading_never_reaches_the_provider(db):
    seed_profiles(db)
    opportunity = seed_convocatoria(db, 4406, closes_in_hours=-1)
    analyzer = Analyzer()
    assert evaluation.evaluate_manual(db, settings(), 4406, VEYON, request_id=1, analyzer=analyzer)["reason"] == "no_current_reading"
    seed_reading(db, 4406, opportunity)
    assert evaluation.evaluate_manual(db, settings(), 4406, VEYON, request_id=1, analyzer=analyzer)["reason"] == "closed"
    assert analyzer.calls == []
    assert db.execute("SELECT count(*) AS n FROM prod2_evaluations WHERE id_convocatoria = 4406 AND kind = 'gemini_match'"
                      ).fetchone()["n"] == 0



def ficha(convocation, *, registration, submission):
    return {"procedure_id": convocation + 1, "convocatoria_id": convocation, "nomenclature": "AS-1", "entity": "Entidad",
            "object": "Servicio", "description": "Licencias", "selection_type": "Licitación Pública",
            "schedule": [{"stage": "Registro de participantes", "starts_at_text": registration[0], "ends_at_text": registration[1]},
                         {"stage": "Presentación de ofertas", "starts_at_text": submission[0], "ends_at_text": submission[1]}],
            "documents": [], "document_inventory_total": 0, "document_inventory_complete": True, "item_states": ["Convocado"]}


def lima(days):
    from zoneinfo import ZoneInfo
    return (datetime.now(timezone.utc) + timedelta(days=days)).astimezone(ZoneInfo("America/Lima")).strftime("%d/%m/%Y %H:%M")


def run_real_enrich(db, tmp_path, convocation, detail):
    """The selected runner's own ``enrich`` in handoff mode, with the manifest the service would freeze."""
    from buenapro_worker.prod2 import service
    from buenapro_worker.prod2.selected_control import RunLimits
    t = make(tmp_path, service_config=service_config(tmp_path), project_root=ROOT, load_config=service._config)
    selected = service._selected(ROOT)
    store = mg.Requests(db)
    item = store.item(convocation)
    manifest = json.loads(json.dumps(mg.build_manifest(t.rt, item, 7)))
    folder = tmp_path / "case" / str(convocation)
    folder.mkdir(parents=True)
    selected.STOP.clear()
    guard = HandoffGuards(tmp_path / "case", stop=selected.STOP, max_candidates=1, max_new_documents=mg.MAX_NEW_DOCUMENTS,
                          binding={"version": mg.VERSION, "request_id": 7, "convocation_id": convocation})
    limits = RunLimits(max_accepted=1, max_candidates=1, max_jev_calls=0, max_llm1_calls=0, model_workers=1,
                       max_embedding_calls=0)  # Zero ceilings: any paid call would raise.
    limits.handoff_guard = guard
    guard.admit_candidate(convocation)
    capture = {"observed_at": datetime.now(timezone.utc).isoformat(), "detail": detail}
    return selected.enrich(db, SimpleNamespace(gemini_model="gemini-test"), folder, item, manifest,
                           selected.SharedPacer(manifest["request_interval"]), limits, "key", capture=capture,
                           defer_reading=False)


@needs_db
def test_the_selected_runner_exposes_what_the_reader_uses():
    import inspect
    from buenapro_worker.prod2 import service
    selected = service._selected(ROOT)
    for name in ("STOP", "EGRESS_DOWN", "EMBEDDING_DISPATCH_STOP", "SharedPacer", "enrich", "cleanup_finished_case",
                 "egress_failure", "stop_after_uncertain_embedding_error", "source_hooks"):
        assert hasattr(selected, name), name
    assert list(inspect.signature(selected.enrich).parameters) == [
        "conn", "settings", "folder", "item", "manifest", "pacer", "limits", "jev_key", "capture", "defer_reading"]


@needs_db
def test_a_closed_ficha_returns_the_gate_record_before_any_paid_or_document_call(db, tmp_path):
    seed_profiles(db)
    seed_convocatoria(db, 4410, closes_in_hours=None)
    record = run_real_enrich(db, tmp_path, 4410, ficha(
        4410, registration=("01/10/2024 08:00", "10/10/2024 18:00"), submission=("05/10/2024 08:00", "15/10/2024 18:00")))
    assert record["status"] == "closed_without_reading" and record["new_document_requests"] == 0
    with pytest.raises(mg.Fail) as failure:
        mg.settle_reading({"convocation_id": 4410, **record})
    assert failure.value.reason == "convocatoria_cerrada"
    # The fresh ficha was saved (the web shows it closed) and nothing was reserved, downloaded or paid.
    saved = db.execute("SELECT proposals_closes_at FROM prod2_details WHERE id_convocatoria = 4410").fetchone()
    assert saved["proposals_closes_at"].year == 2024
    assert db.execute("SELECT count(*) AS n FROM prod2_evaluations WHERE id_convocatoria = 4410 AND kind <> 'jev_affinity'"
                      ).fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM prod2_document_downloads WHERE id_convocatoria = 4410").fetchone()["n"] == 0
    assert not list((tmp_path / "case").rglob("*.download.private.json"))


@needs_db
def test_a_ficha_not_confirmed_open_is_also_stopped_by_the_gate(db, tmp_path):
    seed_profiles(db)
    seed_convocatoria(db, 4411, closes_in_hours=None)
    record = run_real_enrich(db, tmp_path, 4411, ficha(
        4411, registration=(lima(5), lima(8)), submission=(lima(6), lima(9))))  # Not started yet.
    assert record["status"] == "opening_not_confirmed"
    with pytest.raises(mg.Fail) as failure:
        mg.settle_reading({"convocation_id": 4411, **record})
    assert failure.value.reason == "convocatoria_cerrada"


@needs_db
def test_an_open_ficha_without_documents_ends_in_review_before_any_paid_call(db, tmp_path):
    seed_profiles(db)
    seed_convocatoria(db, 4412, closes_in_hours=None)
    record = run_real_enrich(db, tmp_path, 4412, ficha(
        4412, registration=(lima(-5), lima(1)), submission=(lima(-2), lima(2))))
    assert record["status"] == "documents_missing" and record["confirmed_open"] is True
    with pytest.raises(mg.Fail) as failure:
        mg.settle_reading({"convocation_id": 4412, **record})
    assert failure.value.reason == "reading_requires_review"
    assert db.execute("SELECT count(*) AS n FROM prod2_evaluations WHERE id_convocatoria = 4412 AND kind <> 'jev_affinity'"
                      ).fetchone()["n"] == 0


@needs_db
def test_a_whole_read_of_a_closed_convocatoria_freezes_the_case_and_never_repeats(db, tmp_path):
    from buenapro_worker.prod2 import service
    seed_profiles(db)
    seed_convocatoria(db, 4413, closes_in_hours=None)
    fetched = []

    def fresh(rt, selected, item, folder, pacer):
        fetched.append(item["row"]["convocation_id"])
        return {"observed_at": datetime.now(timezone.utc).isoformat(), "detail": ficha(
            4413, registration=("01/10/2024 08:00", "10/10/2024 18:00"), submission=("05/10/2024 08:00", "15/10/2024 18:00"))}
    t = make(tmp_path, service_config=service_config(tmp_path), project_root=ROOT, load_config=service._config,
             fresh_ficha=fresh)
    store, case = mg.Requests(db), t.state / "cases" / "7"
    for _ in range(2):
        with pytest.raises(mg.Fail) as failure:
            mg.read_convocatoria(t.rt, store, None, request(7, source_id=4413), 4413)
        assert failure.value.reason == "convocatoria_cerrada"
    assert fetched == [4413]  # The settled outcome is final: no second visit to SEACE, no second chance to pay.
    outcome = json.loads((case / "4413" / "outcome.private.json").read_text())
    assert outcome["status"] == "closed_without_reading" and outcome["accepted"] is True and outcome["manual_request_id"] == 7
    frozen = json.loads((case / "manifest.private.json").read_text())
    assert frozen["run_id"] == "prod2-manual-7" and frozen["embedding_reading"]["config_sha256"]
    assert (case / "handoff-guards" / "candidates").is_dir() and not (case / "handoff-guards" / "dispatch-stop.private.json").exists()


# The two tests below need a second session, so they commit and clean up after themselves.

@pytest.fixture
def sessions():
    opened = []

    def connect():
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
        opened.append(conn)
        return conn
    yield connect
    for conn in opened:
        conn.close()


@needs_db
def test_two_consumers_never_claim_the_same_request(sessions):
    one, two = sessions(), sessions()
    tenant = seed_profiles(one)
    ids = [seed_request(one, tenant, convocation=900000 + n, age=timedelta(minutes=10 - n)) for n in range(3)]
    try:
        with one.transaction():
            held = one.execute(mg.CLAIM_SQL).fetchone()  # The first consumer is mid-claim.
            taken = mg.Requests(two).claim()
            assert held["id"] == ids[0] and taken["id"] == ids[1]  # SKIP LOCKED: the second takes the next one.
        assert mg.Requests(two).claim()["id"] == ids[0]  # The first never moved its row: it is free again.
        assert mg.Requests(two).claim()["id"] == ids[2] and mg.Requests(two).claim() is None
    finally:
        one.execute("DELETE FROM manual_grading_requests WHERE tenant_id = %s", (tenant,))
        one.execute("DELETE FROM tenants WHERE id = %s", (tenant,))


@needs_db
def test_the_executor_lock_is_exclusive_and_visible_to_its_owner(sessions):
    one, two = sessions(), sessions()
    assert one.execute(mg.LOCK_SQL).fetchone()["locked"] is True
    try:
        assert two.execute(mg.LOCK_SQL).fetchone()["locked"] is False  # Never waits: the pass is skipped.
        assert mg.holds_executor_lock(one) is True and mg.holds_executor_lock(two) is False
    finally:
        one.execute(mg.UNLOCK_SQL)
    assert two.execute(mg.LOCK_SQL).fetchone()["locked"] is True
    two.execute(mg.UNLOCK_SQL)


def purge(conn, tenant, *convocations):
    conn.execute("DELETE FROM manual_grading_requests WHERE tenant_id = %s", (tenant,))
    for convocation in convocations:
        found = conn.execute("SELECT opportunity_id FROM prod2_discoveries WHERE id_convocatoria = %s", (convocation,)).fetchone()
        conn.execute("DELETE FROM prod2_accepted_run_members WHERE convocation_id = %s", (convocation,))
        conn.execute("DELETE FROM prod2_jev_decision_cache WHERE convocation_id = %s", (convocation,))
        conn.execute("DELETE FROM prod2_discoveries WHERE id_convocatoria = %s", (convocation,))
        if found:
            conn.execute("DELETE FROM opportunities WHERE id = %s", (found["opportunity_id"],))
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))


@needs_db
def test_a_whole_pass_on_the_real_database_claims_reads_evaluates_and_releases_the_lock(sessions, tmp_path):
    seed = sessions()
    tenant = seed_profiles(seed)
    opportunity = seed_convocatoria(seed, 4500)
    request_id = seed_request(seed, tenant, convocation=4500)
    try:
        calls = []

        def read(rt, store, owner, req, convocation):
            calls.append(("read", seed.execute("SELECT status FROM manual_grading_requests WHERE id = %s",
                                               (req["id"],)).fetchone()["status"]))
            seed_reading(seed, convocation, opportunity)

        def evaluate(conn, settings_, convocation, profile, **kwargs):
            calls.append(("evaluate", conn.execute("SELECT status FROM manual_grading_requests WHERE id = %s",
                                                   (kwargs["request_id"],)).fetchone()["status"]))
            return {"outcome": "evaluated"}
        t = make(tmp_path, service=None, requests=mg.Requests, connect=lambda: mg.database(DATABASE_URL), read=read,
                 evaluate=evaluate, now=lambda: datetime.now(timezone.utc))
        (t.service_dir / "state.private.json").write_text(json.dumps(
            {"status": "waiting", "next_cycle_at": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()}))
        summary = mg.run_pass(t.rt)
        assert summary["handled"] == {"done": 1} and "skipped" not in summary
        assert calls == [("read", "reading"), ("evaluate", "evaluating")]  # Each stage ran under its own status.
        final = seed.execute("SELECT status, reason FROM manual_grading_requests WHERE id = %s", (request_id,)).fetchone()
        assert (final["status"], final["reason"]) == ("done", None)
        assert seed.execute(mg.LOCK_SQL).fetchone()["locked"] is True  # The pass handed the executor lock back.
        seed.execute(mg.UNLOCK_SQL)
        assert mg.run_pass(t.rt)["skipped"] == "idle"
    finally:
        purge(seed, tenant, 4500)


@needs_db
def test_a_pass_never_starts_while_another_coordinator_owns_the_executor(sessions, tmp_path):
    seed, coordinator = sessions(), sessions()
    tenant = seed_profiles(seed)
    seed_convocatoria(seed, 4501)
    request_id = seed_request(seed, tenant, convocation=4501)
    assert coordinator.execute(mg.LOCK_SQL).fetchone()["locked"] is True  # The daily cycle's executor.
    try:
        t = make(tmp_path, service=None, requests=mg.Requests, connect=lambda: mg.database(DATABASE_URL),
                 now=lambda: datetime.now(timezone.utc))
        assert mg.run_pass(t.rt)["skipped"] == "executor_lock_busy"
        status = seed.execute("SELECT status FROM manual_grading_requests WHERE id = %s", (request_id,)).fetchone()["status"]
        assert status == "pending" and t.calls.read == [] and t.calls.evaluate == []
    finally:
        coordinator.execute(mg.UNLOCK_SQL)
        purge(seed, tenant, 4501)
