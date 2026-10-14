"""Completed epoch evidence is inspected offline; no real DB/source/providers."""
from contextlib import nullcontext
from datetime import timedelta
import json
from pathlib import Path
import shutil

import pytest

from buenapro_worker.prod2 import empty_scope_probe as probe, keyword_sweep as sweep
from buenapro_worker.prod2.batch_plan import digest
from buenapro_worker.prod2.keyword_tracking import (
    CACHE_ERROR, KeywordTracker, completed_cache_errors, summarize_result,
)
from test_prod2_keyword_sweep import Factory, NOW, QUERY as RAW_QUERY, no_http, row

QUERY = {**RAW_QUERY, "keyword": "software"}
EPOCH = "synthetic-cache-epoch"


@pytest.fixture(autouse=True)
def clocks(monkeypatch):
    monkeypatch.setattr(sweep, "_now", lambda: NOW)
    monkeypatch.setattr(probe, "_now", lambda: NOW)


class Result:
    rowcount = 1
    def __init__(self, values):
        self.values = values
    def fetchone(self):
        return self.values[0] if self.values else None
    def fetchall(self):
        return self.values


class Connection:
    """Model only ownership/progress SQL; never connects to PostgreSQL."""
    def __init__(self, tasks):
        self.tasks, self.statements = tasks, []
    def transaction(self):
        return nullcontext()
    def close(self):
        pass
    def execute(self, sql, params=()):
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            return Result([{"acquired": True}])
        if "pg_advisory_unlock" in sql:
            return Result([{}])
        if sql.startswith("SELECT query_hash"):
            return Result([task for task in self.tasks if task.get("last_complete_json") is not None])
        task = next(task for task in self.tasks if task["query_hash"] == params[-1 if "status='running'" in sql else 1]) if sql.startswith("SELECT *") or "status='running'" in sql else self.tasks[0]
        if sql.startswith("SELECT *"):
            return Result([task])
        if "status='running'" in sql:
            task.update(status="running", claim_token=params[1], attempts=task["attempts"] + 1)
            return Result([])
        if sql.startswith("UPDATE prod2_keyword_tasks SET status=%s"):
            task.update(status=params[0], result_json=json.loads(params[1]))
            if params[2]:
                task["last_complete_json"] = json.loads(params[4])
            return Result([])
        raise AssertionError("Unexpected fake database statement")


def native_complete(cache, *, query=None, rows=None):
    query = query or QUERY
    path = cache / EPOCH / digest(query)
    report = sweep.sweep_query(query, source_factory=Factory(rows if rows is not None else [row(i) for i in range(5)]),
        emit=lambda item: True, folder=path)
    assert report["coverage_complete"]
    report.update(sweep.inspect_complete_cache(query, path))
    return summarize_result(query, report)


def task(summary, query=None):
    query = query or QUERY
    return {"query_hash": digest(query), "query_manifest": query, "status": "complete", "attempts": 1,
        "last_complete_json": summary, "result_json": summary}


def zero_complete(cache, query=None):
    query = query or QUERY
    proof = probe.probe_empty_scope({**query, "keyword": ""}, source_factory=Factory([]),
        folder=cache / EPOCH / "scope-probes")
    return summarize_result(query, {"query": query, "status": "complete", "coverage_complete": True,
        "coverage_kind": "verified_empty_scope_observed", "source_atomic_snapshot": False, "errors": [],
        "scope_proof_hash": proof["proof_hash"], "source_observed_at": proof["observed_at"]})


@pytest.mark.parametrize("mutation", ["root", "windows", "window", "page", "completion", "manifest", "attempt_manifest", "tamper_page", "tamper_completion"])
def test_missing_or_corrupt_native_completion_blocks_before_execute(tmp_path, mutation):
    summary = native_complete(tmp_path)
    root = tmp_path / EPOCH / digest(QUERY) / ("sweep-" + digest(QUERY))
    if mutation in {"root", "windows", "window"}:
        path = root if mutation == "root" else root / "windows" if mutation == "windows" else next((root / "windows").iterdir())
        shutil.rmtree(path)
    else:
        pattern = {"page": "page-000002.private.json", "completion": "complete.private.json",
            "manifest": "manifest.private.json", "attempt_manifest": "manifest.private.json",
            "tamper_page": "page-000001.private.json", "tamper_completion": "complete.private.json"}[mutation]
        path = (root / pattern if mutation == "manifest" else next(root.rglob("attempt-*")) / pattern
            if mutation == "attempt_manifest" else next(root.rglob(pattern)))
        if mutation.startswith("tamper"):
            saved = json.loads(path.read_text())
            if mutation == "tamper_page":
                saved["capture"]["rows"][0]["description"] = "CHANGED SYNTHETIC"
            else:
                saved["completed_at"] = "2026-10-06T16:59:59+00:00"
            path.write_text(json.dumps(saved))
        else:
            path.unlink()
    conn = Connection([task(summary)])
    assert completed_cache_errors(conn, EPOCH, tmp_path)[0]["errors"][0]["code"] == CACHE_ERROR
    tracker = KeywordTracker(lambda: conn, EPOCH, "synthetic-run", tmp_path)
    result = tracker.run(QUERY, lambda path: pytest.fail("Missing complete cache must never reach source execute"))
    assert result["status"] == "blocked" and conn.tasks[0]["status"] == "blocked"
    assert conn.tasks[0]["last_complete_json"] == summary


def test_valid_native_cache_and_replay_retain_hash_and_original_time(tmp_path):
    summary = native_complete(tmp_path)
    conn = Connection([task(summary)])
    assert completed_cache_errors(conn, EPOCH, tmp_path) == []
    assert all(sql.startswith("SELECT") for sql in conn.statements)
    tracker = KeywordTracker(lambda: conn, EPOCH, "new-run-same-epoch", tmp_path)
    result = tracker.run(QUERY, lambda path: sweep.sweep_query(QUERY, source_factory=no_http, emit=lambda item: False, folder=path))
    saved = conn.tasks[0]["last_complete_json"]
    assert result["coverage_complete"] and result["http_searches"] == 0
    assert saved["cache_evidence_hash"] == summary["cache_evidence_hash"]
    assert saved["source_observed_at"] == summary["source_observed_at"]
    assert saved["replayed_observation"]


@pytest.mark.parametrize("mutation", ["delete", "tamper", "hash", "timestamp"])
def test_missing_or_corrupt_zero_proof_blocks_before_probes(tmp_path, mutation):
    summary = zero_complete(tmp_path)
    proof_path = next((tmp_path / EPOCH / "scope-probes" / "empty-scopes").glob("*.private.json"))
    if mutation == "delete":
        proof_path.unlink()
    elif mutation == "tamper":
        saved = json.loads(proof_path.read_text())
        saved["scope_zero_verified"] = False
        proof_path.write_text(json.dumps(saved))
    elif mutation == "hash":
        summary["scope_proof_hash"] = "a" * 64
    else:
        summary["source_observed_at"] = "2026-10-06T16:59:59+00:00"
    conn = Connection([task(summary)])
    assert completed_cache_errors(conn, EPOCH, tmp_path, [QUERY])[0]["status"] == "blocked"
    tracker = KeywordTracker(lambda: conn, EPOCH, "synthetic-run", tmp_path)
    assert tracker.run(QUERY, lambda path: pytest.fail("Completed zero proof cannot be regenerated"))["status"] == "blocked"


def test_valid_zero_summary_preserves_exact_proof_hash_and_time(tmp_path):
    summary = zero_complete(tmp_path)
    conn = Connection([task(summary)])
    assert summary["scope_proof_hash"]
    assert completed_cache_errors(conn, EPOCH, tmp_path) == []
    tracker = KeywordTracker(lambda: conn, EPOCH, "new-run-same-epoch", tmp_path)
    result = tracker.run(QUERY, lambda path: {**summary, "errors": []})
    assert result["coverage_complete"]
    assert conn.tasks[0]["last_complete_json"]["scope_proof_hash"] == summary["scope_proof_hash"]
    assert conn.tasks[0]["last_complete_json"]["source_observed_at"] == summary["source_observed_at"]


def test_all_split_leaves_required_and_bound_to_same_completed_pass(tmp_path):
    rows = [row(i, "2026-10-01" if i < 250 else "2026-10-05") for i in range(500)]
    summary = native_complete(tmp_path, rows=rows)
    conn = Connection([task(summary)])
    assert completed_cache_errors(conn, EPOCH, tmp_path) == []
    path = next((tmp_path / EPOCH / digest(QUERY)).rglob("complete.private.json"))
    path.unlink()
    assert completed_cache_errors(conn, EPOCH, tmp_path)[0]["status"] == "blocked"


def test_new_epoch_has_no_old_completion_guard_and_partial_does_not_need_complete_marker(tmp_path):
    conn = Connection([task(native_complete(tmp_path))])
    assert completed_cache_errors(conn, EPOCH, tmp_path, [{**QUERY, "keyword": "chatbot"}]) == []
    # A read for another epoch is empty in real PG; simulate that independently.
    assert completed_cache_errors(Connection([]), "fresh-epoch", tmp_path) == []
    pending = task(None)
    pending.update(status="partial", result_json={"coverage_complete": False})
    assert completed_cache_errors(Connection([pending]), EPOCH, tmp_path) == []


def test_changed_evidence_even_if_still_valid_cannot_replace_epoch_completion(tmp_path):
    summary = native_complete(tmp_path)
    path = next((tmp_path / EPOCH / digest(QUERY)).rglob("complete.private.json"))
    saved = json.loads(path.read_text())
    saved["completed_at"] = "2026-10-06T17:00:01+00:00"
    path.write_text(json.dumps(saved))
    assert completed_cache_errors(Connection([task(summary)]), EPOCH, tmp_path)[0]["status"] == "blocked"


def test_fresh_completion_and_replay_use_identical_original_marker_timestamp(tmp_path, monkeypatch):
    ticks = iter(NOW + timedelta(microseconds=i) for i in range(10000))
    monkeypatch.setattr(sweep, "_now", lambda: next(ticks))
    path = tmp_path / EPOCH / digest(QUERY)
    first = sweep.sweep_query(QUERY, source_factory=Factory([row(0)]), emit=lambda item: True, folder=path)
    original = summarize_result(QUERY, first)["source_observed_at"]
    inspected = sweep.inspect_complete_cache(QUERY, path)
    second = sweep.sweep_query(QUERY, source_factory=no_http, emit=lambda item: False, folder=path)
    assert inspected["source_observed_at"] == original
    assert summarize_result(QUERY, second)["source_observed_at"] == original


def test_guard_remains_required_after_later_partial_or_blocked_replay(tmp_path):
    summary = native_complete(tmp_path)
    record = task(summary)
    record.update(status="partial", result_json={"coverage_complete": False})
    shutil.rmtree(tmp_path / EPOCH / digest(QUERY))
    assert completed_cache_errors(Connection([record]), EPOCH, tmp_path)[0]["status"] == "blocked"
