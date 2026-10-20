"""Loopback-only PostgreSQL driver QA with synthetic native search sessions.

Opt in with BUENAPRO_DISCOVERY_TEST_DATABASE_URL. Every test owns and removes
one schema. No production connection, source HTTP, paid model or Redis is used.
"""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import os
import threading
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import parallel_discovery as driver
from buenapro_worker.prod2.discovery_transport import SourceGate
from test_prod2_keyword_sweep import Source, row


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def scratch():
    dsn = os.environ.get("BUENAPRO_DISCOVERY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Explicit loopback PostgreSQL discovery QA URL required")
    parsed = urlsplit(dsn)
    if (parsed.scheme not in {"postgres", "postgresql"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path != "/prod2_discovery_qa"):
        pytest.fail("Discovery driver QA requires its loopback scratch database", pytrace=False)
    schema, connections = "parallel_driver_" + uuid4().hex, []
    admin = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row,
                               options=f"-c search_path={schema} -c statement_timeout=10000")
        connections.append(conn)
        return conn

    try:
        with connect() as conn:
            conn.execute("CREATE TABLE prod2_discoveries(id_convocatoria bigint,id_procedimiento bigint)")
            conn.execute("CREATE TABLE prod2_accepted_run_members(convocation_id bigint)")
            conn.execute("CREATE TABLE prod2_jev_decision_cache(convocation_id bigint)")
            for migration in ["0040_prod2_keyword_tracking.sql", "0045_prod2_discovery_registry.sql"]:
                conn.execute((ROOT / "db/migrations" / migration).read_text())
        yield SimpleNamespace(connect=connect, schema=schema)
    finally:
        for conn in connections:
            conn.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def plan(run_id, workers=10):
    keywords = ["software", "cloud", "licencias", "servidor", "datos", "soporte"]
    return {"run_id": run_id, "version": driver.VERSION, "mode": "metadata_only",
        "published_from": "2026-10-01", "published_to": "2026-10-05", "years": [2026],
        "objects": ["good", "service"], "keywords": keywords,
        "queries": driver.queries_for(keywords, [2026], "2026-10-01", "2026-10-05"),
        "request_interval_seconds": 1, "max_http_requests": 2000, "max_runtime_seconds": 900,
        "max_workers": workers, "max_pages_per_query": 100,
        "ramp": {"pilot_queries": 1, "warmup_queries": 8, "warmup_workers": min(4, workers)}}


class SyntheticGate(SourceGate):
    """Real owner/pacing hooks, synthetic parsed source responses only."""

    def __init__(self, *, changed=False, forbidden=False, **kwargs):
        self.now, self.changed, self.forbidden = 0., changed, forbidden
        self.sessions, self.active, self.peak = 0, 0, 0
        self.events, self.session_lock = [], threading.Lock()
        super().__init__(**kwargs, clock=lambda: self.now, sleep=self._advance)

    def _advance(self, seconds):
        self.now += seconds

    def _synthetic_request(self):
        self.hooks()["request"][0](httpx.Request("POST", "https://prod2.seace.gob.pe/synthetic"))
        self.hooks()["response"][0](httpx.Response(200))

    @contextmanager
    def source_factory(self):
        if self.forbidden:
            raise AssertionError("Completed cache must not open a source session")
        with self.session_lock:
            self.sessions += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        gate = self

        class Native(Source):
            def search(self, keyword, **args):
                gate._synthetic_request()
                good = args["object_type"] == "Bien"
                base, label = (100, "Bien") if good else (200, "Servicio")
                self.all_rows = [row(base + i, label=label) for i in range(3)]
                if gate.changed and good:
                    self.all_rows[0]["description"] = "SOFTWARE VERSION CHANGED"
                return super().search(keyword, **args)

            def search_page(self, number):
                gate._synthetic_request()
                return super().search_page(number)

        try:
            yield Native([], self.events, size=2)
        finally:
            with self.session_lock:
                self.active -= 1


def gates(*, changed=False, forbidden=False):
    instances = []

    def make(**kwargs):
        gate = SyntheticGate(changed=changed, forbidden=forbidden, **kwargs)
        instances.append(gate)
        return gate

    return make, instances


@pytest.mark.parametrize("workers", [1, 4, 10])
def test_driver_1_4_10_workers_persists_each_id_once_and_every_query(scratch, tmp_path, workers):
    requested = plan(f"driver-workers-{workers}", workers)
    factory, instances = gates()
    report = driver.execute_mapping(requested, tmp_path / "mapping", scratch.connect, factory)
    assert report["source_coverage_complete"] and report["queries_complete"] == 12
    assert report["metadata"]["counts"]["new_id"] == 6
    assert report["metadata"]["total_ids"] == 6
    assert report["metadata"]["observation_count"] == 36
    assert report["model_calls"] == report["document_downloads"] == report["redis_mutations"] == 0
    assert report["published_opportunities"] == 0
    assert len(instances) == 1 and instances[0].active == 0
    assert instances[0].peak <= workers
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM prod2_listing_registry").fetchone()["n"] == 6
        assert conn.execute("SELECT count(*) AS n FROM prod2_map_run_members").fetchone()["n"] == 6
        assert conn.execute("SELECT count(*) AS n FROM prod2_keyword_tasks WHERE status='complete'").fetchone()["n"] == 12


def test_driver_cross_run_classifies_unchanged_and_changed_without_paid_work(scratch, tmp_path):
    first, _ = gates()
    driver.execute_mapping(plan("driver-first"), tmp_path / "first", scratch.connect, first)
    same, _ = gates()
    second = driver.execute_mapping(plan("driver-second"), tmp_path / "second", scratch.connect, same)
    assert second["metadata"]["counts"] == {
        "new_id": 0, "unchanged": 6, "changed": 0, "known_baseline_missing": 0}
    altered, _ = gates(changed=True)
    third = driver.execute_mapping(plan("driver-third"), tmp_path / "third", scratch.connect, altered)
    assert third["metadata"]["counts"] == {
        "new_id": 0, "unchanged": 5, "changed": 1, "known_baseline_missing": 0}
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM prod2_listing_registry").fetchone()["n"] == 6
        assert conn.execute("SELECT count(*) AS n FROM prod2_map_run_members").fetchone()["n"] == 18


def test_existing_selected_coordinator_lock_prevents_all_source_sessions(scratch, tmp_path):
    factory, instances = gates()
    with scratch.connect() as owner:
        assert owner.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]
        with pytest.raises(driver.DiscoveryError, match="another_prod2_coordinator_active"):
            driver.execute_mapping(plan("driver-busy"), tmp_path / "mapping", scratch.connect, factory)
    assert instances == []
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM prod2_map_runs").fetchone()["n"] == 0


def test_resumed_complete_cached_observation_opens_no_source_sessions(scratch, tmp_path):
    requested, folder = plan("driver-replay"), tmp_path / "mapping"
    first, _ = gates()
    initial = driver.execute_mapping(requested, folder, scratch.connect, first)
    before = {path: path.read_bytes() for path in folder.rglob("page-*.private.json")}
    forbidden, instances = gates(forbidden=True)
    replay = driver.execute_mapping(requested, folder, scratch.connect, forbidden)
    assert initial["source_coverage_complete"] and replay["source_coverage_complete"]
    assert replay["metadata"]["total_ids"] == 6
    assert replay["metadata"]["observation_count"] == 36
    assert replay["http"]["requests"] == 0 and instances[0].sessions == 0
    assert all(path.read_bytes() == data for path, data in before.items())


def test_completed_cache_tamper_blocks_before_gate_or_source(scratch, tmp_path):
    requested, folder = plan("driver-tamper"), tmp_path / "mapping"
    first, _ = gates()
    driver.execute_mapping(requested, folder, scratch.connect, first)
    page = next(folder.rglob("page-*.private.json"))
    page.write_text("{}")
    forbidden, instances = gates(forbidden=True)
    with pytest.raises(driver.DiscoveryError, match="completed_cache_missing_no_source_replay"):
        driver.execute_mapping(requested, folder, scratch.connect, forbidden)
    assert instances == []


def test_bigint_lookalike_lock_cannot_replace_the_selected_two_integer_lock(scratch):
    stop = threading.Event()
    with scratch.connect() as conn, driver.coordinator(conn, stop) as owner:
        owner.check()
        conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")
        conn.execute("SELECT pg_advisory_lock(hashtext('prod2_accepted_executor')::bigint << 32)")
        with pytest.raises(driver.DiscoveryError, match="coordinator_lock_lost"):
            owner.check()
        assert stop.is_set()


def test_metadata_commit_failure_stops_after_first_page_and_preserves_inputs(scratch, tmp_path, monkeypatch):
    from buenapro_worker.prod2 import discovery_registry

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("Synthetic database failure; private diagnostic")

    monkeypatch.setattr(discovery_registry, "observe_many", unavailable)
    factory, instances = gates()
    report = driver.execute_mapping(plan("driver-pg-failure", workers=1),
                                    tmp_path / "mapping", scratch.connect, factory)
    assert not report["source_coverage_complete"] and report["queries_complete"] == 0
    assert report["metadata"]["total_ids"] == 0
    assert instances[0].stop.is_set()
    assert instances[0].metrics()["requests"] == 3  # Two nonempty probes, first search.
    assert len(list((tmp_path / "mapping").rglob("page-*.private.json"))) == 1
    assert not list((tmp_path / "mapping").rglob("complete.private.json"))
    with scratch.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM prod2_keyword_tasks WHERE status='pending'").fetchone()["n"] == 11


def schema_snapshot(conn):
    """Catalog/row proof confined to the current test schema."""
    rows = conn.execute("""SELECT c.relname,c.oid,c.relfilenode FROM pg_class c
        WHERE c.relnamespace=current_schema()::regnamespace AND c.relkind IN ('r','i')
        ORDER BY c.relname""").fetchall()
    tables = conn.execute("""SELECT c.relname FROM pg_class c
        WHERE c.relnamespace=current_schema()::regnamespace AND c.relkind='r'
        ORDER BY c.relname""").fetchall()
    return rows, {row["relname"]: conn.execute(sql.SQL("SELECT count(*) AS n FROM {}")
        .format(sql.Identifier(row["relname"]))).fetchone()["n"] for row in tables}


def drop_only_registry_tables(conn):
    from buenapro_worker.prod2.discovery_registry import TABLE_COLUMNS
    conn.execute(sql.SQL("DROP TABLE {} CASCADE").format(
        sql.SQL(",").join(sql.Identifier(name) for name in sorted(TABLE_COLUMNS))))


def test_explicit_schema_apply_already_ready_preserves_catalog_and_rows(scratch, monkeypatch):
    from buenapro_worker.prod2.discovery_registry import freeze_run
    monkeypatch.setattr(SourceGate, "source_factory", lambda *a: pytest.fail("schema mode must not use HTTP"))
    with scratch.connect() as conn:
        freeze_run(conn, "prepared-existing", plan("prepared-existing"))
        before = schema_snapshot(conn)
        result = driver.apply_reviewed_schema(conn, ROOT)
        assert result == {"status": "already_ready", "migration": "0045_prod2_discovery_registry.sql",
                          "source_calls": 0, "model_calls": 0}
        assert schema_snapshot(conn) == before
        assert conn.execute("SELECT to_regclass('schema_migrations') AS ledger").fetchone()["ledger"] is None


def test_explicit_schema_apply_creates_only_six_tables_and_one_ledger_record(scratch, monkeypatch):
    from buenapro_worker.prod2.discovery_registry import TABLE_COLUMNS, require_schema
    monkeypatch.setattr(SourceGate, "source_factory", lambda *a: pytest.fail("schema mode must not use HTTP"))
    with scratch.connect() as conn:
        drop_only_registry_tables(conn)
        conn.execute("CREATE TABLE schema_migrations(version text PRIMARY KEY)")
        before_tables = set(schema_snapshot(conn)[1])
        result = driver.apply_reviewed_schema(conn, ROOT)
        assert result == {"status": "schema_prepared", "migration": "0045_prod2_discovery_registry.sql",
                          "source_calls": 0, "model_calls": 0}
        require_schema(conn)
        assert set(schema_snapshot(conn)[1]) - before_tables == set(TABLE_COLUMNS)
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [
            {"version": "0045_prod2_discovery_registry.sql"}]
        before_repeat = schema_snapshot(conn)
        assert driver.apply_reviewed_schema(conn, ROOT)["status"] == "already_ready"
        assert schema_snapshot(conn) == before_repeat


@pytest.mark.parametrize("blocked_by", ["missing_ledger", "selected_coordinator"])
def test_explicit_schema_apply_does_no_ddl_when_readiness_or_ownership_fails(scratch, monkeypatch, blocked_by):
    monkeypatch.setattr(SourceGate, "source_factory", lambda *a: pytest.fail("schema mode must not use HTTP"))
    with scratch.connect() as conn, scratch.connect() as owner:
        drop_only_registry_tables(conn)
        if blocked_by == "selected_coordinator":
            conn.execute("CREATE TABLE schema_migrations(version text PRIMARY KEY)")
            assert owner.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]
        before = schema_snapshot(conn)
        expected = ("existing_migration_ledger_required" if blocked_by == "missing_ledger"
                    else "another_prod2_coordinator_active")
        with pytest.raises(driver.DiscoveryError, match=expected):
            driver.apply_reviewed_schema(conn, ROOT)
        assert schema_snapshot(conn) == before


def test_existing_baseline_index_name_on_wrong_table_and_column_is_rejected_not_repaired(scratch):
    from buenapro_worker.prod2.discovery_registry import RegistryError, require_schema
    with scratch.connect() as conn:
        conn.execute("DROP INDEX prod2_jev_decision_cache_convocation_idx")
        conn.execute("""CREATE INDEX prod2_jev_decision_cache_convocation_idx
            ON prod2_discoveries(id_procedimiento)""")
        before = schema_snapshot(conn)
        definition = conn.execute("""SELECT pg_get_indexdef(
            'prod2_jev_decision_cache_convocation_idx'::regclass) AS definition""").fetchone()
        with pytest.raises(RegistryError, match="^historical_baseline_index_missing_or_incompatible$"):
            require_schema(conn)
        with pytest.raises(RegistryError, match="^historical_baseline_index_missing_or_incompatible$"):
            driver.apply_reviewed_schema(conn, ROOT)
        assert schema_snapshot(conn) == before
        assert conn.execute("""SELECT pg_get_indexdef(
            'prod2_jev_decision_cache_convocation_idx'::regclass) AS definition""").fetchone() == definition
