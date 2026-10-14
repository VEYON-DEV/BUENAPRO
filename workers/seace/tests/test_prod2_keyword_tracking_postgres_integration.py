"""Opt-in keyword tracking SQL in private schemas with unconditional rollback.

No source/model calls or public writes. Each schema includes its own migration
ledger, and search_path omits public. A second session only probes the query
lock or returns before touching a tracking table owned by the first session.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import os

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import keyword_tracking as subject, keyword_sweep, empty_scope_probe, repository
from buenapro_worker.prod2.batch_plan import digest
from buenapro_worker.prod2.runtime import production_settings
from test_prod2_keyword_sweep import Factory, row as source_row, no_http

pytestmark = pytest.mark.skipif(os.environ.get("BUENAPRO_PG_SCRATCH_TEST") != "1",
                                reason="Explicit SSH/PostgreSQL scratch opt-in required")
LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('prod2_keyword_query'),hashtext(%s)) AS acquired"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('prod2_keyword_query'),hashtext(%s)) AS unlocked"


@pytest.fixture
def scratch(tmp_path):
    root = Path(__file__).resolve().parents[3]
    state = SimpleNamespace(stage="tunnel", conn=None, observer=None,
        schema="prod2_keyword_scratch_" + uuid4().hex, sweep="scratch-" + uuid4().hex,
        migration=root / "db/migrations/0040_prod2_keyword_tracking.sql", folder=tmp_path)
    try:
        with production_settings(ssh_host="ubuntu@168.129.177.199",
                                 ssh_key=root / "credentials/ssh-key-2026-07-14.key") as settings:
            state.conn = psycopg.connect(settings.database_url, row_factory=dict_row)
            state.observer = psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)
            state.stage = "isolated_schema"
            state.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(state.schema)))
            state.conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(state.schema)))
            # Never let prepare_schema resolve a public migration ledger.
            state.conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            local = datetime.now(timezone.utc).astimezone(repository.LIMA)
            state.manifest = {"published_from": (local.date() - timedelta(days=180)).isoformat(),
                              "published_to": local.date().isoformat(), "keywords": ["software"], "years": [local.year]}
            state.year = local.year
            try:
                yield state
            finally:
                state.stage = "rollback"
                state.conn.rollback()
                # Session locks survive rollback; release only this test's sessions.
                state.conn.execute("SELECT pg_advisory_unlock_all()")
                assert state.conn.execute("SELECT to_regnamespace(%s) AS ns", (state.schema,)).fetchone()["ns"] is None
                state.conn.rollback()
                state.observer.execute("SELECT pg_advisory_unlock_all()")
    except Exception as exc:
        pytest.fail(f"Keyword tracking scratch failed at {state.stage}: {type(exc).__name__}; SQLSTATE={getattr(exc, 'sqlstate', None)}",
                    pytrace=False)
    finally:
        for conn in (state.observer, state.conn):
            if conn is not None:
                try:
                    conn.rollback()
                except psycopg.Error:
                    pass
                conn.close()


def query(state, keyword="software", *, year=None, kind="service"):
    return {"keyword": keyword, "object_type": kind, "year": state.year if year is None else year,
            "published_from": state.manifest["published_from"], "published_to": state.manifest["published_to"], "seace_version": "3"}


def native_result(q, *, status="complete", kind="observed_pass", observed="2026-10-06T12:00:00+00:00", **extra):
    return {"query": q, "status": status, "coverage_complete": status == "complete", "source_atomic_snapshot": False,
        "coverage_kind": kind, "errors": [] if status == "complete" else [{"code": "synthetic_source_pending"}],
        "pages_processed": 1, "http_searches": 1, "rows_offered": 0,
        "source_observed_at": observed, **extra}


def task_row(state, q):
    return state.conn.execute("SELECT * FROM prod2_keyword_tasks WHERE sweep_id=%s AND query_hash=%s",
                              (state.sweep, digest(q))).fetchone()


def zero_result(tracker, q):
    proof = empty_scope_probe.probe_empty_scope({**q, "keyword": ""},
        source_factory=Factory([]), folder=tracker.folder / "scope-probes")
    assert proof["scope_zero_verified"] is True
    return native_result(q, kind="verified_empty_scope_observed", pages_processed=0, http_searches=0,
        scope_proof_hash=proof["proof_hash"],
        source_observed_at=max(item["observed_at"] for item in proof["observations"]))


def observed_result(q, folder, *, replay=False):
    day = (datetime.fromisoformat(q["published_from"]) + timedelta(days=1)).date().isoformat()
    factory = no_http if replay else Factory([source_row(i, day=day) for i in range(2)])
    return keyword_sweep.sweep_query(q, source_factory=factory, emit=lambda item: False, folder=folder)


def prepare(state):
    result = subject.prepare_schema(state.conn, state.migration)
    assert result == {"status": "keyword_tracking_schema_ready", "migration": state.migration.name}
    resolved = state.conn.execute("""SELECT c.relname,n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.oid IN ('prod2_keyword_sweeps'::regclass,'prod2_keyword_tasks'::regclass,'schema_migrations'::regclass)""").fetchall()
    assert len(resolved) == 3 and {row["nspname"] for row in resolved} == {state.schema}
    assert state.conn.execute("""SELECT count(*) AS n FROM pg_constraint c
        JOIN pg_class own ON own.oid=c.conrelid JOIN pg_namespace ns ON ns.oid=own.relnamespace
        JOIN pg_class target ON target.oid=c.confrelid JOIN pg_namespace tn ON tn.oid=target.relnamespace
        WHERE ns.nspname=%s AND c.contype='f' AND tn.nspname<>%s""", (state.schema, state.schema)).fetchone()["n"] == 0


def test_prepare_idempotent_and_fixed_epoch_can_expand_keywords_years_and_objects(scratch):
    s = scratch
    s.stage = "prepare_idempotent"
    prepare(s)
    before = s.conn.execute("SELECT * FROM schema_migrations").fetchall()
    prepare(s)
    assert s.conn.execute("SELECT * FROM schema_migrations").fetchall() == before
    assert len(before) == 1 and before[0]["version"] == s.migration.name
    s.stage = "canonical_seed_replay_and_expansion"
    first = query(s)
    subject.seed_queries(s.conn, s.sweep, s.manifest, [first, first])
    subject.seed_queries(s.conn, s.sweep, s.manifest, [first])
    assert s.conn.execute("SELECT count(*) AS n FROM prod2_keyword_tasks").fetchone()["n"] == 1
    expanded = [query(s, keyword, year=year, kind=kind) for keyword in ("software", "chatbot")
                for year in (s.year, s.year - 1) for kind in ("good", "service")]
    manifest = {**s.manifest, "keywords": ["software", "chatbot"], "years": [s.year, s.year - 1], "profile_id": str(uuid4())}
    subject.seed_queries(s.conn, s.sweep, manifest, expanded)
    rows = s.conn.execute("SELECT * FROM prod2_keyword_tasks").fetchall()
    assert len(rows) == 8 and all(row["status"] == "pending" and row["attempts"] == 0 for row in rows)
    scope = s.conn.execute("SELECT * FROM prod2_keyword_sweeps").fetchone()
    assert scope["frozen_scope"] == subject.scope_for(s.manifest) == subject.scope_for(manifest)
    assert scope["scope_hash"] == digest(scope["frozen_scope"])
    progress = subject.read_progress(s.conn, s.sweep, ["software", "chatbot", "unused"])
    assert [row["queries_planned"] for row in progress] == [4, 4, 0]
    assert not progress[-1]["tracking_registered"] and all(not row["used"] for row in progress)
    s.stage = "epoch_dates_and_native_source_cannot_mutate"
    changed = {**s.manifest, "published_from": (datetime.fromisoformat(s.manifest["published_from"]) - timedelta(days=1)).date().isoformat()}
    with pytest.raises(ValueError, match="dates/policy changed"):
        subject.seed_queries(s.conn, s.sweep, changed, [{**first, "published_from": changed["published_from"]}])
    with pytest.raises(ValueError, match="frozen epoch"):
        subject.seed_queries(s.conn, s.sweep, s.manifest, [{**first, "published_from": changed["published_from"]}])
    with pytest.raises(ValueError, match="canonical"):
        subject.seed_queries(s.conn, s.sweep, s.manifest, [{**first, "keyword": "SOFTWARE"}])
    assert s.conn.execute("SELECT count(*) AS n FROM prod2_keyword_tasks").fetchone()["n"] == 8
    assert s.conn.execute("SELECT frozen_scope FROM prod2_keyword_sweeps").fetchone()["frozen_scope"] == scope["frozen_scope"]


def test_native_results_preserve_completion_replay_partial_and_cache_missing_status(scratch):
    s = scratch
    prepare(s)
    qs = [query(s, keyword, kind="good" if index == 0 else "service")
          for index, keyword in enumerate(("software", "chatbot", "redes", "licencias", "plc", "seguridad"))]
    subject.seed_queries(s.conn, s.sweep, s.manifest, qs)
    tracker = subject.KeywordTracker(lambda: s.conn, s.sweep, "synthetic-run", s.folder)
    s.stage = "run_and_verified_zero_completion"
    first = zero_result(tracker, qs[0])
    assert tracker.run(qs[0], lambda folder: first)["status"] == "complete"
    initial = task_row(s, qs[0])
    assert initial["status"] == "complete" and initial["attempts"] == 1
    assert initial["result_json"]["coverage_complete"] and initial["last_completed_at"] is not None
    assert initial["last_complete_json"] == initial["result_json"]
    s.stage = "observed_pass_replay_retains_source_time"
    complete = tracker.run(qs[1], lambda folder: observed_result(qs[1], folder))
    assert complete["coverage_complete"] and complete["rows_offered"] == 2
    observed = complete["source_observed_at"]
    cache = tracker.folder / digest(qs[1]) / ("sweep-" + digest(qs[1]))
    replay = tracker.run(qs[1], lambda folder: observed_result(qs[1], folder, replay=True))
    assert replay["http_searches"] == replay["http_pages"] == 0
    assert replay["cached_pages_reemitted"] == 1
    completed = task_row(s, qs[1])
    assert completed["attempts"] == 2 and completed["result_json"]["replayed_observation"]
    assert completed["result_json"]["source_observed_at"] == observed
    assert completed["last_complete_json"]["report_hash"] == digest(replay)
    s.stage = "missing_completed_native_cache_blocks_without_source"
    markers = list(cache.rglob("complete.private.json"))
    assert len(markers) == 1
    markers[0].unlink()  # Keep the root directory; its existence is insufficient.
    def must_not_run(folder):
        raise AssertionError("Completed source cache cannot be silently discarded")
    blocked = tracker.run(qs[1], must_not_run)
    assert blocked["status"] == "blocked"
    assert blocked["errors"] == [{"code": subject.CACHE_ERROR}]
    after = task_row(s, qs[1])
    assert after["status"] == "blocked" and after["attempts"] == 3
    assert after["last_complete_json"] == completed["last_complete_json"]
    assert after["last_completed_at"] == completed["last_completed_at"]
    before_guard = s.conn.execute("SELECT * FROM prod2_keyword_tasks ORDER BY query_hash").fetchall()
    errors = subject.completed_cache_errors(s.conn, s.sweep, s.folder, queries=[qs[0], qs[1]])
    assert len(errors) == 1 and errors[0]["query"] == qs[1] and errors[0]["errors"] == [{"code": subject.CACHE_ERROR}]
    assert s.conn.execute("SELECT * FROM prod2_keyword_tasks ORDER BY query_hash").fetchall() == before_guard
    s.stage = "partial_stopped_failed_and_blocked_are_not_complete"
    for q, state in ((qs[2], "partial"), (qs[3], "stopped"), (qs[4], "blocked")):
        tracker.run(q, lambda folder, q=q, state=state: native_result(q, status=state))
        row = task_row(s, q)
        assert row["status"] == ("partial" if state == "stopped" else state)
        assert row["attempts"] == 1 and not row["result_json"]["coverage_complete"]
        assert row["last_completed_at"] is None and row["last_complete_json"] is None
    def fail_source(folder):
        raise RuntimeError("Synthetic source failure")
    failed = tracker.run(qs[5], fail_source)
    assert failed["status"] == "partial" and failed["errors"] == [{"code": "query_execution_failed"}]
    assert task_row(s, qs[5])["result_json"]["error_codes"] == ["query_execution_failed"]
    s.stage = "progress_summary_uses_tracking_not_candidate_acceptance"
    progress = subject.read_progress(s.conn, s.sweep, [q["keyword"] for q in qs])
    assert all(row["used"] and row["tracking_registered"] for row in progress)
    assert [row["status"] for row in progress] == ["complete", "blocked", "partial", "partial", "blocked", "partial"]
    assert not any(row["queries_complete"] for row in progress[1:])
    tables = s.conn.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (s.schema,)).fetchall()
    assert {row["tablename"] for row in tables} == {"schema_migrations", "prod2_keyword_sweeps", "prod2_keyword_tasks"}


def test_query_session_ownership_blocks_another_worker_and_releases_on_failure(scratch):
    s = scratch
    prepare(s)
    q = query(s)
    subject.seed_queries(s.conn, s.sweep, s.manifest, [q])
    owner = subject.KeywordTracker(lambda: s.conn, s.sweep, "synthetic-owner", s.folder)
    rival = subject.KeywordTracker(lambda: s.observer, s.sweep, "synthetic-rival", s.folder)
    identity = s.sweep + ":" + digest(q)
    s.stage = "two_sessions_same_query_while_native_callback_active"
    def execute(folder):
        active = task_row(s, q)
        assert active["status"] == "running" and active["attempts"] == 1
        assert active["owner_run_id"] == "synthetic-owner" and active["claim_token"] is not None
        called = []
        denied = rival.run(q, lambda folder: called.append(folder))
        assert denied == {"query": q, "status": "blocked", "coverage_complete": False,
                          "source_atomic_snapshot": False, "errors": [{"code": "query_owned_by_another_worker"}]}
        assert called == []
        assert s.observer.execute(LOCK_SQL, (identity,)).fetchone()["acquired"] is False
        assert task_row(s, q)["attempts"] == 1  # Rival never touched private rows.
        return zero_result(owner, q)
    assert owner.run(q, execute)["status"] == "complete"
    assert s.observer.execute(LOCK_SQL, (identity,)).fetchone()["acquired"] is True
    s.stage = "external_owner_is_not_unlocked_by_blocked_worker"
    calls = []
    assert owner.run(q, lambda folder: calls.append(folder))["status"] == "blocked"
    assert calls == [] and task_row(s, q)["attempts"] == 1
    assert s.conn.execute(LOCK_SQL, (identity,)).fetchone()["acquired"] is False
    assert s.observer.execute(UNLOCK_SQL, (identity,)).fetchone()["unlocked"]
    s.stage = "invalid_native_result_never_completes_and_lock_is_released"
    invalid = owner.run(q, lambda folder: {**zero_result(owner, q), "source_atomic_snapshot": True})
    assert invalid["status"] == "blocked" and invalid["errors"] == [{"code": subject.CACHE_ERROR}]
    assert task_row(s, q)["status"] == "blocked"
    assert s.observer.execute(LOCK_SQL, (identity,)).fetchone()["acquired"] is True
    s.observer.execute(UNLOCK_SQL, (identity,))
    s.stage = "claim_token_compare_and_swap_prevents_foreign_completion"
    def replace_owner(folder):
        s.conn.execute("UPDATE prod2_keyword_tasks SET claim_token=%s WHERE sweep_id=%s AND query_hash=%s",
                       (str(uuid4()), s.sweep, digest(q)))
        return zero_result(owner, q)
    with pytest.raises(ValueError, match="owner changed"):
        owner.run(q, replace_owner)
    assert task_row(s, q)["status"] == "running"
    assert s.observer.execute(LOCK_SQL, (identity,)).fetchone()["acquired"] is True
    s.observer.execute(UNLOCK_SQL, (identity,))
    # Valid resume succeeds after the former worker releases its session lock.
    assert owner.run(q, lambda folder: zero_result(owner, q))["status"] == "complete"


def test_prepare_rejects_existing_incompatible_columns_or_ownership_constraints(scratch):
    s = scratch
    prepare(s)
    s.stage = "column_contract_rejection"
    s.conn.execute("ALTER TABLE prod2_keyword_tasks ADD COLUMN incompatible_extra TEXT")
    with pytest.raises(ValueError, match="incompatible columns"):
        subject.prepare_schema(s.conn, s.migration)
    assert s.conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == 1
    s.conn.execute("ALTER TABLE prod2_keyword_tasks DROP COLUMN incompatible_extra")
    prepare(s)
    s.stage = "ownership_constraint_contract_rejection"
    status_check = s.conn.execute("""SELECT conname FROM pg_constraint WHERE conrelid='prod2_keyword_tasks'::regclass
        AND contype='c' AND pg_get_constraintdef(oid) LIKE '%%pending%%'""").fetchone()["conname"]
    s.conn.execute(sql.SQL("ALTER TABLE prod2_keyword_tasks DROP CONSTRAINT {}").format(sql.Identifier(status_check)))
    with pytest.raises(ValueError, match="ownership constraints"):
        subject.prepare_schema(s.conn, s.migration)
    assert s.conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == 1
