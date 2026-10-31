"""The national listing's current day: retried in the same run, then tolerated as partial.

Synthetic and offline: no SEACE, PostgreSQL, Redis or provider call; the pause is a recorded fake.
"""
from copy import deepcopy
from datetime import date, datetime

import pytest

from buenapro_worker.prod2 import national_listing as listing
from buenapro_worker.prod2.keyword_chain import assess_final_report
from buenapro_worker.prod2.national_listing import CURRENT_DAY_CODES, POLICY, listing_queries
from test_prod2_keyword_chain import RUN, report as chain_report
from test_prod2_keyword_sweep import Source, row
from test_prod2_selected_national_listing import national_manifest
from test_prod2_selected_script import args, subject
from test_prod2_service import Clock, Egress, NOW, ROOT, fake_children, paths, service, write  # noqa: F401
from test_prod2_service_schedule import national

PAST, TODAY = "2026-10-17", "2026-10-18"
RECONCILE, COUNTER = "first_page_reconciliation_changed", "counter_changed_during_pass"
QUERIES = listing_queries(PAST, TODAY, ["service"])  # [the previous day, the current day]


class FakeStop:
    """STOP with a recorded pause: ``wait`` returns at once (True when the stop arrives during it)."""

    def __init__(self, stop_during_wait=False):
        self.waits, self.stopped, self.stop_during_wait = [], False, stop_during_wait

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = self.stopped or self.stop_during_wait
        return self.stopped


def day_rows(day, base):
    return [row(base + index, day, label="Servicio") for index in range(3)]  # Two pages of two.


class DaySource(Source):
    """The sweep tests' source, listing whatever the script says the day has at each first-page search."""

    def __init__(self, script, events, **options):
        super().__init__([], events, **options)
        self.script = script

    def search(self, keyword, **arguments):
        day = datetime.strptime(arguments["published_from"], "%d/%m/%Y").date().isoformat()
        self.all_rows = self.script(day)
        return super().search(keyword, **arguments)


class Days:
    """Per-day listings. On the ``unstable`` day the first ``failures`` passes fail, either because a new
    publication arrives before the verification (the first page changes) or because the total grows."""

    def __init__(self, unstable, failures, how=RECONCILE, failure_page=None):
        self.unstable, self.failures, self.how, self.failure_page = unstable, failures, how, failure_page
        self.searches, self.arrived, self.events = {}, 0, []

    def script(self, day):
        count = self.searches[day] = self.searches.get(day, 0) + 1
        rows = day_rows(day, 10 if day == TODAY else 0)
        if day == self.unstable and self.how == RECONCILE:
            # A pass makes two first-page searches: the discovery (odd) and the verification (even).
            if count % 2 == 0 and (count - 1) // 2 < self.failures:
                self.arrived += 1  # It stays listed afterwards, like a real publication.
            return [row(100 + index, day, label="Servicio") for index in range(self.arrived)] + rows
        return rows

    def mutate(self, result, number):
        day = datetime.strptime(result["snapshot"]["published_from"], "%d/%m/%Y").date().isoformat()
        if (self.how == COUNTER and number == 2 and day == self.unstable
                and self.searches.get(self.unstable, 0) <= self.failures):
            result["rows"] = [*result["rows"], row(77, self.unstable, label="Servicio")]
            result["counter"] = [3, 4, 4, 2, 2]
            result["snapshot"].update(total_rows=4, total_pages=2)

    def factory(self, pacer):
        days = self

        class Session:
            def __enter__(self):
                return DaySource(days.script, days.events, mutate=days.mutate, failure_page=days.failure_page)

            def __exit__(self, *exc):
                return False

        return Session()


class Stage:
    def __init__(self):
        self.ids = set()

    def stage(self, candidate_id, payload):
        self.ids.add(int(candidate_id))
        return True

    def stats(self):
        return {"seen": len(self.ids)}


def sweep(tmp_path, monkeypatch, days, manifest=None, *, stop=None, today=TODAY):
    stop = stop or FakeStop()
    monkeypatch.setattr(subject, "source_factory", days.factory)
    monkeypatch.setattr(subject, "STOP", stop)
    monkeypatch.setattr(subject, "lima_today", lambda: date.fromisoformat(today))
    manifest = manifest or national_manifest(objects=["service"], published_from=PAST, published_to=TODAY)
    stage = Stage()
    limits = subject.RunLimits(max_accepted=10, max_candidates=100, max_jev_calls=100, max_llm1_calls=10)
    reports, _ = subject.run_sweeps(manifest, tmp_path, subject.SharedPacer(2), stage, limits)
    return {r["query"]["published_from"]: r for r in reports}, stage, stop


# --- Same-run retry -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("how", [RECONCILE, COUNTER])
def test_the_current_day_is_retried_after_a_pause_and_completes_without_a_flag(tmp_path, monkeypatch, how):
    reports, stage, stop = sweep(tmp_path, monkeypatch, Days(TODAY, 1, how))
    today = reports[TODAY]
    assert today["coverage_complete"] and today["status"] == "complete"
    assert today["current_day_retries"] == 1 and "current_day_partial" not in today
    assert stop.waits == [60]
    assert reports[PAST]["coverage_complete"] and "current_day_retries" not in reports[PAST]
    assert {2000, 2001, 2002, 2010, 2011, 2012} <= stage.ids
    coverage = list(reports.values())
    assert subject.source_coverage_settled(coverage) and subject.tolerated_current_day(coverage) == []


@pytest.mark.parametrize("how", [RECONCILE, COUNTER])
def test_the_current_day_still_partial_after_two_retries_is_flagged_and_keeps_its_rows(tmp_path, monkeypatch, how):
    reports, stage, stop = sweep(tmp_path, monkeypatch, Days(TODAY, 10, how))
    today = reports[TODAY]
    assert stop.waits == [60, 60]  # Two more tries, a minute apart: three passes in all.
    assert today["current_day_retries"] == 2 and today["current_day_partial"] is True
    assert today["status"] == "partial" and today["coverage_complete"] is False
    assert [error["code"] for error in today["errors"]] == [how]
    assert {2010, 2011} <= stage.ids  # What the passes listed was emitted, and goes on to JEV as usual.
    assert reports[PAST]["coverage_complete"]
    coverage = list(reports.values())
    assert subject.tolerated_current_day(coverage) == [today["query"]] and subject.source_coverage_settled(coverage)
    assert subject.new_policy_report_fields({"discovery_policy": POLICY}, [], coverage) == {
        "discovery_policy": POLICY, "current_day_partial": [today["query"]]}


def test_a_retry_of_the_current_day_is_a_new_attempt_of_the_same_cached_query(tmp_path, monkeypatch):
    reports, _, _ = sweep(tmp_path, monkeypatch, Days(TODAY, 1))
    attempts = lambda query: list((tmp_path / "queries" / subject.digest(query)).glob("sweep-*/windows/*/attempt-*"))
    assert len(attempts(reports[TODAY]["query"])) == 2  # The partial pass stays as evidence.
    assert len(attempts(reports[PAST]["query"])) == 1


def test_a_previous_day_left_partial_stays_incomplete_and_is_not_retried(tmp_path, monkeypatch):
    reports, _, stop = sweep(tmp_path, monkeypatch, Days(PAST, 10))
    past = reports[PAST]
    assert stop.waits == [] and past["status"] == "partial" and past["coverage_complete"] is False
    assert [error["code"] for error in past["errors"]] == [RECONCILE]
    assert "current_day_partial" not in past and "current_day_retries" not in past
    assert reports[TODAY]["coverage_complete"]
    coverage = list(reports.values())
    assert subject.tolerated_current_day(coverage) == [] and not subject.source_coverage_settled(coverage)


def test_a_day_that_is_not_the_current_one_in_lima_is_not_retried_either(tmp_path, monkeypatch):
    reports, _, stop = sweep(tmp_path, monkeypatch, Days(TODAY, 10), today="2026-10-25")
    assert stop.waits == [] and reports[TODAY]["status"] == "partial"
    assert "current_day_partial" not in reports[TODAY] and not subject.source_coverage_settled(list(reports.values()))


def test_any_other_error_on_the_current_day_is_neither_retried_nor_tolerated(tmp_path, monkeypatch):
    reports, _, stop = sweep(tmp_path, monkeypatch, Days(TODAY, 0, failure_page=2))
    today = reports[TODAY]
    assert stop.waits == [] and today["status"] == "partial"
    assert [error["code"] for error in today["errors"]] == ["source_or_capture_error"]
    assert "current_day_partial" not in today and "current_day_retries" not in today
    assert not subject.source_coverage_settled(list(reports.values()))


def test_a_stop_during_the_pause_ends_the_retries_and_the_day_stays_incomplete(tmp_path, monkeypatch):
    reports, _, stop = sweep(tmp_path, monkeypatch, Days(TODAY, 10), stop=FakeStop(stop_during_wait=True))
    assert stop.waits == [60]
    assert "current_day_partial" not in reports[TODAY] and not reports[TODAY]["coverage_complete"]
    assert not subject.source_coverage_settled(list(reports.values()))


def test_the_keyword_mode_neither_retries_nor_tolerates_the_same_instability(tmp_path, monkeypatch):
    manifest = subject.plan(args("--keyword", "software", "--from-date", TODAY, "--to-date", TODAY)) | {
        "resolved_years": [2026], "objects": ["service"], "published_from": TODAY, "published_to": TODAY}
    assert "discovery_policy" not in manifest
    reports, _, stop = sweep(tmp_path, monkeypatch, Days(TODAY, 10), manifest)
    only = reports[TODAY]
    assert stop.waits == [] and only["status"] == "partial" and only["coverage_complete"] is False
    assert [error["code"] for error in only["errors"]] == [RECONCILE]
    assert "current_day_partial" not in only and "current_day_retries" not in only
    assert not subject.source_coverage_settled(list(reports.values()))
    assert subject.new_policy_report_fields(manifest, [], list(reports.values())) == {}


def test_the_current_day_helpers_are_exact_about_day_codes_and_status():
    query = {"keyword": "", "object_type": "service", "year": 2026, "published_from": TODAY,
             "published_to": TODAY, "seace_version": "3"}
    today = date.fromisoformat(TODAY)
    partial = {"status": "partial", "errors": [{"code": RECONCILE}]}
    assert listing.is_current_day(query, TODAY, today) and listing.is_current_day(query, TODAY, date(2026, 10, 19))
    assert not listing.is_current_day(query, TODAY, date(2026, 10, 20))        # Two days ago.
    assert not listing.is_current_day(query, "2026-10-19", today)              # Not the window's last day.
    assert not listing.is_current_day({**query, "published_from": PAST}, TODAY, today)
    assert listing.current_day_unstable(partial, query, TODAY, today)
    assert listing.current_day_unstable({**partial, "errors": [{"code": COUNTER}, {"code": RECONCILE}]},
                                        query, TODAY, today)
    for changed in ({"status": "blocked"}, {"status": "complete"}, {"errors": []}, {"errors": None},
                    {"errors": [{"code": RECONCILE}, {"code": "source_403"}]}, {"errors": [{"code": "other"}]},
                    {"errors": [{"code": "stop_requested"}]}):
        assert not listing.current_day_unstable({**partial, **changed}, query, TODAY, today), changed
    assert CURRENT_DAY_CODES == {RECONCILE, COUNTER}
    assert listing.CURRENT_DAY_RETRIES == 2 and listing.CURRENT_DAY_PAUSE_SECONDS == 60


# --- The final-report gate ------------------------------------------------------------------------------------------

def partial_day_fields():
    """The coverage of the national listing with its current day tolerated as partial."""
    past, today = deepcopy(QUERIES)  # Tests mutate what they get.
    return {"discovery_policy": POLICY, "queries_total": 2, "queries_complete": 1, "source_coverage_complete": True,
            "coverage": [{"query": past, "status": "complete", "coverage_complete": True, "errors": []},
                         {"query": today, "status": "partial", "coverage_complete": False,
                          "current_day_partial": True, "errors": [{"code": RECONCILE, "error_type": "SweepError"}]}],
            "current_day_partial": [today]}


def national_report():
    return {**chain_report(), **partial_day_fields()}


def gate(value, code=0):
    return assess_final_report(value, RUN, code)


def test_a_tolerated_current_day_completes_the_cycle_without_review():
    result = gate(national_report())
    assert result["advance"] is True and result["state"] == "complete"
    assert result["reason"] == "source_and_queue_complete" and result["summary"]["current_day_partial"] == 1
    assert gate(national_report(), 2)["state"] == "complete_with_review"


def test_a_national_report_without_a_partial_day_is_complete_as_before():
    value = chain_report()
    value.update(discovery_policy=POLICY, current_day_partial=[])
    result = gate(value)
    assert result["state"] == "complete" and result["summary"]["current_day_partial"] == 0
    assert "current_day_partial" not in gate(chain_report())["summary"]


def retag(value, index, **changes):
    value["coverage"][index].update(changes)


@pytest.mark.parametrize("change,reason", [
    (lambda v: (retag(v, 0, coverage_complete=False, status="partial", current_day_partial=True,
                      errors=[{"code": RECONCILE}]), v.update(current_day_partial=[QUERIES[0]])),
     "invalid_coverage_evidence"),                                                      # Not the last day.
    (lambda v: retag(v, 1, errors=[{"code": "source_or_capture_error"}]), "invalid_coverage_evidence"),
    (lambda v: retag(v, 1, errors=[{"code": RECONCILE}, {"code": "source_or_capture_error"}]),
     "invalid_coverage_evidence"),
    (lambda v: retag(v, 1, errors=[]), "invalid_coverage_evidence"),
    (lambda v: retag(v, 1, status="blocked"), "invalid_coverage_evidence"),
    (lambda v: v["coverage"][1].pop("current_day_partial"), "invalid_coverage_evidence"),
    (lambda v: retag(v, 1, coverage_complete=True), "invalid_coverage_evidence"),
    (lambda v: v.update(discovery_policy="prod2_keyword_v1"), "invalid_coverage_evidence"),
    (lambda v: v.pop("discovery_policy"), "invalid_coverage_evidence"),
    (lambda v: v.update(current_day_partial="all"), "invalid_coverage_evidence"),
    (lambda v: v.update(current_day_partial=[QUERIES[1], QUERIES[1]]), "invalid_coverage_evidence"),
    (lambda v: v.update(current_day_partial=[{**QUERIES[1], "year": 2025}]), "invalid_coverage_evidence"),
    (lambda v: (v["coverage"][1]["query"].update(keyword="software"),
                v.update(current_day_partial=[v["coverage"][1]["query"]])), "invalid_coverage_evidence"),
    (lambda v: v["coverage"][1]["errors"].append({"code": "source_403"}), "source_access_blocked"),
])
def test_the_gate_refuses_a_current_day_claim_that_does_not_hold(change, reason):
    value = national_report()
    change(value)
    result = gate(value)
    assert result["advance"] is False and result["reason"] == reason


@pytest.mark.parametrize("change", [
    lambda v: v.update(current_day_partial=[]),                          # Partial and not claimed.
    lambda v: v.update(source_coverage_complete=False),                  # The report itself says incomplete.
    lambda v: v.update(queries_complete=2),                              # Counted as complete and tolerated.
    lambda v: v.update(queries_total=3),
])
def test_an_unclaimed_or_inconsistent_partial_day_is_still_an_incomplete_source(change):
    value = national_report()
    change(value)
    result = gate(value)
    assert result["advance"] is False and result["reason"] == "source_incomplete"


@pytest.mark.parametrize("claimed_complete", [False, True])
def test_a_previous_day_partial_by_the_same_code_is_still_an_incomplete_source(claimed_complete):
    value = national_report()
    retag(value, 0, status="partial", coverage_complete=False, errors=[{"code": RECONCILE}])
    retag(value, 1, status="complete", coverage_complete=True, errors=[])
    value["coverage"][1].pop("current_day_partial")
    value.update(current_day_partial=[], source_coverage_complete=claimed_complete)
    result = gate(value)
    assert result["advance"] is False and result["reason"] == "source_incomplete"


def test_the_keyword_mode_cannot_borrow_the_tolerance():
    value = chain_report()
    value["coverage"][0].update(coverage_complete=False, status="partial", current_day_partial=True,
                                errors=[{"code": RECONCILE}], query=QUERIES[1])
    value.update(current_day_partial=[QUERIES[1]], queries_complete=1)
    assert gate(value)["reason"] == "invalid_coverage_evidence"
    value.pop("current_day_partial")
    assert gate(value)["reason"] == "source_incomplete"


# --- The service ------------------------------------------------------------------------------------------------------

def test_a_cycle_with_a_tolerated_current_day_completes_and_the_alert_says_so(paths, monkeypatch):
    config, state = paths
    write(config, national())
    children = fake_children(monkeypatch, mutate=lambda value: value.update(partial_day_fields()))
    egress = Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert len(children) == 1 and service._read(state / "state.private.json")["status"] == "complete"
    assert "leídas, día en curso parcial en 1 consulta, 0 en revisión" in egress.alerts[0]


def test_a_cycle_with_a_previous_day_partial_is_retried_once_and_then_stops_for_review(paths, monkeypatch):
    def previous_day_partial(value):
        past, today = QUERIES
        value.update(discovery_policy=POLICY, queries_total=2, queries_complete=1, source_coverage_complete=False,
                     coverage=[{"query": past, "status": "partial", "coverage_complete": False,
                                "errors": [{"code": RECONCILE}]},
                               {"query": today, "status": "complete", "coverage_complete": True, "errors": []}],
                     current_day_partial=[])

    config, state = paths
    write(config, national())
    children = fake_children(monkeypatch, mutate=previous_day_partial)
    Egress(monkeypatch, [])
    clock = Clock()
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 2
    assert len(children) == 2  # The usual single retry for an incomplete search.
    assert service._read(state / "state.private.json")["reason"] == "final_report_source_incomplete"
