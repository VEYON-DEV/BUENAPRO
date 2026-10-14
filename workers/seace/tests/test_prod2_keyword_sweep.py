"""Synthetic native discovery with no network, Redis, DB or provider calls."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import stat

import pytest

from buenapro_worker.prod2 import keyword_sweep as sweep
from buenapro_worker.prod2.client import Prod2ProtocolError


NOW = datetime(2026, 10, 6, 17, tzinfo=timezone.utc)
QUERY = {"keyword": "SOFTWARE", "object_type": "service", "year": 2026,
         "published_from": "2026-10-01", "published_to": "2026-10-05", "seace_version": "3"}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(sweep, "_now", lambda: NOW)


def row(index, day="2026-10-03", *, label="Servicio", description="SOFTWARE"):
    return {"procedure_id": 1000 + index, "convocatoria_id": 2000 + index,
        "entity": "SYNTHETIC BUYER", "published_at": date.fromisoformat(day).strftime("%d/%m/%Y") + " 12:30",
        "nomenclature": f"SYNTHETIC-2026-{index}", "object": label,
        "description": description, "amount_text": "1,234.56", "currency": "Soles", "seace_version": "3"}


class Source:
    def __init__(self, rows, events, *, size=2, mutate=None, failure_page=None, search_error=None):
        self.all_rows, self.events, self.size = deepcopy(rows), events, size
        self.mutate, self.failure_page, self.search_error = mutate, failure_page, search_error

    def search(self, keyword, **args):
        self.events.append(("search", keyword, deepcopy(args)))
        if self.search_error:
            raise self.search_error
        self.args = {"keyword": keyword, **args}
        start = datetime.strptime(args["published_from"], "%d/%m/%Y").date()
        end = datetime.strptime(args["published_to"], "%d/%m/%Y").date()
        self.rows = [value for value in self.all_rows
            if start <= datetime.strptime(value["published_at"], "%d/%m/%Y %H:%M").date() <= end]
        return self._result(1)

    def search_page(self, number):
        self.events.append(("page", number))
        if number == self.failure_page:
            raise Prod2ProtocolError("Synthetic page failure")
        return self._result(number)

    def _result(self, number):
        total = len(self.rows)
        pages = max(1, (total + self.size - 1) // self.size)
        start, end = (number - 1) * self.size, min(number * self.size, total)
        result = {"rows": deepcopy(self.rows[start:end]),
            "counter": [start + 1 if total else 0, end, total, number, pages],
            "search_verified": True, "coverage_complete": False,
            "counter_origin": "native_search_counter" if number == 1 else "initial_native_total_and_observed_indices",
            "snapshot": {"page_size": self.size, "total_rows": total, "total_pages": pages,
                **self.args, "year_filter_kind": "convocatoria_year", "native_atomic_snapshot": False}}
        if self.mutate:
            self.mutate(result, number)
        return result


class Factory:
    def __init__(self, rows, **options):
        self.rows, self.options, self.events, self.sessions, self.active = rows, options, [], 0, 0

    @contextmanager
    def __call__(self):
        self.sessions += 1
        self.active += 1
        assert self.active == 1, "Fresh verification follows closing the discovery session"
        options = self.options(self.sessions) if callable(self.options) else self.options
        rows = self.rows(self.sessions) if callable(self.rows) else self.rows
        try:
            yield Source(rows, self.events, **options)
        finally:
            self.active -= 1


def run(tmp_path, factory, *, emit=None, query=None, **kwargs):
    offered = []
    if emit is None:
        def emit(item):
            offered.append(item)
            return True
    report = sweep.sweep_query(query or QUERY, source_factory=factory, emit=emit, folder=tmp_path, **kwargs)
    return report, offered


def no_http():
    raise AssertionError("Complete cached passes must not access source_factory")


def test_cached_batch_replays_every_row_in_order_with_identical_cache_and_counters(tmp_path):
    first, offered = run(tmp_path, Factory([row(index) for index in range(5)]))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.private.json")
              if not p.name.startswith("checkpoint-")}
    batches = []
    def emit_many(items):
        batches.append(deepcopy(items))
        return [item["row"]["convocation_id"] == 2002 for item in items]
    replay, _ = run(tmp_path, no_http,
        emit=lambda _: (_ for _ in ()).throw(AssertionError("Cached replay must batch")),
        emit_many=emit_many)
    assert replay["coverage_complete"] and replay["http_searches"] == replay["http_pages"] == 0
    assert [item for batch in batches for item in batch] == offered
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert replay["rows_offered"] == first["rows_offered"] == 5
    assert replay["emit_returned_true"] == 1 and replay["emit_returned_false"] == 4
    assert before == {p: p.read_bytes() for p in before}


def test_batch_is_not_used_for_fresh_native_pages(tmp_path):
    report, offered = run(tmp_path, Factory([row(index) for index in range(5)]),
        emit_many=lambda _: (_ for _ in ()).throw(AssertionError("Native pages remain serial")))
    assert report["coverage_complete"] and len(offered) == 5
    assert report["cached_pages_reemitted"] == 0


@pytest.mark.parametrize("returned", [[True], [True, 1], None, True])
def test_cached_batch_invalid_response_cannot_establish_coverage(tmp_path, returned):
    run(tmp_path, Factory([row(index) for index in range(5)]))
    report, _ = run(tmp_path, no_http, emit_many=lambda _: returned)
    assert not report["coverage_complete"] and report["status"] == "partial"
    assert report["errors"][0]["code"] == "emit_must_return_bool"
    assert report["rows_offered"] == 2 and report["emit_returned_true"] == report["emit_returned_false"] == 0


def test_cached_batch_lost_response_retains_cache_and_never_claims_full_replay(tmp_path):
    run(tmp_path, Factory([row(index) for index in range(5)]))
    before = {p: p.read_bytes() for p in tmp_path.rglob("complete.private.json")}
    def lost(items):
        raise ConnectionError("Synthetic private transport detail")
    report, _ = run(tmp_path, no_http, emit_many=lost)
    assert not report["coverage_complete"] and report["status"] == "partial"
    assert report["errors"][0]["code"] == "emit_callback_error"
    assert "private transport" not in str(report)
    assert before == {p: p.read_bytes() for p in before}


def test_full_native_pagination_reconciles_in_new_session_and_normalizes_rows(tmp_path):
    factory = Factory([row(index) for index in range(5)])
    report, offered = run(tmp_path, factory)
    assert report["status"] == "complete" and report["coverage_complete"]
    assert report["coverage_kind"] == "observed_pass" and not report["source_atomic_snapshot"]
    assert report["pages_processed"] == 3 and report["http_pages"] == 2 and report["http_searches"] == 2
    assert factory.sessions == 2 and factory.active == 0
    assert [event[0] for event in factory.events] == ["search", "page", "page", "search"]
    assert [item["page_number"] for item in offered] == [1, 1, 2, 2, 3]
    assert all(set(item) == {"row", "query", "page_number", "observed_at"} for item in offered)
    assert offered[0]["row"]["convocation_id"] == 2000
    assert offered[0]["row"]["reference_amount"] == "1234.56"
    assert offered[0]["row"]["published_at"] == "2026-10-03T12:30:00-05:00"
    assert not any("cookie" in path.read_text() or "ViewState" in path.read_text() for path in tmp_path.rglob("*.json"))
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in tmp_path.rglob("*.json"))


def test_false_emits_never_stop_or_mean_missing_coverage(tmp_path):
    report, _ = run(tmp_path, Factory([row(index) for index in range(5)]), emit=lambda item: False)
    assert report["coverage_complete"] and report["rows_offered"] == 5
    assert report["emit_returned_false"] == 5 and report["emit_returned_true"] == 0


def test_reserved_reference_value_preserves_source_literal_and_replays_all_pages(tmp_path):
    rows = [row(index) for index in range(3)]
    rows[0]["amount_text"] = "Reservado"
    factory = Factory(rows)
    report, offered = run(tmp_path, factory)
    assert report["coverage_complete"] and report["rows_offered"] == 3
    assert offered[0]["row"]["reference_amount"] is None
    assert offered[1]["row"]["reference_amount"] == "1234.56"
    page_paths = sorted(tmp_path.rglob("page-*.private.json"))
    before = {path: path.read_bytes() for path in page_paths}
    first = json.loads(page_paths[0].read_bytes())
    assert first["capture"]["rows"][0]["reference_amount"] == "Reservado"
    again, replay = run(tmp_path, no_http)
    assert again["coverage_complete"] and again["http_searches"] == 0
    assert replay == offered
    assert before == {path: path.read_bytes() for path in page_paths}


@pytest.mark.parametrize("amount", ["Reservado?", "0,00", "1,234.567", "", None])
def test_unknown_or_malformed_reference_values_still_fail_closed(tmp_path, amount):
    source_row = row(0)
    source_row["amount_text"] = amount
    report, offered = run(tmp_path, Factory([source_row]))
    assert report["status"] == "partial" and not report["coverage_complete"]
    assert offered == []
    assert not list(tmp_path.rglob("complete.private.json"))


def test_complete_cache_reemits_every_page_without_http(tmp_path):
    first, offered = run(tmp_path, Factory([row(index) for index in range(5)]))
    assert first["coverage_complete"]
    before = {path: path.read_bytes() for path in tmp_path.rglob("page-*.json")}
    second, again = run(tmp_path, no_http)
    assert second["coverage_complete"] and again == offered
    assert second["http_searches"] == second["http_pages"] == 0
    assert second["cached_pages_reemitted"] == 3
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("page-*.json")}


def test_zero_is_verified_and_reconciled_not_inferred_from_http_failure(tmp_path):
    report, offered = run(tmp_path, Factory([]))
    assert report["coverage_complete"] and report["rows_offered"] == 0 and offered == []
    assert report["http_searches"] == 2 and report["pages_processed"] == 1
    cached, _ = run(tmp_path, no_http)
    assert cached["coverage_complete"] and cached["http_searches"] == 0


@pytest.mark.parametrize("count", [499, 500])
def test_result_boundary_splits_inclusive_dates_and_paginates_every_child(tmp_path, count):
    rows = [row(index, "2026-10-01" if index % 2 else "2026-10-02") for index in range(count)]
    query = {**QUERY, "published_to": "2026-10-02"}
    report, offered = run(tmp_path, Factory(rows, size=100), query=query)
    assert report["coverage_complete"] and len(offered) == count
    assert {item["query"]["published_from"] for item in offered} == {"2026-10-01", "2026-10-02"}
    assert all(item["query"]["published_from"] == item["query"]["published_to"] for item in offered)
    assert len({item["row"]["convocation_id"] for item in offered}) == count
    assert len(report["leaf_windows"]) == 2
    split = next(tmp_path.rglob("split.private.json"))
    proof = json.loads(split.read_text())
    assert proof["native_capture"]["page"]["total_results"] == count
    assert len(proof["native_capture"]["rows"]) == 100
    cached, again = run(tmp_path, no_http, query=query)
    assert cached["coverage_complete"] and again == offered


def test_498_is_not_silently_split_or_capped(tmp_path):
    report, offered = run(tmp_path, Factory([row(index) for index in range(498)], size=100))
    assert report["coverage_complete"] and len(offered) == 498
    assert len(report["leaf_windows"]) == 1 and not list(tmp_path.rglob("split.private.json"))


def test_capped_single_day_remains_blocked_without_emitting_a_fake_small_page(tmp_path):
    query = {**QUERY, "published_from": "2026-10-03", "published_to": "2026-10-03"}
    factory = Factory([row(index) for index in range(499)], size=100)
    report, offered = run(tmp_path, factory, query=query)
    assert report["status"] == "blocked" and not report["coverage_complete"] and offered == []
    assert report["errors"][0]["code"] == "capped_single_day_requires_review"
    assert factory.sessions == 1 and not list(tmp_path.rglob("complete.private.json"))
    assert not list(tmp_path.rglob("page-*.private.json"))
    proof = json.loads(next(tmp_path.rglob("capped-*.private.json")).read_text())
    assert proof["native_capture"]["page"]["total_results"] == 499
    assert len(proof["native_capture"]["rows"]) == 100 and not proof["coverage_complete"]


def test_stop_mid_page_is_partial_and_resume_reemits_full_cached_page_with_dedup(tmp_path):
    offered = []
    def emit(item):
        offered.append(item)
        return True
    first, _ = run(tmp_path, Factory([row(index) for index in range(3)]), emit=emit,
                   stop_requested=lambda: len(offered) >= 1)
    assert first["status"] == "stopped" and not first["coverage_complete"]
    assert not list(tmp_path.rglob("complete.private.json"))
    seen = {item["row"]["convocation_id"] for item in offered}
    def dedup(item):
        code = item["row"]["convocation_id"]
        new = code not in seen
        seen.add(code)
        return new
    factory = Factory([row(index) for index in range(3)])
    second, _ = run(tmp_path, factory, emit=dedup)
    assert second["coverage_complete"] and seen == {2000, 2001, 2002}
    assert second["cached_pages_reemitted"] == 1 and second["emit_returned_false"] == 1
    assert [event for event in factory.events if event[0] == "page"] == [("page", 2)]


def test_stop_before_search_does_not_open_a_session(tmp_path):
    report, offered = run(tmp_path, no_http, stop_requested=True)
    assert report["status"] == "stopped" and offered == [] and report["http_searches"] == 0


def test_complete_cache_with_stop_is_not_falsely_fully_reemitted(tmp_path):
    run(tmp_path, Factory([row(index) for index in range(3)]))
    offered = []
    report, _ = run(tmp_path, no_http, emit=lambda item: offered.append(item) or True,
                    stop_requested=lambda: len(offered) >= 1)
    assert report["status"] == "stopped" and not report["coverage_complete"]
    assert len(offered) == 1


def test_page_failure_keeps_private_capture_and_resumes_with_fresh_anchor(tmp_path):
    first, _ = run(tmp_path, Factory([row(index) for index in range(3)], failure_page=2))
    assert first["status"] == "partial" and not first["coverage_complete"]
    assert len(list(tmp_path.rglob("page-*.private.json"))) == 1
    factory = Factory([row(index) for index in range(3)])
    second, offered = run(tmp_path, factory)
    assert second["coverage_complete"] and len(offered) == 3
    assert second["cached_pages_reemitted"] == 1 and second["http_pages"] == 1


def test_changed_resume_anchor_starts_new_immutable_attempt(tmp_path):
    rows = [row(index) for index in range(3)]
    first, _ = run(tmp_path, Factory(rows, failure_page=2))
    assert not first["coverage_complete"]
    before = {path: path.read_bytes() for path in tmp_path.rglob("page-*.private.json")}
    changed = [row(index, description="UPDATED SOFTWARE") for index in range(3)]
    second, _ = run(tmp_path, Factory(changed))
    assert second["coverage_complete"]
    assert all(path.read_bytes() == content for path, content in before.items())
    assert len(list(tmp_path.rglob("attempt-*"))) == 2


def test_terminal_reconciliation_change_never_creates_complete_marker(tmp_path):
    stable = [row(index) for index in range(3)]
    changed = [row(index, description="UPDATED SOFTWARE") for index in range(3)]
    factory = Factory(lambda session: stable if session == 1 else changed)
    report, _ = run(tmp_path, factory)
    assert report["status"] == "partial" and not report["coverage_complete"]
    assert report["errors"][0]["code"] == "first_page_reconciliation_changed"
    assert not list(tmp_path.rglob("complete.private.json"))


def test_crash_after_last_page_without_complete_marker_reconciles_on_resume(tmp_path):
    rows = [row(index) for index in range(3)]
    # Verification alone fails, after BOTH immutable full pages were saved.
    factory = Factory(rows, **{})
    factory.options = lambda session: {"search_error": Prod2ProtocolError("Synthetic verifier failed")} if session == 2 else {}
    first, _ = run(tmp_path, factory)
    assert not first["coverage_complete"] and len(list(tmp_path.rglob("page-*.private.json"))) == 2
    resumed = Factory(rows)
    report, offered = run(tmp_path, resumed)
    assert report["coverage_complete"] and len(offered) == 3 and report["cached_pages_reemitted"] == 2
    assert report["http_pages"] == 0 and resumed.sessions == 2


def test_midnight_does_not_make_observation_timestamp_part_of_first_page_hash(tmp_path, monkeypatch):
    rows = [row(index) for index in range(3)]
    first, _ = run(tmp_path, Factory(rows, failure_page=2))
    assert not first["coverage_complete"]
    monkeypatch.setattr(sweep, "_now", lambda: NOW + timedelta(days=15))
    report, _ = run(tmp_path, Factory(rows))
    assert report["coverage_complete"] and len(list(tmp_path.rglob("attempt-*"))) == 1


@pytest.mark.parametrize("mutation", ["unverified", "filter", "counter", "origin", "rows", "object"])
def test_first_page_invalid_context_never_emits(tmp_path, mutation):
    def mutate(raw, number):
        if mutation == "unverified":
            raw["search_verified"] = False
        elif mutation == "filter":
            raw["snapshot"]["keyword"] = "WRONG"
        elif mutation == "counter":
            raw["counter"][0] = 2
        elif mutation == "origin":
            raw["counter_origin"] = "guessed"
        elif mutation == "rows":
            raw["rows"].pop()
        else:
            raw["rows"][0]["object"] = "Bien"
    report, offered = run(tmp_path, Factory([row(0)], mutate=mutate))
    assert not report["coverage_complete"] and report["errors"] and offered == []
    assert not list(tmp_path.rglob("page-*.private.json"))


def test_duplicate_identity_across_pages_is_a_partial_pass(tmp_path):
    rows = [row(0), row(1), row(0)]
    report, offered = run(tmp_path, Factory(rows))
    assert not report["coverage_complete"] and len(offered) == 2
    assert report["errors"][0]["code"] == "duplicate_id_across_pages"
    assert not list(tmp_path.rglob("complete.private.json"))


def test_changed_native_page_counter_cannot_complete(tmp_path):
    def mutate(raw, number):
        if number == 2:
            raw["snapshot"]["total_rows"] = 4
            raw["counter"][1:3] = [4, 4]
            raw["rows"].append(row(99))
    report, _ = run(tmp_path, Factory([row(index) for index in range(3)], mutate=mutate))
    assert not report["coverage_complete"] and report["errors"][0]["code"] == "counter_changed_during_pass"


@pytest.mark.parametrize("status", [403, 429])
def test_source_rejections_are_blocked_without_automatic_retry(tmp_path, status):
    factory = Factory([row(0)], search_error=Prod2ProtocolError(f"PROD2 HTTP status {status}"))
    report, offered = run(tmp_path, factory)
    assert report["status"] == "blocked" and not report["coverage_complete"] and offered == []
    assert factory.sessions == 1 and report["http_searches"] == 1
    assert report["errors"][0]["code"] == f"source_{status}"


def test_page_budget_does_not_claim_remaining_pages(tmp_path):
    report, offered = run(tmp_path, Factory([row(index) for index in range(5)]), max_pages=1)
    assert report["status"] == "partial" and not report["coverage_complete"] and len(offered) == 2
    assert report["errors"][0]["code"] == "page_budget_exhausted"
    assert not list(tmp_path.rglob("complete.private.json"))


def test_prior_exercise_year_with_current_publication_window_is_supported(tmp_path):
    report, offered = run(tmp_path, Factory([row(0)]), query={**QUERY, "year": 2025})
    assert report["coverage_complete"] and offered[0]["query"]["year"] == 2025
    assert offered[0]["row"]["published_at"].startswith("2026-")


def test_goods_are_independent_from_service_queries(tmp_path):
    report, offered = run(tmp_path, Factory([row(0, label="Bien")]), query={**QUERY, "object_type": "good"})
    assert report["coverage_complete"] and offered[0]["row"]["object_type"] == "good"


@pytest.mark.parametrize("result", [None, 1, "yes"])
def test_invalid_emit_result_never_marks_coverage(tmp_path, result):
    report, _ = run(tmp_path, Factory([row(0)]), emit=lambda item: result)
    assert not report["coverage_complete"] and report["errors"][0]["code"] == "emit_must_return_bool"


def test_emit_failure_stops_and_reports_without_logging_callback_message(tmp_path):
    def broken(item):
        raise RuntimeError("sensitive internal queue message")
    report, _ = run(tmp_path, Factory([row(0)]), emit=broken)
    assert not report["coverage_complete"] and report["errors"][0]["code"] == "emit_callback_error"
    assert "sensitive internal" not in json.dumps(report)


def test_cached_page_tampering_fails_before_emission_or_http(tmp_path):
    run(tmp_path, Factory([row(0)]))
    path = next(tmp_path.rglob("page-*.private.json"))
    cached = json.loads(path.read_text())
    cached["capture"]["rows"][0]["description"] = "TAMPERED"
    path.write_text(json.dumps(cached))
    report, offered = run(tmp_path, no_http)
    assert not report["coverage_complete"] and offered == []
    assert report["errors"][0]["code"] == "cached_page_binding_mismatch"


def test_frozen_root_manifest_tampering_is_a_reported_error(tmp_path):
    run(tmp_path, Factory([row(0)]))
    root = next(tmp_path.glob("sweep-*"))
    path = root / "manifest.private.json"
    cached = json.loads(path.read_text())
    cached["version"] = "unexpected-version"
    path.write_text(json.dumps(cached))
    report, offered = run(tmp_path, no_http)
    assert not report["coverage_complete"] and offered == []
    assert report["errors"][0]["code"] == "immutable_cache_conflict"


def test_missing_middle_cached_page_never_claims_complete(tmp_path):
    run(tmp_path, Factory([row(index) for index in range(5)]))
    next(tmp_path.rglob("page-000002.private.json")).unlink()
    report, offered = run(tmp_path, no_http)
    assert not report["coverage_complete"] and offered == []
    assert report["errors"][0]["code"] == "cached_page_gap"


def test_same_query_lock_blocks_duplicate_worker_without_source_calls(tmp_path):
    run(tmp_path, Factory([row(0)]))
    lock_path = next(tmp_path.rglob(".lock"))
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report, offered = run(tmp_path, no_http)
        assert report["status"] == "blocked" and offered == []
        assert report["errors"][0]["code"] == "query_owned_by_another_worker"
    finally:
        os.close(fd)


def test_cache_symlink_cannot_redirect_private_writes(tmp_path):
    run(tmp_path, Factory([row(0)]))
    root = next(tmp_path.glob("sweep-*"))
    windows = root / "windows"
    moved = tmp_path / "outside"
    windows.rename(moved)
    windows.symlink_to(moved, target_is_directory=True)
    report, offered = run(tmp_path, no_http)
    assert not report["coverage_complete"] and offered == []
    assert report["errors"][0]["code"] == "unsafe_private_cache"


@pytest.mark.parametrize("max_pages", [0, -1, True, 1.5])
def test_bad_budget_is_rejected_before_source(tmp_path, max_pages):
    with pytest.raises(ValueError):
        run(tmp_path, no_http, max_pages=max_pages)
