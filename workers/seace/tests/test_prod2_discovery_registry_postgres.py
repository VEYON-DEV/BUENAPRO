"""Opt-in real SQL contracts in private schemas of an explicit loopback QA DB.

No production settings, credentials, source, Redis, models or public schema are
used. The fixture rejects non-loopback/non-QA DSNs before connecting and drops
only its own schema after every test, including failures.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import discovery_registry as subject


DSN_ENV = "BUENAPRO_DISCOVERY_TEST_DATABASE_URL"
pytestmark = pytest.mark.skipif(not os.environ.get(DSN_ENV),
    reason="Explicit loopback discovery QA database URL required")
ROOT = Path(__file__).resolve().parents[3]
BASE_TABLES = ("prod2_discoveries", "prod2_accepted_run_members", "prod2_jev_decision_cache")
QUERY = {"keyword": "plc", "year": 2026, "object_type": "service",
    "published_from": "2026-10-01", "published_to": "2026-10-05", "seace_version": "3"}
T0, T1, T2 = (f"2026-10-{day:02d}T12:00:00+00:00" for day in (10, 11, 12))


def local_qa_dsn():
    try:
        parts = conninfo_to_dict(os.environ[DSN_ENV])
        valid = (set(parts) <= {"host", "port", "dbname", "user"}
            and parts.get("host") in {"127.0.0.1", "::1"}
            and parts.get("dbname", "").startswith("prod2_discovery_qa")
            and bool(parts.get("user")) and 1024 <= int(parts.get("port", "0")) <= 65535)
    except (KeyError, ValueError, psycopg.Error):
        valid = False
    if not valid:
        pytest.fail("Discovery SQL test requires an explicit loopback/port/QA database DSN without secrets", pytrace=False)
    return os.environ[DSN_ENV]


@pytest.fixture
def scratch():
    dsn = local_qa_dsn()
    schema = "prod2_map_qa_" + uuid4().hex
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5,
        application_name="buenapro-prod2-registry-qa",
        options="-c statement_timeout=15000 -c lock_timeout=10000")

    def connect():
        other = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5,
            application_name="buenapro-prod2-registry-qa-parallel",
            options="-c statement_timeout=15000 -c lock_timeout=10000")
        other.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return other

    try:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute("CREATE TABLE prod2_discoveries(id_convocatoria bigint PRIMARY KEY,id_procedimiento bigint NOT NULL)")
        conn.execute("CREATE TABLE prod2_accepted_run_members(convocation_id bigint NOT NULL)")
        conn.execute("CREATE TABLE prod2_jev_decision_cache(convocation_id bigint NOT NULL)")
        conn.execute((ROOT / "db/migrations/0045_prod2_discovery_registry.sql").read_text())
        yield SimpleNamespace(conn=conn, schema=schema, connect=connect)
    finally:
        try:
            conn.rollback()
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            assert conn.execute("SELECT to_regnamespace(%s) AS ns", (schema,)).fetchone()["ns"] is None
        finally:
            conn.close()


def freeze(conn, run_id, *, queries=None):
    manifest = {"run_id": run_id, "queries": queries or [QUERY, {**QUERY, "keyword": "scada"}],
        "policy": "synthetic-map-only-qa", "provider_calls": 0}
    subject.freeze_run(conn, run_id, manifest)
    return manifest


def entry(index, *, description="SYNTHETIC PLC", observed=T1, query=None):
    return {"row": {"convocation_id": 2000 + index, "procedure_id": 1000 + index,
        "nomenclature": f"QA-2026-{index}", "buyer_name": "SYNTHETIC BUYER", "description": description,
        "object_type": "service", "object_label": "Servicio", "published_at": "2026-10-05T12:30:00-05:00",
        "reference_amount": "1234.56", "currency": "Soles", "seace_version": "3"},
        "query": deepcopy(query or QUERY), "page_number": 1, "observed_at": observed}


def snapshot(conn):
    return {name: sorted(json.dumps(row, sort_keys=True, default=str) for row in conn.execute(
        sql.SQL("SELECT * FROM {}").format(sql.Identifier(name))).fetchall())
        for name in (*BASE_TABLES, *subject.TABLE_COLUMNS)}


def test_real_migration_schema_is_private_and_ddl_is_not_required_at_runtime(scratch):
    s = scratch
    ready = subject.require_schema(s.conn)
    assert ready["status"] == "discovery_registry_schema_ready"
    assert ready["tables"] == sorted(subject.TABLE_COLUMNS)
    assert ready["historical_baseline_tables"] == sorted(BASE_TABLES)
    before = snapshot(s.conn)
    assert subject.require_schema(s.conn)["status"] == "discovery_registry_schema_ready"
    assert snapshot(s.conn) == before
    relations = s.conn.execute("""SELECT n.nspname,c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='r'""",
        (s.schema,)).fetchall()
    assert {r["relname"] for r in relations} == set(BASE_TABLES) | set(subject.TABLE_COLUMNS)
    assert {r["nspname"] for r in relations} == {s.schema}
    foreign = s.conn.execute("""SELECT count(*) AS n FROM pg_constraint c
        JOIN pg_class own ON own.oid=c.conrelid JOIN pg_class target ON target.oid=c.confrelid
        WHERE own.relnamespace=%s::regnamespace AND c.contype='f'
          AND target.relnamespace<>own.relnamespace""", (s.schema,)).fetchone()["n"]
    assert foreign == 0


@pytest.mark.parametrize(("mutation", "code"), [
    ("ALTER TABLE prod2_listing_registry ALTER COLUMN content_hash DROP NOT NULL", "registry_schema_nullable_column"),
    ("ALTER TABLE prod2_map_run_members DROP CONSTRAINT prod2_map_run_members_identity_fk", "registry_schema_identity_constraint_incompatible"),
    ("ALTER TABLE prod2_map_run_members DROP CONSTRAINT prod2_map_run_members_class_ck", "registry_schema_check_constraint_incompatible"),
])
def test_real_catalog_rejects_nullable_or_missing_identity_and_classification_constraints(scratch, mutation, code):
    scratch.conn.execute(mutation)
    with pytest.raises(subject.RegistryError) as raised:
        subject.require_schema(scratch.conn)
    assert raised.value.code == code


def test_real_freeze_is_idempotent_and_rejects_changed_manifest_without_mutation(scratch):
    conn = scratch.conn
    manifest = freeze(conn, "freeze-qa")
    before = snapshot(conn)
    subject.freeze_run(conn, "freeze-qa", deepcopy(manifest))
    assert snapshot(conn) == before
    with pytest.raises(subject.RegistryError) as raised:
        subject.freeze_run(conn, "freeze-qa", {**manifest, "policy": "changed"})
    assert raised.value.code == "frozen_manifest_changed" and snapshot(conn) == before
    assert conn.info.transaction_status == TransactionStatus.IDLE


def test_real_bulk_classifies_all_four_states_and_replays_without_inflating_associations(scratch):
    conn = scratch.conn
    conn.execute("INSERT INTO prod2_discoveries VALUES (2001,1001)")
    conn.execute("INSERT INTO prod2_accepted_run_members VALUES (2002)")
    conn.execute("INSERT INTO prod2_jev_decision_cache VALUES (2003)")
    baseline = {name: rows for name, rows in snapshot(conn).items() if name in BASE_TABLES}
    freeze(conn, "older-qa")
    assert subject.observe_many(conn, "older-qa", [entry(4, description="OLD", observed=T0), entry(5, observed=T0)]) == [True, True]
    freeze(conn, "current-qa")
    entries = [entry(i, description="CHANGED" if i == 4 else "SYNTHETIC PLC") for i in range(6)]
    assert subject.observe_many(conn, "current-qa", entries) == [True] * 6
    first = subject.read_report(conn, "current-qa")
    assert first["counts"] == {"new_id": 1, "unchanged": 1, "changed": 1, "known_baseline_missing": 3}
    assert first["ids"] == {"new_id": [2000], "unchanged": [2005], "changed": [2004], "known_baseline_missing": [2001, 2002, 2003]}
    assert first["total_ids"] == first["observation_count"] == 6 and first["duplicate_associations"] == 0
    assert first["source_coverage_complete"] is False and first["provider_calls"] == first["published_opportunities"] == 0
    assert first["candidate_ids"] == [2000, 2001, 2002, 2003, 2004]
    assert first["needs_review_ids"] == [2001, 2002, 2003]
    assert first["requires_human_go"] is True and first["automatic_paid_authorization"] is False
    assert subject.observe_many(conn, "current-qa", entries) == [False] * 6
    assert subject.read_report(conn, "current-qa") == first
    other_query = {**QUERY, "keyword": "scada"}
    extra = [entry(i, description="CHANGED" if i == 4 else "SYNTHETIC PLC", query=other_query) for i in (0, 4)]
    assert subject.observe_many(conn, "current-qa", extra) == [False, False]
    final = subject.read_report(conn, "current-qa")
    assert final["counts"] == first["counts"] and final["total_ids"] == 6
    assert final["observation_count"] == 8 and final["duplicate_associations"] == 2
    assert {name: rows for name, rows in snapshot(conn).items() if name in BASE_TABLES} == baseline


def test_real_older_capture_is_preserved_without_replacing_latest_listing(scratch):
    conn = scratch.conn
    freeze(conn, "latest-qa")
    subject.observe_many(conn, "latest-qa", [entry(0, description="LATEST", observed=T2)])
    latest = conn.execute("SELECT * FROM prod2_listing_registry").fetchone()
    freeze(conn, "older-qa")
    subject.observe_many(conn, "older-qa", [entry(0, description="OLDER", observed=T0)])
    current = conn.execute("SELECT * FROM prod2_listing_registry").fetchone()
    assert current["content_hash"] == latest["content_hash"] and current["listing_json"] == latest["listing_json"]
    assert current["observed_at"] == latest["observed_at"] and current["first_observed_at"] < latest["first_observed_at"]
    report = subject.read_report(conn, "older-qa")
    assert report["ids"]["changed"] == [2000] and report["older_observations"] == 1
    assert report["observations"][0]["listing_json"]["description"] == "OLDER"
    assert report["members"][0]["latest_listing_json"]["description"] == "OLDER"
    assert conn.execute("SELECT count(*) AS n FROM prod2_listing_observations").fetchone()["n"] == 2
    freeze(conn, "equivalent-qa")
    equivalent = entry(0, description="LATEST", observed="2026-10-13T12:00:00+00:00", query={**QUERY, "keyword": "scada"})
    equivalent["row"]["published_at"] = "2026-10-05T17:30:00+00:00"
    equivalent["page_number"] = 2
    assert subject.observe_many(conn, "equivalent-qa", [equivalent]) == [True]
    assert subject.read_report(conn, "equivalent-qa")["ids"]["unchanged"] == [2000]


def test_real_change_within_same_run_preserves_both_listings_and_exports_candidate(scratch):
    conn = scratch.conn
    freeze(conn, "baseline-qa")
    subject.observe_many(conn, "baseline-qa", [entry(0, description="BASE", observed=T0)])
    freeze(conn, "change-during-qa")
    assert subject.observe_many(conn, "change-during-qa", [entry(0, description="BASE", observed=T1)]) == [True]
    initial = subject.read_report(conn, "change-during-qa")
    assert initial["ids"]["unchanged"] == [2000] and initial["candidate_ids"] == []
    assert subject.observe_many(conn, "change-during-qa", [entry(0, description="EDITED", observed=T2)]) == [False]
    final = subject.read_report(conn, "change-during-qa")
    assert final["ids"]["unchanged"] == [2000] and final["counts"]["changed"] == 0
    assert final["changed_during_run_ids"] == final["candidate_ids"] == [2000]
    assert final["members"][0]["observed_hash_count"] == 2
    assert final["members"][0]["latest_listing_json"]["description"] == "EDITED"
    assert [r["listing_json"]["description"] for r in final["observations"]] == ["BASE", "EDITED"]
    assert final["observation_count"] == 2 and final["duplicate_associations"] == 1
    assert final["automatic_paid_authorization"] is False and final["provider_calls"] == 0
    json.dumps(final, allow_nan=False)


def test_real_repeated_entry_in_one_batch_retains_first_association_without_inflation(scratch):
    conn = scratch.conn
    freeze(conn, "same-batch-qa")
    observed = entry(0)
    assert subject.observe_many(conn, "same-batch-qa", [observed, deepcopy(observed)]) == [True, False]
    first = subject.read_report(conn, "same-batch-qa")
    assert first["total_ids"] == first["observation_count"] == 1 and first["duplicate_associations"] == 0
    assert subject.observe_many(conn, "same-batch-qa", [observed, deepcopy(observed)]) == [False, False]
    assert subject.read_report(conn, "same-batch-qa") == first


@pytest.mark.parametrize("conflict", ["procedure", "timestamp", "query"])
def test_real_batch_conflicts_roll_back_every_listing_member_and_association(scratch, conflict):
    conn = scratch.conn
    freeze(conn, "atomic-qa")
    subject.observe_many(conn, "atomic-qa", [entry(0)])
    before = snapshot(conn)
    bad = entry(0)
    if conflict == "procedure":
        bad["row"]["procedure_id"] = 9999
        expected = "convocation_procedure_conflict"
    elif conflict == "timestamp":
        bad["row"]["description"] = "CONFLICT AT SAME INSTANT"
        expected = "observation_timestamp_conflict"
    else:
        bad["query"]["keyword"] = "not-frozen"
        expected = "query_not_frozen_or_ambiguous"
    with pytest.raises(subject.RegistryError) as raised:
        subject.observe_many(conn, "atomic-qa", [entry(1), bad])
    assert raised.value.code == expected and snapshot(conn) == before
    assert conn.info.transaction_status == TransactionStatus.IDLE


def test_real_leaf_query_is_bound_to_frozen_root_and_fk_rejects_other_root(scratch):
    conn = scratch.conn
    freeze(conn, "leaf-qa", queries=[QUERY])
    leaf = {**QUERY, "published_from": "2026-10-04"}
    assert subject.observe_many(conn, "leaf-qa", [entry(0, query=leaf)]) == [True]
    observed = subject.read_report(conn, "leaf-qa")["observations"][0]
    assert observed["root_query"] == QUERY and observed["observed_query"] == leaf
    assert observed["query_hash"] != observed["observed_query_hash"]
    before = snapshot(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute("UPDATE prod2_map_query_observations SET query_hash=%s", ("f" * 64,))
    assert snapshot(conn) == before


def test_ten_real_connections_serialize_overlapping_ids_without_double_membership(scratch):
    s = scratch
    queries = [{**QUERY, "keyword": f"plc-{index}"} for index in range(10)]
    freeze(s.conn, "parallel-qa", queries=queries)
    barrier = threading.Barrier(10, timeout=10)

    def worker(index):
        order = list(range(100)) if index % 2 else list(reversed(range(100)))
        entries = [entry(i, query=queries[index], observed=f"2026-10-11T12:00:{index:02d}+00:00") for i in order]
        with s.connect() as conn:
            barrier.wait()
            flags = subject.observe_many(conn, "parallel-qa", entries)
        return dict(zip((e["row"]["convocation_id"] for e in entries), flags))

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(worker, range(10)))
    assert all(len(flags) == 100 for flags in results)
    assert all(sum(flags[identity] for flags in results) == 1 for identity in range(2000, 2100))
    report = subject.read_report(s.conn, "parallel-qa")
    assert report["total_ids"] == report["counts"]["new_id"] == 100
    assert report["observation_count"] == 1000 and report["duplicate_associations"] == 900
    assert s.conn.execute("SELECT count(*) AS n FROM prod2_listing_registry").fetchone()["n"] == 100
    assert s.conn.execute("SELECT count(*) AS n FROM prod2_listing_observations").fetchone()["n"] == 1000
    assert report["source_coverage_complete"] is False and report["provider_calls"] == 0
