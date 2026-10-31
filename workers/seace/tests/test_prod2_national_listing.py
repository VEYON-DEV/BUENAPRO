"""National daily listing (discovery without keywords); synthetic, no network, Redis, DB or provider calls."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import accepted_repository as accepted, keyword_sweep as sweep
from buenapro_worker.prod2.batch_plan import digest
from buenapro_worker.prod2.discovery import CaptureError, validate_capture
from buenapro_worker.prod2.keyword_tracking import (
    KeywordTracker, completed_cache_errors, scope_for, seed_queries, summarize_result, validated_query,
)
from buenapro_worker.prod2.national_listing import MAX_DAYS, POLICY, listing_days, listing_queries
from test_prod2_keyword_sweep import Factory, NOW, no_http, row
from test_prod2_keyword_tracking_cache import Connection, EPOCH, task

DAY = "2026-10-03"
QUERY = {"keyword": "", "object_type": "service", "year": 2026,
         "published_from": DAY, "published_to": DAY, "seace_version": "3"}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(sweep, "_now", lambda: NOW)


def national_rows(count, day=DAY, label="Servicio"):
    return [row(index, day, label=label) for index in range(count)]


def sweep_day(folder, rows, **kwargs):
    factory = Factory(rows)
    offered = []
    report = sweep.sweep_query(QUERY, source_factory=factory, emit=lambda item: offered.append(item) or True,
                               folder=folder, allow_empty_keyword=True, **kwargs)
    return report, offered, factory


# --- Planning: one query per publication day and object ---------------------------------------------------------

def test_one_query_per_day_and_object_with_the_year_of_the_day():
    queries = listing_queries("2025-12-30", "2026-01-01")
    assert len(queries) == 6
    assert [(q["published_from"], q["object_type"], q["year"]) for q in queries] == [
        ("2025-12-30", "good", 2025), ("2025-12-30", "service", 2025),
        ("2025-12-31", "good", 2025), ("2025-12-31", "service", 2025),
        ("2026-01-01", "good", 2026), ("2026-01-01", "service", 2026)]
    for query in queries:
        assert query["keyword"] == "" and query["published_from"] == query["published_to"]
        assert query["seace_version"] == "3" and set(query) == {
            "keyword", "object_type", "year", "published_from", "published_to", "seace_version"}


def test_a_single_day_window_has_one_good_and_one_service_query():
    assert [q["object_type"] for q in listing_queries("2026-10-03", "2026-10-03")] == ["good", "service"]


def test_the_window_is_bounded_ordered_and_limited_to_goods_and_services():
    assert len(listing_days("2026-09-03", "2026-10-03")) == MAX_DAYS == 31
    with pytest.raises(ValueError, match="exceeds"):
        listing_days("2026-09-02", "2026-10-03")
    with pytest.raises(ValueError, match="reversed"):
        listing_days("2026-10-04", "2026-10-03")
    for objects in ([], ["works"], ["good", "good"]):
        with pytest.raises(ValueError, match="goods and services"):
            listing_queries(DAY, DAY, objects)
    assert [q["object_type"] for q in listing_queries(DAY, DAY, ["service"])] == ["service"]


def test_a_day_of_a_year_missing_from_the_resolved_years_is_refused():
    assert len(listing_queries("2026-01-01", "2026-01-02", resolved_years=[2026])) == 4
    with pytest.raises(ValueError, match="resolved years"):
        listing_queries("2025-12-31", "2026-01-01", resolved_years=[2026])


# --- Validation: an empty keyword only in this mode -------------------------------------------------------------

def capture(query, **page):
    return {"query": query, "observed_at": NOW.isoformat(),
            "page": {"number": 1, "total_pages": 1, "total_results": 0, "page_size": 1, **page}, "rows": []}


def test_an_empty_keyword_is_refused_unless_the_listing_is_explicit():
    with pytest.raises(CaptureError, match="keyword"):
        validate_capture(capture(QUERY), now=NOW)
    with pytest.raises(CaptureError, match="keyword"):
        validate_capture(capture(QUERY), now=NOW, allow_empty_keyword=False)
    assert validate_capture(capture(QUERY), now=NOW, allow_empty_keyword=True).query == QUERY


@pytest.mark.parametrize("keyword", [" ", "   ", "\t"])
def test_a_blank_keyword_is_never_the_empty_listing(keyword):
    with pytest.raises(CaptureError, match="keyword"):
        validate_capture(capture({**QUERY, "keyword": keyword}), now=NOW, allow_empty_keyword=True)


def test_the_empty_listing_needs_a_one_day_window():
    with pytest.raises(CaptureError, match="one-day"):
        validate_capture(capture({**QUERY, "published_to": "2026-10-04"}), now=NOW, allow_empty_keyword=True)


def test_a_keyword_query_is_unaffected_by_the_explicit_listing():
    keyword = {**QUERY, "keyword": "software", "published_to": "2026-10-05"}
    assert validate_capture(capture(keyword), now=NOW, allow_empty_keyword=True).query == keyword
    assert validate_capture(capture(keyword), now=NOW).query == keyword


def test_the_keyword_sweep_and_tracking_still_refuse_an_empty_keyword_by_default(tmp_path):
    with pytest.raises(CaptureError, match="keyword"):
        sweep.sweep_query(QUERY, source_factory=Factory([]), emit=lambda item: True, folder=tmp_path)
    with pytest.raises(CaptureError, match="keyword"):
        validated_query(QUERY)
    with pytest.raises(CaptureError, match="keyword"):
        sweep.inspect_complete_cache(QUERY, tmp_path)
    assert validated_query(QUERY, allow_empty_keyword=True) == QUERY
    assert list(tmp_path.iterdir()) == []


# --- Sweep: the native search is made without a keyword -----------------------------------------------------------

def test_a_day_is_swept_with_the_native_search_without_a_keyword(tmp_path):
    report, offered, factory = sweep_day(tmp_path, national_rows(5))
    assert report["status"] == "complete" and report["coverage_complete"] and len(offered) == 5
    searches = [event for event in factory.events if event[0] == "search"]
    assert searches and all(event[1] == "" and event[2]["allow_empty_keyword"] is True for event in searches)
    assert all(event[2]["published_from"] == event[2]["published_to"] == "03/10/2026" for event in searches)
    assert all(item["query"] == QUERY for item in offered)
    # The discovery pass and the fresh verification session both asked without a keyword: 2 sessions.
    assert factory.sessions == 2 and len(searches) == 2


def test_an_empty_day_is_complete_and_a_cached_day_replays_without_http(tmp_path):
    report, offered, _ = sweep_day(tmp_path, [])
    assert report["coverage_complete"] and offered == []
    replay = sweep.sweep_query(QUERY, source_factory=no_http, emit=lambda item: True, folder=tmp_path,
                               allow_empty_keyword=True)
    assert replay["coverage_complete"] and replay["http_searches"] == 0


def test_a_replay_of_a_cached_day_cannot_skip_the_explicit_listing(tmp_path):
    sweep_day(tmp_path, national_rows(3))
    with pytest.raises(CaptureError, match="keyword"):
        sweep.sweep_query(QUERY, source_factory=no_http, emit=lambda item: True, folder=tmp_path)


def test_a_capped_single_day_still_requires_review_and_is_not_split(tmp_path):
    rows = [row(index, DAY) for index in range(2)]

    def capped(result, number):
        result["counter"] = [1, 2, 499, 1, 250]
        result["snapshot"].update(total_rows=499, total_pages=250)
        result["rows"] = result["rows"][:2]

    factory = Factory(rows, mutate=capped)
    report = sweep.sweep_query(QUERY, source_factory=factory, emit=lambda item: True, folder=tmp_path,
                               allow_empty_keyword=True)
    assert report["status"] == "blocked" and not report["coverage_complete"]
    assert [error["code"] for error in report["errors"]] == ["capped_single_day_requires_review"]
    assert len([event for event in factory.events if event[0] == "search"]) == 1
    with pytest.raises(sweep.SweepError, match="capped_single_day_requires_review"):
        sweep._split(QUERY)


def test_completed_day_evidence_is_inspected_only_with_the_explicit_listing(tmp_path):
    report, _, _ = sweep_day(tmp_path, national_rows(4))
    assert report["coverage_complete"]
    evidence = sweep.inspect_complete_cache(QUERY, tmp_path, allow_empty_keyword=True)
    assert evidence["query"] == QUERY and evidence["coverage_kind"] == "observed_pass"
    with pytest.raises(CaptureError, match="keyword"):
        sweep.inspect_complete_cache(QUERY, tmp_path)


# --- Tracking: scope, seeding and the epoch cache --------------------------------------------------------------------

def test_the_policy_enters_the_epoch_scope_only_when_used():
    manifest = {"published_from": DAY, "published_to": DAY, "keywords": [""], "resolved_years": [2026]}
    keyword = scope_for({"published_from": DAY, "published_to": DAY})
    assert "discovery_policy" not in keyword and scope_for(manifest) == keyword
    national = scope_for({**manifest, "discovery_policy": POLICY})
    assert national == {**keyword, "discovery_policy": POLICY} and national != keyword


class Seeding:
    """Only what ``seed_queries`` writes; nothing connects to PostgreSQL."""

    def __init__(self, scope):
        self.scope, self.inserted = scope, []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, params=()):
        scope = self.scope
        return SimpleNamespace(fetchone=lambda: {"scope_hash": digest(scope), "frozen_scope": scope})

    @contextmanager
    def cursor(self):
        yield SimpleNamespace(executemany=lambda sql, rows: self.inserted.extend(rows))


def test_the_national_queries_are_seeded_only_for_a_national_manifest():
    manifest = {"published_from": "2026-10-03", "published_to": "2026-10-04", "discovery_policy": POLICY}
    queries = listing_queries("2026-10-03", "2026-10-04")
    connection = Seeding(scope_for(manifest))
    seed_queries(connection, "epoch-a", manifest, queries)
    assert len(connection.inserted) == 4
    keyword = {k: v for k, v in manifest.items() if k != "discovery_policy"}
    with pytest.raises(CaptureError, match="keyword"):
        seed_queries(Seeding(scope_for(keyword)), "epoch-a", keyword, queries)


def test_a_listing_day_outside_the_epoch_window_is_refused_and_a_keyword_query_still_needs_the_whole_window():
    manifest = {"published_from": "2026-10-03", "published_to": "2026-10-04", "discovery_policy": POLICY}
    connection = Seeding(scope_for(manifest))
    with pytest.raises(ValueError, match="frozen epoch"):
        seed_queries(connection, "epoch-a", manifest, listing_queries("2026-10-05", "2026-10-05"))
    assert connection.inserted == []
    whole = {"published_from": "2026-10-03", "published_to": "2026-10-04"}
    day = {**QUERY, "keyword": "software"}
    with pytest.raises(ValueError, match="frozen epoch"):
        seed_queries(Seeding(scope_for(whole)), "epoch-a", whole, [day])


def native_task(cache, query=QUERY):
    path = cache / EPOCH / digest(query)
    report = sweep.sweep_query(query, source_factory=Factory(national_rows(5)), emit=lambda item: True,
                               folder=path, allow_empty_keyword=True)
    assert report["coverage_complete"]
    report.update(sweep.inspect_complete_cache(query, path, allow_empty_keyword=True))
    return task(summarize_result(query, report), query)


def test_completed_national_evidence_is_read_back_only_with_the_explicit_listing(tmp_path):
    connection = Connection([native_task(tmp_path)])
    assert completed_cache_errors(connection, EPOCH, tmp_path, allow_empty_keyword=True) == []
    errors = completed_cache_errors(connection, EPOCH, tmp_path)
    assert len(errors) == 1 and errors[0]["status"] == "blocked"


def test_the_tracker_runs_a_national_day_and_reuses_its_cache(tmp_path):
    connection = Connection([native_task(tmp_path / "seed")])
    seeded = connection.tasks[0]
    # Re-home the seeded evidence in the tracker's epoch folder and run the same query again.
    tracker = KeywordTracker(lambda: connection, EPOCH, "run-1", tmp_path / "seed", allow_empty_keyword=True)
    calls = []
    result = tracker.run(QUERY, lambda path: calls.append(path) or sweep.sweep_query(
        QUERY, source_factory=no_http, emit=lambda item: True, folder=path, allow_empty_keyword=True))
    assert calls and result["coverage_complete"] and result["http_searches"] == 0
    assert seeded["status"] == "complete"
    with pytest.raises(CaptureError, match="keyword"):
        KeywordTracker(lambda: connection, EPOCH, "run-2", tmp_path / "seed").run(QUERY, lambda path: {})


# --- Accepted candidates keep a query without keyword ----------------------------------------------------------------

def test_promoting_a_candidate_of_the_listing_validates_its_empty_keyword_only_when_explicit():
    discovered = {"procedure_id": 11, "convocation_id": 22, "nomenclature": "N-1", "buyer_name": "BUYER",
                  "published_at": "03/10/2026 10:00", "description": "SERVICIO DE SOFTWARE", "object_label": "Servicio",
                  "reference_amount": "1,000.00", "currency": "Soles", "seace_version": "3"}
    observed = datetime(2026, 10, 3, 18, tzinfo=timezone.utc)
    with pytest.raises(CaptureError, match="keyword"):
        accepted._capture_row(deepcopy(discovered), QUERY, observed)
    capture = accepted._capture_row(deepcopy(discovered), QUERY, observed, allow_empty_keyword=True)
    assert capture.query == QUERY and capture.rows[0]["convocation_id"] == 22


def test_a_fork_of_a_stopped_queue_cannot_change_the_discovery_policy():
    from buenapro_worker.prod2 import embedding_resume as resume
    source = {"keywords": ["software"], "years": [2026]}
    resume._same_scope(source, dict(source))
    with pytest.raises(resume.EmbeddingResumeError, match="frozen source"):
        resume._same_scope(source, {**source, "discovery_policy": POLICY})
    resume._same_scope({**source, "discovery_policy": POLICY}, {**source, "discovery_policy": POLICY})
