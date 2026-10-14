"""Empty-scope proofs use synthetic native sessions, never real HTTP."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import fcntl
import json
import os
import stat

import pytest

from buenapro_worker.prod2 import empty_scope_probe as probe
from buenapro_worker.prod2.client import Prod2ProtocolError
from test_prod2_keyword_sweep import Factory, NOW, QUERY, Source, no_http, row


EMPTY = {**QUERY, "keyword": ""}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(probe, "_now", lambda: NOW)


def run(tmp_path, factory, query=None):
    return probe.probe_empty_scope(query or EMPTY, source_factory=factory, folder=tmp_path)


def test_zero_requires_two_fresh_closed_sessions_and_exact_counter_proofs(tmp_path):
    factory = Factory([])
    result = run(tmp_path, factory)
    assert result["status"] == "zero_verified" and result["scope_zero_verified"] is True
    assert result["sessions_independent"] and factory.sessions == 2 and factory.active == 0
    assert len(result["observations"]) == 2
    assert all(item["counter"] == [0, 0, 0, 1, 1] for item in result["observations"])
    assert all(event[2]["allow_empty_keyword"] is True for event in factory.events)
    assert result["observations"][0]["snapshot_hash"] == result["observations"][1]["snapshot_hash"]
    assert not result["source_atomic_snapshot"]
    assert probe.scope_is_verified_zero(result, QUERY)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in tmp_path.rglob("*.json"))


def test_complete_frozen_scope_reuses_exact_proof_without_source_calls(tmp_path):
    result = run(tmp_path, Factory([]))
    assert run(tmp_path, no_http) == result
    assert len(list(tmp_path.rglob("*.private.json"))) == 1


@pytest.mark.parametrize("count", [1, 499, 500])
def test_nonempty_is_never_a_zero_optimization_even_at_portal_cap(tmp_path, count):
    factory = Factory([row(index) for index in range(count)], size=100)
    result = run(tmp_path, factory)
    assert not result["scope_zero_verified"] and result["status"] == "nonempty"
    assert factory.sessions == 1 and result["total_results"] == count
    assert not probe.scope_is_verified_zero(result, QUERY)
    assert run(tmp_path, no_http) == result


def test_prior_exercise_year_is_independent_of_publication_dates(tmp_path):
    query = {**EMPTY, "year": 2025}
    result = run(tmp_path, Factory([]), query)
    assert result["scope_zero_verified"]
    assert result["query"]["year"] == 2025 and result["query"]["published_from"].startswith("2026")
    assert probe.scope_is_verified_zero(result, {**query, "keyword": "IA"})


@pytest.mark.parametrize("mutation", ["year", "object_type", "published_from", "published_to", "seace_version"])
def test_proof_never_implies_another_year_object_window_or_version(tmp_path, mutation):
    result = run(tmp_path, Factory([]))
    changed = dict(QUERY)
    changed[mutation] = {"year": 2025, "object_type": "good", "published_from": "2026-09-30",
                        "published_to": "2026-10-04", "seace_version": "2"}[mutation]
    assert not probe.scope_is_verified_zero(result, changed)


def test_source_becoming_nonempty_between_sessions_is_failed_not_quiet_zero(tmp_path):
    factory = Factory(lambda session: [] if session == 1 else [row(0)])
    result = run(tmp_path, factory)
    assert result["status"] == "failed" and not result["scope_zero_verified"]
    assert result["errors"][0]["code"] == "scope_changed_between_sessions"
    assert len(result["observations"]) == 2 and result["total_results"] == 1
    assert not probe.scope_is_verified_zero(result, QUERY)


def test_even_two_zero_counters_require_stable_native_snapshots(tmp_path):
    factory = Factory([])
    factory.options = lambda session: {"size": 2 if session == 1 else 3}
    result = run(tmp_path, factory)
    assert not result["scope_zero_verified"]
    assert result["errors"][0]["code"] == "scope_snapshot_changed_between_sessions"


def test_reused_same_jsf_client_cannot_prove_independent_sessions(tmp_path):
    source = Source([], [])
    @contextmanager
    def reused():
        yield source
    result = run(tmp_path, reused)
    assert not result["scope_zero_verified"]
    assert result["errors"][0]["code"] == "independent_sessions_required"


def test_new_wrappers_with_shared_http_session_cannot_prove_independence(tmp_path):
    client = object()
    @contextmanager
    def wrappers():
        source = Source([], [])
        source.client = client
        yield source
    result = run(tmp_path, wrappers)
    assert not result["scope_zero_verified"]
    assert result["errors"][0]["code"] == "independent_sessions_required"


@pytest.mark.parametrize("mutation", ["verified", "origin", "year", "object", "keyword", "dates", "counter",
                                      "rows_missing", "rows_present", "page_size", "bool_counter"])
def test_missing_or_mismatched_native_context_never_authorizes_zero(tmp_path, mutation):
    def mutate(raw, number):
        if mutation == "verified":
            raw["search_verified"] = False
        elif mutation == "origin":
            raw["counter_origin"] = "derived_guess"
        elif mutation == "year":
            raw["snapshot"]["year"] = 2025
        elif mutation == "object":
            raw["snapshot"]["object_type"] = "Bien"
        elif mutation == "keyword":
            raw["snapshot"]["keyword"] = "SOFTWARE"
        elif mutation == "dates":
            raw["snapshot"]["published_from"] = "01/09/2026"
        elif mutation == "counter":
            raw["counter"] = [0, 0, 0, 1, 0]
        elif mutation == "rows_missing":
            raw["rows"] = None
        elif mutation == "rows_present":
            raw["rows"] = [row(0)]
        elif mutation == "page_size":
            raw["snapshot"]["page_size"] = 0
        else:
            raw["counter"][0] = False
    result = run(tmp_path, Factory([], mutate=mutate))
    assert result["status"] == "failed" and not result["scope_zero_verified"] and result["errors"]


def test_session_fields_are_not_persisted_in_whitelisted_snapshot(tmp_path):
    def mutate(raw, number):
        raw["snapshot"]["opaque_token"] = "synthetic-session-token"
        raw["cookie"] = "synthetic-cookie"
    result = run(tmp_path, Factory([], mutate=mutate))
    assert result["scope_zero_verified"]
    assert "synthetic-session-token" not in json.dumps(result)
    assert "synthetic-cookie" not in json.dumps(result)


@pytest.mark.parametrize("status", [403, 429])
def test_rejection_is_explicit_and_unpaid_probe_can_retry_on_next_command(tmp_path, status):
    result = run(tmp_path, Factory([], search_error=Prod2ProtocolError(f"PROD2 HTTP status {status}")))
    assert result["status"] == "blocked" and not result["scope_zero_verified"]
    assert result["errors"][0]["code"] == f"source_{status}"
    assert len(list(tmp_path.rglob("failed-*.private.json"))) == 1
    assert run(tmp_path, Factory([]))["scope_zero_verified"]


@pytest.mark.parametrize("mutation", ["hash", "query", "snapshot", "counter", "one_session", "independent",
    "bool_counter", "bool_row_count", "bool_total", "future_timestamp", "naive_timestamp",
    "errors", "coverage_kind", "atomic", "final_timestamp", "numeric_zero_flag"])
def test_mutated_or_incomplete_cached_proof_never_authorizes_omission(tmp_path, mutation):
    result = run(tmp_path, Factory([]))
    changed = deepcopy(result)
    if mutation == "hash":
        changed["proof_hash"] = "0" * 64
    elif mutation == "query":
        changed["query"]["year"] = 2025
    elif mutation == "snapshot":
        changed["observations"][0]["snapshot"]["year"] = 2025
    elif mutation == "counter":
        changed["observations"][0]["counter"] = [1, 1, 1, 1, 1]
    elif mutation == "one_session":
        changed["observations"].pop()
    elif mutation == "independent":
        changed["sessions_independent"] = False
    elif mutation == "bool_counter":
        changed["observations"][0]["counter"][0] = False
    elif mutation == "bool_row_count":
        changed["observations"][0]["row_count"] = False
    elif mutation == "bool_total":
        changed["total_results"] = False
    elif mutation == "future_timestamp":
        changed["observations"][0]["observed_at"] = (NOW + timedelta(minutes=6)).isoformat()
    elif mutation == "naive_timestamp":
        changed["observations"][0]["observed_at"] = NOW.replace(tzinfo=None).isoformat()
    elif mutation == "errors":
        changed["errors"] = [{"code": "partial_capture"}]
    elif mutation == "coverage_kind":
        changed["coverage_kind"] = "national_snapshot"
    elif mutation == "atomic":
        changed["source_atomic_snapshot"] = True
    elif mutation == "final_timestamp":
        changed["observed_at"] = (NOW + timedelta(minutes=1)).isoformat()
    else:
        changed["scope_zero_verified"] = 1
    # Recompute outer hash for structural mutations: nested binding checks must
    # still reject contradictions, not merely detect a checksum mismatch.
    if mutation != "hash":
        changed["proof_hash"] = probe._hash({key: value for key, value in changed.items() if key != "proof_hash"})
    assert not probe.scope_is_verified_zero(changed, QUERY)
    path = next(path for path in tmp_path.rglob("*.private.json") if not path.name.startswith("failed-"))
    path.write_text(json.dumps(changed))
    cached = run(tmp_path, no_http)
    assert not cached["scope_zero_verified"] and cached["status"] == "failed" and cached["errors"]


def test_invalid_empty_keyword_or_reversed_query_fails_before_source(tmp_path):
    for query in ({**QUERY}, {**EMPTY, "keyword": " "}, {**EMPTY, "published_from": "2026-10-06"}):
        with pytest.raises(ValueError):
            run(tmp_path, no_http, query)


def test_probe_scope_lock_prevents_duplicate_unpaid_requests(tmp_path):
    run(tmp_path, Factory([], search_error=Prod2ProtocolError("Synthetic failure")))
    path = next(tmp_path.rglob("*.lock"))
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(tmp_path, no_http)
        assert not result["scope_zero_verified"] and result["status"] == "blocked"
        assert result["errors"][0]["code"] == "scope_owned_by_another_worker"
    finally:
        os.close(fd)
