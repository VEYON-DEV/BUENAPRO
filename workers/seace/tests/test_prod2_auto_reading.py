"""Automatic reading of PROD2 mayores by ``prod2-manual`` (``manual_grading``): the queue ``prod2_reading_queue``.

The same passes that serve the «Calificar» requests also serve the queue the discovery fills, but only inside
``--auto-reading-window`` (Lima time) and after the manual requests. Offline: the queue, the requests table, the
reader and the evaluator are fakes (``Queue`` and ``AutoStore`` are twins of ``ReadingQueue`` and ``Requests``) and
the paid chain never runs. The PostgreSQL tests run only with BUENAPRO_TEST_DATABASE_URL (a database with
db/migrations applied); they roll back, except the ones that need a second session or a whole pass, which commit
and clean up after themselves (like the manual grading ones, they assume no other item waits at the head of the queue).
"""
from datetime import datetime, time as day_time, timedelta, timezone
import json
import sys
from types import SimpleNamespace

import psycopg
import pytest

from buenapro_worker.prod2 import manual_grading as mg
from buenapro_worker.prod2.handoff_guards import HandoffGuards
from test_prod2_manual_grading import (  # noqa: F401  (fakes, seeds and fixtures shared with the manual grading tests)
    CONVOCATION, DATABASE_URL, ROOT, Selected, Store, db, make, needs_db, purge, request, seed_convocatoria,
    seed_profiles, seed_reading, service_config, sessions, waiting)

AUTO_NOW = datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)  # 03:00 in Lima, inside 01:00-07:00
WINDOW = (day_time(1, 0), day_time(7, 0))
FAR_PAST = datetime(2001, 1, 1, tzinfo=timezone.utc)


def lima(hour, minute=0, day=31):
    """The UTC instant of a Lima wall-clock time of 2026-10 (Lima is UTC-5 all year)."""
    return datetime(2026, 10, day, tzinfo=timezone.utc) + timedelta(hours=hour + 5, minutes=minute)


# --- fakes -------------------------------------------------------------------------------------------------------

class Queue:
    """In-memory twin of ``mg.ReadingQueue``: the same contract, plus a log of every transition."""

    def __init__(self, *items):
        self.rows = {item["id_convocatoria"]: {"affinity_index": 90.0, "status": "pending", "attempts": 0,
                                               "reason": None, **item} for item in items}
        self.log, self.claims, self.recovered = [], 0, 0
        self.conn = SimpleNamespace(name="work")

    def recover(self):
        return self.recovered

    def skip_closed(self):
        return 0

    def has_work(self):
        return any(row["status"] == "pending" for row in self.rows.values())

    def claim(self, skip=()):
        self.claims += 1
        pending = sorted((row for row in self.rows.values()
                          if row["status"] == "pending" and row["id_convocatoria"] not in skip),
                         key=lambda row: row["id_convocatoria"])
        if not pending:
            return None
        row = pending[0]
        row.update(status="reading", attempts=row["attempts"] + 1)
        self.log.append((row["id_convocatoria"], "pending", "reading", None))
        return {**row}

    def _held(self, item):
        stored = self.rows[item["id_convocatoria"]]
        return stored if stored["status"] == "reading" and stored["attempts"] == item["attempts"] else None

    def settle(self, item, status, reason=None):
        stored = self._held(item)
        if stored is None:
            return False
        self.log.append((item["id_convocatoria"], "reading", status, reason))
        stored.update(status=status, reason=reason)
        return True

    def release(self, item, reason=None, *, refund=False):
        stored = self._held(item)
        if stored is None:
            return False
        self.log.append((item["id_convocatoria"], "reading", "pending", reason))
        stored.update(status="pending", reason=reason or stored["reason"], attempts=max(stored["attempts"] - int(refund), 0))
        return True

    def counts(self):
        return {status: sum(row["status"] == status for row in self.rows.values()) for status in mg.QUEUE_STATUSES}

    def state(self, convocation):
        row = self.rows[convocation]
        return row["status"], row["reason"], row["attempts"]


class AutoStore(Store):
    """The requests fake with a reading and a ficha per convocatoria (``Store`` has a single global one)."""

    def __init__(self, *requests, read=()):
        super().__init__(*requests)
        self.read_ids, self.facts_by = set(read), {}

    def facts(self, convocation, profile):
        return {**self.facts_row, **self.facts_by.get(convocation, {})}

    def reading(self, convocation):
        return convocation in self.read_ids, self.statuses


def automatic(tmp_path, *items, manual=(), store=None, **fields):
    """A runtime inside its window (03:00 Lima, the service far from its next cycle) wired to the queue fake."""
    store = store or AutoStore(*manual)
    queue, events = Queue(*items), []

    def read_auto(rt, store_, owner, case, convocation):
        events.append(("read", convocation))
        t.calls.auto_read.append((convocation, case))
        store.read_ids.add(convocation)

    def auto_evaluate(conn, settings_, receipts, **kwargs):
        events.append(("evaluate", kwargs["convocations"][0]))
        t.calls.auto_evaluate.append({"receipts": receipts, **kwargs})
        return {"planned": 2, "outcomes": [{"outcome": "evaluated"}, {"outcome": "evaluated"}], "stopped": None,
                "cost_usd": 0.02}

    def read(rt, store_, owner, req, convocation):
        events.append(("manual_read", req["id"]))
        t.calls.read.append(req["id"])
        store.read_ids.add(convocation)
    values = dict(queue=lambda conn: queue, auto_window=WINDOW, read_auto=read_auto, auto_evaluate=auto_evaluate,
                  read=read)
    t = make(tmp_path, store, service={"status": "waiting", "next_cycle_at": (AUTO_NOW + timedelta(hours=22)).isoformat()},
             **{**values, **fields})
    t.clock.current = AUTO_NOW
    t.queue, t.events = queue, events
    t.calls.auto_read, t.calls.auto_evaluate = [], []
    return t


def item(convocation, **fields):
    return {"id_convocatoria": convocation, **fields}


def folder(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def forbidden(*args, **kwargs):
    raise AssertionError("the automatic queue must not be touched here")


# --- the window ----------------------------------------------------------------------------------------------------

def test_the_window_is_parsed_as_lima_time_and_may_cross_midnight():
    assert mg.parse_window("01:00-07:00") == (day_time(1, 0), day_time(7, 0))
    assert mg.parse_window(" 22:30-06:15 ") == (day_time(22, 30), day_time(6, 15))
    assert mg.parse_window("00:00-23:59") == (day_time(0, 0), day_time(23, 59))


@pytest.mark.parametrize("text", ["", "1:00-7:00", "01:00", "01:00-07", "24:00-07:00", "01:60-07:00", "01:00-07:60",
                                  "01:00-25:00", "01:00-01:00", "01:00 - 07:00", "01:00–07:00", "ab:cd-ef:gh"])
def test_an_invalid_or_empty_window_is_refused(text):
    with pytest.raises(ValueError, match="invalid_auto_window"):
        mg.parse_window(text)


@pytest.mark.parametrize("hour,minute,expected", [(0, 59, False), (1, 0, True), (3, 30, True), (6, 59, True),
                                                  (7, 0, False), (7, 1, False), (12, 0, False), (23, 59, False)])
def test_the_window_includes_its_start_and_excludes_its_end(hour, minute, expected):
    assert mg.in_window(WINDOW, lima(hour, minute)) is expected


def test_the_window_is_read_in_lima_not_in_utc():
    # 01:00 UTC is 20:00 in Lima the day before: closed. 06:00 UTC is 01:00 in Lima: open.
    assert mg.in_window(WINDOW, datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)) is False
    assert mg.in_window(WINDOW, datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)) is True
    assert mg.in_window(WINDOW, datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)) is False  # 07:00 in Lima
    # The same instant in another zone is the same moment.
    assert mg.in_window(WINDOW, lima(3).astimezone(timezone(timedelta(hours=9)))) is True


@pytest.mark.parametrize("hour,minute,expected", [(21, 59, False), (22, 0, True), (23, 59, True), (0, 0, True),
                                                  (3, 0, True), (5, 59, True), (6, 0, False), (12, 0, False)])
def test_a_window_may_cross_midnight(hour, minute, expected):
    night = (day_time(22, 0), day_time(6, 0))
    assert mg.in_window(night, lima(hour, minute)) is expected


def test_without_a_window_it_is_never_open():
    assert mg.in_window(None, lima(3)) is False


# --- off by default ------------------------------------------------------------------------------------------------

def test_the_queue_is_off_unless_a_window_is_configured(tmp_path):
    rt = mg.Runtime(state_dir=tmp_path, service_state_dir=tmp_path, service_config=tmp_path / "c", project_root=ROOT,
                    settings=None, connect=None)
    assert rt.auto_window is None and rt.max_auto == 3 and rt.auto_evaluation_max_calls == 20
    assert rt.auto_open(lima(3)) is False


def test_without_a_window_a_pass_never_touches_the_queue_and_reports_what_it_always_did(tmp_path):
    t = automatic(tmp_path, item(1), manual=[request(1, source_id=7)], auto_window=None, queue=forbidden)
    summary = mg.run_pass(t.rt)
    assert summary["handled"] == {"done": 1} and "auto" not in summary and "queue" not in summary
    assert t.events == [("manual_read", 1)]
    assert mg.run_pass(t.rt)["skipped"] == "idle"  # A pending queue item alone is not work for a daemon without a window.


def test_outside_the_window_the_queue_is_not_even_looked_at(tmp_path):
    t = automatic(tmp_path, item(1), queue=forbidden)
    t.clock.current = lima(13)
    assert mg.run_pass(t.rt) == {"event": "pass", "at": lima(13).isoformat(timespec="seconds"), "expired": 0,
                                 "skipped": "idle"}
    assert t.events == [] and t.link.held is False


def test_outside_the_window_manual_requests_are_served_as_ever(tmp_path):
    t = automatic(tmp_path, item(1), manual=[request(1, source_id=7)], queue=forbidden)
    t.clock.current = lima(13)
    (t.service_dir / "state.private.json").write_text(json.dumps(
        {"status": "waiting", "next_cycle_at": (lima(13) + timedelta(hours=8)).isoformat()}))
    assert mg.run_pass(t.rt)["handled"] == {"done": 1}


# --- when the queue is served ------------------------------------------------------------------------------------------

def test_in_the_window_a_pass_with_only_queue_work_runs_it(tmp_path):
    t = automatic(tmp_path, item(1), item(2))
    summary = mg.run_pass(t.rt)
    assert "skipped" not in summary and summary["handled"] == {}
    assert summary["auto"]["handled"] == {"done": 2}
    assert summary["queue"] == {"pending": 0, "reading": 0, "done": 2, "failed": 0, "skipped": 0}
    assert t.queue.state(1) == ("done", None, 1) and t.queue.state(2) == ("done", None, 1)
    assert t.events == [("read", 1), ("evaluate", 1), ("read", 2), ("evaluate", 2)]
    assert t.link.unlocked == 1 and t.link.held is False


def test_an_idle_queue_is_an_idle_pass_with_its_counters(tmp_path):
    t = automatic(tmp_path, item(1, status="done"), item(2, status="failed"))
    summary = mg.run_pass(t.rt)
    assert summary["skipped"] == "idle" and summary["queue"]["done"] == 1 and summary["queue"]["failed"] == 1
    assert t.queue.claims == 0 and t.link.held is False


def test_the_pass_reports_what_the_queue_recovered(tmp_path):
    t = automatic(tmp_path, item(1, status="done"))
    t.queue.recovered = 4
    assert mg.run_pass(t.rt)["auto"]["recovered"] == 4


def test_manual_requests_go_first_and_the_queue_gets_what_is_left(tmp_path):
    t = automatic(tmp_path, item(10), item(11), manual=[request(1, source_id=7), request(2, source_id=8)])
    summary = mg.run_pass(t.rt)
    assert summary["handled"] == {"done": 2} and summary["auto"]["handled"] == {"done": 2}
    assert [e for e in t.events if e[0] != "evaluate"] == [("manual_read", 1), ("manual_read", 2), ("read", 10), ("read", 11)]
    assert t.queue.claims == 3  # Two items and the claim that found the queue empty.


def test_a_full_manual_pass_leaves_the_queue_for_the_next_pass(tmp_path):
    t = automatic(tmp_path, item(10), manual=[request(n, source_id=n) for n in range(1, 4)], max_requests=2)
    summary = mg.run_pass(t.rt)
    assert summary["handled"] == {"done": 2} and "handled" not in summary["auto"] and t.queue.claims == 0
    summary = mg.run_pass(t.rt)  # The last manual request, then the queue.
    assert summary["handled"] == {"done": 1} and summary["auto"]["handled"] == {"done": 1}


def test_a_manual_stop_means_no_automatic_reading(tmp_path):
    t = automatic(tmp_path, item(10), manual=[request(1, source_id=7)], evaluate=lambda *a, **k: {"outcome": "provider_rejected"})
    summary = mg.run_pass(t.rt)
    assert summary["stopped"] == "provider_rejected" and t.queue.claims == 0 and "handled" not in summary["auto"]


@pytest.mark.parametrize("state,reason", [
    ({"status": "running"}, "service_running"), ({"status": "retry_wait"}, "service_retry_wait"),
    ({"status": "waiting", "next_cycle_at": (AUTO_NOW + timedelta(minutes=59)).isoformat()}, "cycle_imminent"),
    ({"status": "waiting", "next_cycle_at": "tomorrow"}, "service_state_unreadable")])
def test_the_service_gate_is_the_manual_one(tmp_path, state, reason):
    t = automatic(tmp_path, item(1))
    (t.service_dir / "state.private.json").write_text(json.dumps(state))
    assert mg.run_pass(t.rt)["skipped"] == reason
    assert t.queue.claims == 0 and t.link.held is False and t.events == []


def test_a_blocked_source_a_failed_egress_and_a_busy_executor_stop_the_queue_too(tmp_path):
    t = automatic(tmp_path, item(1), shared_block=lambda: {"status": 403, "source": "prod6", "remaining_seconds": 900})
    assert mg.run_pass(t.rt)["skipped"] == "source_blocked"

    def down():
        raise mg.EgressUnavailable("proxy unreachable")
    t = automatic(folder(tmp_path, "egress"), item(1), check_egress=down)
    assert mg.run_pass(t.rt)["skipped"] == "egress_unavailable"
    t = automatic(folder(tmp_path, "lock"), item(1))
    t.link.busy = True
    assert mg.run_pass(t.rt)["skipped"] == "executor_lock_busy"
    assert t.queue.claims == 0 and t.link.unlocked == 0


@pytest.mark.parametrize("path", [mg.GUARD, "evaluations/guard.json", "auto-evaluations/guard.json"])
def test_any_open_guard_stops_the_queue(tmp_path, path):
    t = automatic(tmp_path, item(1))
    (t.state / path).parent.mkdir(exist_ok=True)
    (t.state / path).write_text("{}")
    assert mg.run_pass(t.rt)["skipped"] == "guard_open" and t.queue.claims == 0


def test_a_provider_pause_stops_the_queue(tmp_path):
    t = automatic(tmp_path, item(1))
    t.rt.cooldown_until = AUTO_NOW + timedelta(minutes=10)
    assert mg.run_pass(t.rt)["skipped"] == "cooldown" and t.queue.claims == 0


def test_at_most_max_auto_readings_start_per_pass(tmp_path):
    t = automatic(tmp_path, *(item(n) for n in range(1, 6)))
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"done": 3} and summary["queue"]["pending"] == 2
    assert mg.run_pass(t.rt)["auto"]["handled"] == {"done": 2}
    t = automatic(folder(tmp_path, "one"), *(item(n) for n in range(1, 4)), max_auto=1)
    assert mg.run_pass(t.rt)["auto"]["handled"] == {"done": 1}


def test_items_that_cost_no_reading_do_not_use_the_budget_of_the_pass(tmp_path):
    store = AutoStore(read=[1])
    store.facts_by[2] = {"proposals_closes_at": AUTO_NOW - timedelta(hours=1)}
    t = automatic(tmp_path, *(item(n) for n in range(1, 8)), store=store, max_auto=3)
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"skipped:already_read": 1, "skipped:convocatoria_cerrada": 1, "done": 3}
    assert [e for e in t.events if e[0] == "read"] == [("read", 3), ("read", 4), ("read", 5)]
    assert summary["queue"]["pending"] == 2 and summary["queue"]["skipped"] == 2


def test_the_queue_stops_when_the_cycle_gets_close(tmp_path):
    t = automatic(tmp_path, *(item(n) for n in range(1, 6)), max_auto=5)
    (t.service_dir / "state.private.json").write_text(json.dumps(
        {"status": "waiting", "next_cycle_at": (AUTO_NOW + timedelta(hours=2)).isoformat()}))  # Claims stop in 1 hour.
    original = t.rt.read_auto

    def slow(rt, store, owner, case, convocation):
        t.clock.advance(minutes=45)
        original(rt, store, owner, case, convocation)
    t.rt.read_auto = slow
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"done": 2} and summary["stopped"] == "cycle_imminent"
    assert t.queue.state(3)[0] == "pending"


def test_the_queue_stops_claiming_when_the_window_closes_but_finishes_the_read_in_hand(tmp_path):
    t = automatic(tmp_path, *(item(n) for n in range(1, 4)), max_auto=3)
    t.clock.current = lima(6, 50)
    (t.service_dir / "state.private.json").write_text(json.dumps(
        {"status": "waiting", "next_cycle_at": (lima(6, 50) + timedelta(hours=22)).isoformat()}))
    original = t.rt.read_auto

    def slow(rt, store, owner, case, convocation):
        t.clock.advance(minutes=15)  # The read runs past 07:00.
        original(rt, store, owner, case, convocation)
    t.rt.read_auto = slow
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"done": 1} and summary["stopped"] == "auto_window_closed"
    assert t.queue.state(1)[0] == "done" and t.queue.state(2)[0] == "pending"


def test_a_shutdown_ends_the_queue_between_items(tmp_path):
    t = automatic(tmp_path, item(1), item(2))
    original = t.rt.read_auto

    def then_stop(rt, store, owner, case, convocation):
        original(rt, store, owner, case, convocation)
        rt.stop.set()
    t.rt.read_auto = then_stop
    summary = mg.run_pass(t.rt)
    assert summary["stopped"] == "shutdown" and t.queue.state(2)[0] == "pending"


# --- what each outcome does to the item ---------------------------------------------------------------------------------

def test_an_item_already_read_is_skipped_without_a_reading_or_an_evaluation(tmp_path):
    t = automatic(tmp_path, item(1), store=AutoStore(read=[1]))
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"skipped:already_read": 1}
    assert t.queue.state(1) == ("skipped", "already_read", 1) and t.events == []


def test_an_item_whose_convocatoria_closed_is_skipped_for_free(tmp_path):
    store = AutoStore()
    store.facts_by[1] = {"proposals_closes_at": AUTO_NOW}
    t = automatic(tmp_path, item(1), store=store)
    assert mg.run_pass(t.rt)["auto"]["handled"] == {"skipped:convocatoria_cerrada": 1}
    assert t.queue.state(1) == ("skipped", "convocatoria_cerrada", 1) and t.events == []


@pytest.mark.parametrize("status", ["closed_without_reading", "opening_not_confirmed"])
def test_a_ficha_found_closed_or_not_confirmed_open_is_skipped_not_failed(tmp_path, status):
    t = automatic(tmp_path, item(1), read_auto=lambda *a: mg.settle_reading({"status": status}))
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"skipped:convocatoria_cerrada": 1}
    assert t.queue.state(1) == ("skipped", "convocatoria_cerrada", 1)
    assert t.calls.auto_evaluate == []  # Nothing read, nothing evaluated.
    assert summary["auto"]["evaluation"] == {"planned": 0, "evaluated": 0, "cost_usd": 0.0}


def test_a_deferred_read_goes_back_to_pending_gives_the_attempt_back_and_ends_the_pass(tmp_path):
    t = automatic(tmp_path, item(1), item(2), read_auto=lambda *a: (_ for _ in ()).throw(mg.Defer("source_blocked")))
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"deferred": 1} and summary["stopped"] == "source_blocked"
    assert t.queue.state(1) == ("pending", None, 0) and t.queue.state(2) == ("pending", None, 0)
    assert t.calls.auto_evaluate == []


def test_a_deferred_read_does_not_use_up_the_attempts(tmp_path):
    t = automatic(tmp_path, item(1, attempts=1))  # One real failure already.
    t.rt.read_auto = lambda *a: (_ for _ in ()).throw(mg.Defer("cycle_imminent"))
    mg.run_pass(t.rt)
    assert t.queue.state(1) == ("pending", None, 1)  # Claimed as attempt 2, given back.
    t.rt.read_auto = lambda *a: (_ for _ in ()).throw(mg.Fail("reading_failed"))
    mg.run_pass(t.rt)
    assert t.queue.state(1) == ("failed", "reading_failed", 2)


def test_a_failed_first_attempt_waits_for_the_next_pass_and_a_second_one_ends_failed(tmp_path):
    t = automatic(tmp_path, item(1), item(2), read_auto=lambda rt, s, o, case, conv: (_ for _ in ()).throw(
        mg.Fail("reading_requires_review")) if conv == 1 else s.read_ids.add(conv))
    summary = mg.run_pass(t.rt)
    # The failed item is not taken again in the same pass: the next one is.
    assert summary["auto"]["handled"] == {"retry:reading_requires_review": 1, "done": 1}
    assert t.queue.state(1) == ("pending", "reading_requires_review", 1) and t.queue.state(2)[0] == "done"
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"failed:reading_requires_review": 1}
    assert t.queue.state(1) == ("failed", "reading_requires_review", 2)
    assert t.link.unlocked == 2 and mg.run_pass(t.rt)["skipped"] == "idle"


def test_an_unexpected_error_is_a_failed_attempt_not_a_crash_of_the_pass(tmp_path):
    t = automatic(tmp_path, item(1), item(2), read_auto=lambda rt, s, o, case, conv: (_ for _ in ()).throw(
        ValueError("boom")) if conv == 1 else s.read_ids.add(conv))
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"retry:reading_failed": 1, "done": 1}
    assert any(line.get("event") == "queue_error" and line["error_type"] == "ValueError"
               and line["convocation_id"] == 1 for line in t.lines)


def test_a_database_outage_is_not_a_failed_attempt(tmp_path):
    t = automatic(tmp_path, item(1), read_auto=lambda *a: (_ for _ in ()).throw(psycopg.OperationalError("gone")))
    with pytest.raises(psycopg.OperationalError):
        mg.run_pass(t.rt)
    assert t.queue.state(1) == ("reading", None, 1) and t.link.unlocked == 1  # A later pass recovers it after 6 h.


@pytest.mark.parametrize("facts,reason", [({"found": False}, "contract_not_found"), ({"affinity": 59.9}, "jev_below_60"),
                                          ({"affinity": None}, "jev_below_60")])
def test_a_free_check_that_fails_never_reads_and_ends_failed_after_two_attempts(tmp_path, facts, reason):
    store = AutoStore()
    store.facts_by[1] = facts
    t = automatic(tmp_path, item(1, attempts=1), store=store)
    mg.run_pass(t.rt)
    assert t.queue.state(1) == ("failed", reason, 2) and t.events == []


def test_earlier_unresolved_paid_attempts_without_a_reading_are_not_paid_again(tmp_path):
    store = AutoStore()
    store.statuses = ["uncertain"]
    t = automatic(tmp_path, item(1, attempts=1), store=store)
    mg.run_pass(t.rt)
    assert t.queue.state(1) == ("failed", "payment_uncertain", 2) and t.events == []
    assert not (t.state / mg.GUARD).exists()  # An earlier cycle's doubt fails the item only, like a manual request.


def test_a_payment_doubt_fails_the_item_opens_the_guard_and_stops_all_sending(tmp_path):
    t = automatic(tmp_path, item(1), item(2), read_auto=lambda *a: (_ for _ in ()).throw(mg.Uncertain("usage_unknown")))
    summary = mg.run_pass(t.rt)
    assert summary["stopped"] == "payment_uncertain" and summary["auto"]["handled"] == {"failed:payment_uncertain": 1}
    assert t.queue.state(1) == ("failed", "payment_uncertain", 1) and t.queue.state(2)[0] == "pending"
    guard = json.loads((t.state / mg.GUARD).read_text())
    assert guard["status"] == "open" and guard["reason"] == "usage_unknown" and guard["request_id"] is None
    assert guard["source"] == mg.AUTO_ORIGIN and guard["convocation_id"] == 1
    assert len(t.alerts) == 1 and t.calls.auto_evaluate == []
    for _ in range(3):  # Nothing is sent until an operator removes the file.
        assert mg.run_pass(t.rt)["skipped"] == "guard_open"
    assert t.queue.claims == 1 and len(t.alerts) == 1
    assert mg.loop(t.rt, poll_seconds=0, once=True) == 2


def test_a_manual_guard_incident_is_never_replaced_by_an_automatic_one(tmp_path):
    t = automatic(tmp_path, item(1))
    t.rt.open_guard(7, "first")
    t.rt.open_guard(None, "second", source=mg.AUTO_ORIGIN, convocation_id=1)
    assert json.loads((t.state / mg.GUARD).read_text())["request_id"] == 7


# --- the automatic evaluation after a reading --------------------------------------------------------------------------

def test_a_read_item_is_evaluated_alone_with_its_own_receipts_and_ceiling(tmp_path):
    t = automatic(tmp_path, item(41), item(42), auto_evaluation_max_calls=7)
    summary = mg.run_pass(t.rt)
    first, second = t.calls.auto_evaluate
    assert first["convocations"] == [41] and second["convocations"] == [42]
    assert first["max_calls"] == 7 and first["receipts"] == t.state / "auto-evaluations"
    assert (t.state / "auto-evaluations").is_dir() and t.state / "auto-evaluations" != t.state / "evaluations"
    assert summary["auto"]["evaluation"] == {"planned": 4, "evaluated": 4, "cost_usd": 0.04}


def test_the_item_is_done_before_it_is_evaluated(tmp_path):
    t = automatic(tmp_path, item(1))
    seen = []
    original = t.rt.auto_evaluate

    def spy(conn, settings_, receipts, **kwargs):
        seen.append(t.queue.state(1)[0])
        return original(conn, settings_, receipts, **kwargs)
    t.rt.auto_evaluate = spy
    mg.run_pass(t.rt)
    assert seen == ["done"]


@pytest.mark.parametrize("stopped", ["payment_uncertain", "receipts_blocked"])
def test_a_payment_doubt_in_the_evaluation_opens_the_guard_and_keeps_the_reading_done(tmp_path, stopped):
    t = automatic(tmp_path, item(1), item(2), auto_evaluate=lambda *a, **k: {"planned": 2, "outcomes": [], "stopped": stopped})
    summary = mg.run_pass(t.rt)
    assert summary["stopped"] == "payment_uncertain" and summary["auto"]["handled"] == {"done": 1}
    assert t.queue.state(1)[0] == "done" and t.queue.state(2)[0] == "pending"
    guard = json.loads((t.state / mg.GUARD).read_text())
    assert guard["reason"] == "evaluation_" + stopped and guard["convocation_id"] == 1 and len(t.alerts) == 1
    assert mg.run_pass(t.rt)["skipped"] == "guard_open"


def test_a_provider_rejection_leaves_the_evaluation_to_the_service_without_failing_the_reading(tmp_path):
    t = automatic(tmp_path, item(1), item(2), auto_evaluate=lambda *a, **k: {
        "planned": 2, "outcomes": [{"outcome": "provider_rejected"}], "stopped": "provider_rejected"})
    summary = mg.run_pass(t.rt)
    assert summary["stopped"] == "provider_rejected" and t.queue.state(1) == ("done", None, 1)
    assert t.queue.state(2)[0] == "pending" and not (t.state / mg.GUARD).exists()
    assert mg.run_pass(t.rt)["skipped"] == "cooldown"  # The provider is not asked again for a while.
    t.clock.advance(minutes=16)
    assert mg.run_pass(t.rt)["auto"]["handled"] == {"done": 1}


def test_an_error_of_the_evaluation_never_fails_the_reading_nor_stops_the_queue(tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("model")
    t = automatic(tmp_path, item(1), item(2), auto_evaluate=broken)
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"done": 2} and "stopped" not in summary
    assert t.queue.state(1)[0] == t.queue.state(2)[0] == "done"
    assert [line["event"] for line in t.lines if "evaluation" in line.get("event", "")] == ["auto_evaluation_error"] * 2
    assert len(t.alerts) == 1  # One alert per kind of incident.


def test_a_database_outage_during_the_evaluation_propagates(tmp_path):
    t = automatic(tmp_path, item(1), auto_evaluate=lambda *a, **k: (_ for _ in ()).throw(psycopg.InterfaceError("closed")))
    with pytest.raises(psycopg.InterfaceError):
        mg.run_pass(t.rt)
    assert t.queue.state(1)[0] == "done" and t.link.unlocked == 1


# --- claims that were lost ------------------------------------------------------------------------------------------------

def test_a_claim_that_someone_else_moved_is_lost_and_nothing_is_overwritten(tmp_path):
    t = automatic(tmp_path, item(1))

    def stolen(rt, store, owner, case, convocation):
        t.queue.rows[1].update(status="pending", attempts=0)  # Recovered and about to be taken by another consumer.
        store.read_ids.add(convocation)
    t.rt.read_auto = stolen
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"lost": 1} and t.queue.state(1) == ("pending", None, 0)
    assert t.calls.auto_evaluate == []


# --- the reading machinery shared with the manual requests ---------------------------------------------------------------------

def reading(tmp_path, selected, **fields):
    plans = []

    def plan(rt, item_, ident, run_id=None):
        plans.append((ident, run_id))
        return {"gemini_model": "gemini-test", "request_interval": 2, "run_id": run_id or f"prod2-manual-{ident}"}
    t = automatic(tmp_path, selected=lambda rt: selected, plan_manifest=plan,
                  fresh_ficha=lambda rt, sel, item_, folder, pacer: {"observed_at": AUTO_NOW.isoformat(), "detail": {}},
                  **fields)
    t.plans = plans
    return t


def test_an_automatic_read_has_its_own_folder_run_binding_and_tag(tmp_path):
    selected = Selected()
    t = reading(tmp_path, selected)
    mg.read_case(t.rt, t.store, None, mg.auto_case(t.rt, CONVOCATION), CONVOCATION)
    call, = selected.enriched
    folder = t.state / "auto" / str(CONVOCATION)
    assert call["folder"] == folder / str(CONVOCATION) and call["jev_key"] == "key"
    assert isinstance(call["limits"].handoff_guard, HandoffGuards) and call["defer_reading"] is False
    assert t.plans == [(CONVOCATION, f"prod2-auto-{CONVOCATION}")]
    assert json.loads((folder / "manifest.private.json").read_text())["run_id"] == f"prod2-auto-{CONVOCATION}"
    record, = selected.cleaned
    assert record["queue_origin"] == mg.AUTO_ORIGIN and record["accepted"] is True and "manual_request_id" not in record
    assert json.loads((folder / str(CONVOCATION) / "outcome.private.json").read_text())["status"] == "llm1_complete"
    assert not (t.state / "cases").exists()  # Nothing of the manual requests' folder was touched.


def test_an_automatic_and_a_manual_reading_of_the_same_convocatoria_share_no_files_and_no_doubts(tmp_path):
    selected = Selected()
    t = reading(tmp_path, selected)
    mg.read_case(t.rt, t.store, None, mg.auto_case(t.rt, CONVOCATION), CONVOCATION)
    mg.read_convocatoria(t.rt, t.store, None, request(1), CONVOCATION)
    auto_call, manual_call = selected.enriched
    assert auto_call["folder"] != manual_call["folder"] and manual_call["folder"] == t.state / "cases" / "1" / str(CONVOCATION)
    assert auto_call["limits"].handoff_guard.binding_hash != manual_call["limits"].handoff_guard.binding_hash
    assert t.plans == [(CONVOCATION, f"prod2-auto-{CONVOCATION}"), (1, None)]
    # A paid attempt left without an answer in the automatic folder is a doubt for that item only.
    auto_folder = t.state / "auto" / "77"
    auto_folder.mkdir(parents=True)
    (auto_folder / "gemini-selected-1.started.private.json").write_text(
        json.dumps({"attempt_id": "gemini-selected-1", "model_call_started": True}))
    with pytest.raises(mg.Uncertain):
        mg.read_case(t.rt, t.store, None, mg.auto_case(t.rt, 77), 77)
    mg.read_convocatoria(t.rt, t.store, None, request(2), 77)  # The manual request on it starts clean.


def test_a_resumed_automatic_read_replays_its_frozen_manifest_and_a_settled_one_is_not_repeated(tmp_path):
    blocked = [{"status": 403, "source": "prod2", "remaining_seconds": 60}]
    selected = Selected(lambda **call: (_ for _ in ()).throw(RuntimeError("PROD2 HTTP status 403")) if blocked
                        else {"status": "closed_without_reading"})
    t = reading(tmp_path, selected, shared_block=lambda: blocked[0] if blocked else None)
    case = mg.auto_case(t.rt, CONVOCATION)
    with pytest.raises(mg.Defer):
        mg.read_case(t.rt, t.store, None, case, CONVOCATION)
    blocked.clear()
    for _ in range(2):
        with pytest.raises(mg.Fail) as failure:
            mg.read_case(t.rt, t.store, None, case, CONVOCATION)
        assert failure.value.reason == "convocatoria_cerrada"
    assert len(t.plans) == 1 and len(selected.enriched) == 2  # Frozen once; the settled outcome is final.
    assert selected.enriched[0]["manifest"] == selected.enriched[1]["manifest"]


def test_an_item_whose_own_reading_was_stored_before_the_pass_died_is_not_skipped_as_already_read(tmp_path):
    selected = Selected()
    store = AutoStore()
    t = reading(tmp_path, selected, store=store, read_auto=None)  # read_auto is the real read_case
    outcome_dir = t.state / "auto" / str(CONVOCATION) / str(CONVOCATION)
    outcome_dir.mkdir(parents=True)
    (outcome_dir / "outcome.private.json").write_text(json.dumps({"status": "llm1_complete"}))
    store.read_ids.add(CONVOCATION)  # The extraction is current: it is ours, from the pass that died.
    t.queue.rows[CONVOCATION] = {"id_convocatoria": CONVOCATION, "affinity_index": 90.0, "status": "pending",
                                 "attempts": 0, "reason": None}
    summary = mg.run_pass(t.rt)
    assert summary["auto"]["handled"] == {"done": 1} and selected.enriched == []  # Settled from its outcome, no payment.
    assert t.queue.state(CONVOCATION)[0] == "done"


def test_the_manifest_of_an_automatic_run_is_planned_by_the_service_with_its_own_run_id(tmp_path):
    from buenapro_worker.prod2 import service
    t = automatic(tmp_path, service_config=service_config(tmp_path), project_root=ROOT, load_config=service._config)
    manifest = mg.build_manifest(t.rt, Store().item_row, CONVOCATION, run_id=f"prod2-auto-{CONVOCATION}")
    assert manifest["run_id"] == f"prod2-auto-{CONVOCATION}" and manifest["profile_id"] == mg.PROFILE
    assert mg.build_manifest(t.rt, Store().item_row, 7)["run_id"] == "prod2-manual-7"  # The manual default is unchanged.
    assert not (t.state / "plan").exists()


# --- the queue without migration 0052 ----------------------------------------------------------------------------------------------

def test_a_database_without_the_queue_table_has_nothing_to_serve():
    class Missing:
        def execute(self, *args, **kwargs):
            raise psycopg.errors.UndefinedTable("prod2_reading_queue")
    queue = mg.ReadingQueue(Missing())
    assert queue.recover() == 0 and queue.has_work() is False and queue.claim() is None
    assert queue.counts() == dict.fromkeys(mg.QUEUE_STATUSES, 0)


# --- the entry point and the compose service --------------------------------------------------------------------------------------------

def script():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import grade_prod2_manual
        return grade_prod2_manual
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def entry_base(tmp_path):
    config = tmp_path / "service.json"
    config.write_text("{}")
    return ["--once", "--state-dir", str(tmp_path / "s"), "--service-state-dir", str(tmp_path / "missing"),
            "--service-config", str(config)]


def test_the_script_has_the_automatic_options_off_by_default():
    args = script().parser().parse_args(["--loop", "--state-dir", "s", "--service-state-dir", "d", "--service-config", "c"])
    assert args.auto_reading_window is None and args.max_auto_per_pass == 3 and args.auto_evaluation_max_calls == 20
    args = script().parser().parse_args(["--loop", "--state-dir", "s", "--service-state-dir", "d", "--service-config", "c",
                                         "--auto-reading-window", "01:00-07:00", "--max-auto-per-pass", "2",
                                         "--auto-evaluation-max-calls", "9"])
    assert (args.auto_reading_window, args.max_auto_per_pass, args.auto_evaluation_max_calls) == ("01:00-07:00", 2, 9)


@pytest.mark.parametrize("option", [["--auto-reading-window", "25:00-07:00"], ["--auto-reading-window", "01:00-01:00"],
                                    ["--auto-reading-window", "madrugada"]])
def test_the_script_refuses_an_invalid_window(tmp_path, capsys, option):
    assert script().main([*entry_base(tmp_path), *option]) == 2
    assert "invalid_auto_window" in capsys.readouterr().out


@pytest.mark.parametrize("option", [["--max-auto-per-pass", "0"], ["--max-auto-per-pass", "51"],
                                    ["--auto-evaluation-max-calls", "0"], ["--auto-evaluation-max-calls", "201"]])
def test_the_script_refuses_out_of_range_automatic_limits(tmp_path, capsys, option):
    assert script().main([*entry_base(tmp_path), *option]) == 2
    assert "invalid_bounds" in capsys.readouterr().out


def test_a_valid_window_passes_the_first_checks_of_the_script(tmp_path, capsys):
    assert script().main([*entry_base(tmp_path), "--auto-reading-window", "22:00-06:00"]) == 2
    assert "service_paths_missing" in capsys.readouterr().out  # The missing service directory is what refused it.


def test_the_compose_service_serves_the_queue_overnight_without_touching_the_others():
    text = (ROOT / "infra/docker/prod2.compose.yml").read_text(encoding="utf-8")
    service_block = text[text.index("\n  prod2-manual:"):]
    command = service_block[service_block.index("    command:"):service_block.index("    volumes:")]
    tokens = [line.strip()[2:].strip('"') for line in command.splitlines()[1:] if line.strip()]
    for option, value in (("--auto-reading-window", "01:00-07:00"), ("--max-auto-per-pass", "3"),
                          ("--auto-evaluation-max-calls", "20"), ("--max-requests-per-pass", "5"),
                          ("--safety-minutes", "60")):
        assert tokens[tokens.index(option) + 1] == value
    assert "cpus: 1.5" in service_block[:service_block.index("    command:")] and text.count("--auto-reading-window") == 2  # Comment and command.


# --- PostgreSQL (opt in) ---------------------------------------------------------------------------------------------------

def enqueue(conn, convocation, affinity=90, *, status="pending", enqueued_ago=timedelta(0), claimed_ago=None, attempts=0,
            reason=None):
    conn.execute("""INSERT INTO prod2_reading_queue (id_convocatoria, affinity_index, status, reason, attempts, enqueued_by,
        enqueued_at, updated_at, claimed_at) VALUES (%s, %s, %s, %s, %s, 'test-run', now() - %s::interval,
        now() - %s::interval, CASE WHEN %s::interval IS NULL THEN NULL ELSE now() - %s::interval END)""",
                 (convocation, affinity, status, reason, attempts, enqueued_ago, enqueued_ago, claimed_ago, claimed_ago))


def close_at(conn, convocation, opportunity, *, proposals=None, registration=None):
    conn.execute("""INSERT INTO prod2_details (id_convocatoria, opportunity_id, detail_hash, detail_fetched_at, source_url,
        proposals_closes_at, registration_closes_at, raw_detail) VALUES (%s, %s, %s, now(), 'https://seace.invalid',
        %s, %s, '{}')""", (convocation, opportunity, "b" * 64, proposals, registration))


def queued(conn, convocation, affinity=90, *, proposals=None, registration=None, **fields):
    """A queue item for a discovered convocatoria whose close dates are the given ones (none: no detail row)."""
    opportunity = seed_convocatoria(conn, convocation, closes_in_hours=None)
    if proposals or registration:
        close_at(conn, convocation, opportunity, proposals=proposals, registration=registration)
    enqueue(conn, convocation, affinity, **fields)
    return opportunity


def status_of(conn, convocation):
    return conn.execute("SELECT status, reason, attempts, claimed_at, finished_at, updated_at FROM prod2_reading_queue "
                        "WHERE id_convocatoria = %s", (convocation,)).fetchone()


@needs_db
def test_claims_go_by_earliest_close_then_affinity_then_age_and_the_unknown_last(db):
    seed_profiles(db)
    db.execute("DELETE FROM prod2_reading_queue")
    base = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    queued(db, 5101, 85, proposals=base + timedelta(hours=5))
    queued(db, 5102, 95, proposals=base + timedelta(hours=5))  # Same close, better affinity: before 5101.
    queued(db, 5103, 85, registration=base)  # No proposals date: its registration close is the one.
    queued(db, 5104, 99, proposals=base + timedelta(days=1), registration=base - timedelta(days=300))  # Proposals win.
    queued(db, 5105, 99)  # No dates at all, best affinity of the unknown.
    queued(db, 5106, 90, enqueued_ago=timedelta(hours=2))
    queued(db, 5107, 90, enqueued_ago=timedelta(hours=1))
    queued(db, 5108, 100, proposals=base - timedelta(days=2), status="done")  # Not pending: never claimed.
    queued(db, 5109, 100, proposals=base - timedelta(days=2), status="reading", attempts=1, claimed_ago=timedelta(minutes=5))
    queue = mg.ReadingQueue(db)
    taken = [queue.claim() for _ in range(8)]
    assert [row and row["id_convocatoria"] for row in taken] == [5103, 5102, 5101, 5104, 5105, 5106, 5107, None]
    assert all(row["status"] == "reading" and row["attempts"] == 1 and row["claimed_at"] for row in taken[:-1])
    assert queue.has_work() is False


@needs_db
def test_a_claim_counts_the_attempt_stamps_the_time_and_returns_the_whole_row(db):
    seed_profiles(db)
    db.execute("DELETE FROM prod2_reading_queue")
    enqueue(db, 5201, 91.5, attempts=1, reason="reading_failed", enqueued_ago=timedelta(hours=3))
    before = status_of(db, 5201)
    row = mg.ReadingQueue(db).claim()
    assert row["id_convocatoria"] == 5201 and row["affinity_index"] == 91.5 and row["enqueued_by"] == "test-run"
    assert (row["status"], row["attempts"], row["reason"]) == ("reading", 2, "reading_failed")
    after = status_of(db, 5201)
    assert after["claimed_at"] and after["updated_at"] > before["updated_at"] and after["finished_at"] is None


@needs_db
def test_the_skip_list_leaves_out_what_the_pass_already_handled(db):
    seed_profiles(db)
    db.execute("DELETE FROM prod2_reading_queue")
    enqueue(db, 5301, 99)
    enqueue(db, 5302, 90)
    queue = mg.ReadingQueue(db)
    assert queue.claim(skip={5301})["id_convocatoria"] == 5302
    assert queue.claim(skip={5301, 5302}) is None
    assert queue.claim(skip=[])["id_convocatoria"] == 5301


@needs_db
def test_items_left_reading_for_more_than_six_hours_go_back_to_pending(db):
    seed_profiles(db)
    db.execute("DELETE FROM prod2_reading_queue")
    enqueue(db, 5401, status="reading", attempts=1, claimed_ago=timedelta(hours=6, minutes=1))
    enqueue(db, 5402, status="reading", attempts=2, claimed_ago=timedelta(hours=30))
    enqueue(db, 5403, status="reading", attempts=1, claimed_ago=timedelta(hours=5, minutes=59))  # Still being read.
    enqueue(db, 5404, status="done", claimed_ago=timedelta(hours=30))
    enqueue(db, 5405, status="failed", claimed_ago=timedelta(hours=30), reason="reading_failed")
    enqueue(db, 5406, status="pending")
    queue = mg.ReadingQueue(db)
    assert queue.recover() == 2
    rows = {n: status_of(db, n) for n in range(5401, 5407)}
    assert [rows[n]["status"] for n in range(5401, 5407)] == ["pending", "pending", "reading", "done", "failed", "pending"]
    assert rows[5401]["reason"] == "claim_expired" and rows[5401]["attempts"] == 1 and rows[5402]["attempts"] == 2
    assert rows[5405]["reason"] == "reading_failed" and queue.recover() == 0
    assert {queue.claim()["id_convocatoria"] for _ in range(3)} == {5401, 5402, 5406}  # Recovered ones are taken again.


@needs_db
def test_every_change_of_a_claim_is_a_compare_and_set_on_its_attempt(db):
    seed_profiles(db)
    db.execute("DELETE FROM prod2_reading_queue")
    for convocation in (5501, 5502, 5503, 5504):
        enqueue(db, convocation)
    queue = mg.ReadingQueue(db)
    first, second, third, fourth = (queue.claim() for _ in range(4))
    assert queue.settle(first, "done") and queue.settle(second, "failed", "reading_failed")
    assert queue.settle(third, "skipped", "already_read")
    assert not queue.settle(first, "failed", "reading_failed")  # Final: not reopened by a holder of a stale copy.
    assert (status_of(db, 5501)["status"], status_of(db, 5501)["reason"]) == ("done", None)
    assert status_of(db, 5501)["finished_at"] and status_of(db, 5502)["reason"] == "reading_failed"
    assert status_of(db, 5503)["reason"] == "already_read"
    stale = {**fourth, "attempts": fourth["attempts"] - 1}
    assert not queue.settle(stale, "done") and not queue.release(stale) and status_of(db, 5504)["status"] == "reading"
    assert queue.release(fourth, "reading_failed")  # A failed attempt: back to the queue, the attempt counted.
    assert (status_of(db, 5504)["status"], status_of(db, 5504)["reason"], status_of(db, 5504)["attempts"]) == (
        "pending", "reading_failed", 1)
    again = queue.claim()
    assert again["id_convocatoria"] == 5504 and again["attempts"] == 2
    assert queue.release(again, refund=True)  # Nothing lost: the attempt is given back and the last reason kept.
    assert (status_of(db, 5504)["status"], status_of(db, 5504)["reason"], status_of(db, 5504)["attempts"]) == (
        "pending", "reading_failed", 1)
    zero = queue.claim()
    assert queue.release(zero, refund=True) and queue.release({**zero, "attempts": 1}, refund=True) is False
    assert status_of(db, 5504)["attempts"] == 1
    fresh = queue.claim()
    assert queue.release(fresh, refund=True) and status_of(db, 5504)["attempts"] == 1


@needs_db
def test_the_counters_name_every_status(db):
    seed_profiles(db)
    db.execute("DELETE FROM prod2_reading_queue")
    queue = mg.ReadingQueue(db)
    assert queue.counts() == dict.fromkeys(mg.QUEUE_STATUSES, 0) and queue.has_work() is False
    for number, status in enumerate(("pending", "pending", "reading", "done", "done", "done", "failed", "skipped")):
        enqueue(db, 5600 + number, status=status, claimed_ago=timedelta(minutes=1) if status == "reading" else None)
    assert queue.counts() == {"pending": 2, "reading": 1, "done": 3, "failed": 1, "skipped": 1}
    assert queue.has_work() is True


@needs_db
def test_the_free_checks_of_an_item_read_the_real_schema(db):
    seed_profiles(db)
    seed_convocatoria(db, 5701, affinity=88, closes_in_hours=5)
    store = mg.Requests(db)
    mg.precheck_auto(store, datetime.now(timezone.utc), 5701)
    with pytest.raises(mg.Skip) as closed:
        mg.precheck_auto(store, datetime.now(timezone.utc) + timedelta(hours=6), 5701)
    assert closed.value.reason == "convocatoria_cerrada"
    with pytest.raises(mg.Fail) as unknown:
        mg.precheck_auto(store, datetime.now(timezone.utc), 5799)
    assert unknown.value.reason == "contract_not_found"
    seed_convocatoria(db, 5702, affinity=59)
    with pytest.raises(mg.Fail) as low:
        mg.precheck_auto(store, datetime.now(timezone.utc), 5702)
    assert low.value.reason == "jev_below_60"


@needs_db
def test_two_consumers_never_claim_the_same_item_and_a_locked_one_is_skipped(sessions):
    one, two = sessions(), sessions()
    tenant = seed_profiles(one)
    convocations = (900101, 900102, 900103)
    for number, convocation in enumerate(convocations):
        queued(one, convocation, 90, proposals=FAR_PAST + timedelta(hours=number))  # Earlier than anything real.
    try:
        with one.transaction():
            held = one.execute(mg.QUEUE_PICK_SQL, {"skip": []}).fetchone()  # The first consumer is mid-claim: row locked.
            taken = mg.ReadingQueue(two).claim()
            assert held["id_convocatoria"] == convocations[0] and taken["id_convocatoria"] == convocations[1]
        assert mg.ReadingQueue(two).claim()["id_convocatoria"] == convocations[0]  # The first never moved its row.
        assert mg.ReadingQueue(two).claim()["id_convocatoria"] == convocations[2]
        assert one.execute("SELECT count(*) AS n FROM prod2_reading_queue WHERE id_convocatoria = ANY(%s) AND status = 'pending'",
                           (list(convocations),)).fetchone()["n"] == 0
    finally:
        one.execute("DELETE FROM prod2_reading_queue WHERE id_convocatoria = ANY(%s)", (list(convocations),))
        purge(one, tenant, *convocations)


@needs_db
def test_a_whole_automatic_pass_on_the_real_database_reads_evaluates_and_marks_the_item(sessions, tmp_path):
    seed = sessions()
    tenant = seed_profiles(seed)
    opportunity = queued(seed, 4600, 93, proposals=datetime.now(timezone.utc) + timedelta(hours=48))
    try:
        seen = []

        def read_auto(rt, store, owner, case, convocation):
            seen.append(("read", seed.execute("SELECT status FROM prod2_reading_queue WHERE id_convocatoria = %s",
                                              (convocation,)).fetchone()["status"], case.folder))
            seed_reading(seed, convocation, opportunity)

        def auto_evaluate(conn, settings_, receipts, **kwargs):
            seen.append(("evaluate", conn.execute("SELECT status FROM prod2_reading_queue WHERE id_convocatoria = 4600"
                                                  ).fetchone()["status"], kwargs["convocations"]))
            return {"planned": 2, "outcomes": [{"outcome": "evaluated"}], "stopped": None, "cost_usd": 0}
        t = make(tmp_path, service=None, requests=mg.Requests, queue=mg.ReadingQueue,
                 connect=lambda: mg.database(DATABASE_URL), read_auto=read_auto, auto_evaluate=auto_evaluate,
                 now=lambda: datetime.now(timezone.utc), auto_window=(day_time(0, 0), day_time(23, 59, 59)), max_auto=1)
        (t.service_dir / "state.private.json").write_text(json.dumps(
            {"status": "waiting", "next_cycle_at": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()}))
        summary = mg.run_pass(t.rt)
        assert summary["auto"]["handled"] == {"done": 1} and "skipped" not in summary
        assert seen == [("read", "reading", t.state / "auto" / "4600"), ("evaluate", "done", [4600])]
        final = status_of(seed, 4600)
        assert (final["status"], final["reason"], final["attempts"]) == ("done", None, 1) and final["finished_at"]
        assert summary["queue"]["done"] >= 1
        assert seed.execute(mg.LOCK_SQL).fetchone()["locked"] is True  # The pass handed the executor lock back.
        seed.execute(mg.UNLOCK_SQL)
        assert mg.run_pass(t.rt)["skipped"] == "idle"
    finally:
        seed.execute("DELETE FROM prod2_reading_queue WHERE id_convocatoria = 4600")
        purge(seed, tenant, 4600)


@needs_db
def test_pending_items_whose_saved_close_passed_are_skipped_without_a_ficha(db):
    seed_profiles(db)
    db.execute("DELETE FROM prod2_reading_queue")
    now = datetime.now(timezone.utc)
    queued(db, 5501, proposals=now - timedelta(hours=1))  # Closed: skipped for free.
    queued(db, 5502, proposals=now + timedelta(days=2), registration=now - timedelta(days=3))  # Proposals still open.
    queued(db, 5503, registration=now - timedelta(minutes=5))  # Only a registration close, already passed.
    queued(db, 5504)  # No saved dates: the fresh ficha decides.
    queued(db, 5505, proposals=now - timedelta(days=1), status="reading", attempts=1, claimed_ago=timedelta(minutes=5))
    queue = mg.ReadingQueue(db)
    assert queue.skip_closed() == 2
    rows = {n: status_of(db, n) for n in range(5501, 5506)}
    assert [rows[n]["status"] for n in range(5501, 5506)] == ["skipped", "pending", "skipped", "pending", "reading"]
    assert rows[5501]["reason"] == "convocatoria_cerrada" and rows[5501]["finished_at"] and rows[5501]["attempts"] == 0
    assert queue.skip_closed() == 0
