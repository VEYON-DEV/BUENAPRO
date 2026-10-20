"""Exact selected metadata publication, real scratch PG and zero external calls."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from buenapro_worker.prod2 import selected_metadata_publication as publication

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def cases(tmp_path, monkeypatch):
    cases, candidates, inputs = [], [], []
    statuses = (["rejected_below_60"]*71 + ["closed_without_reading"]*57 + ["opening_not_confirmed"]*17
                + ["needs_review"]*8 + ["llm1_complete"]*3 + ["selection_needs_review"])
    for conv, status in enumerate(statuses, 1):
        listing = {"procedure_id": 1000+conv, "convocation_id": conv, "nomenclature": f"LP-{conv}-TEST",
            "buyer_name": "Entidad pública", "description": f"Compra de tablero eléctrico {conv}",
            "object_type": "good", "object_label": "Bien", "published_at": "2026-10-07T15:00:00-05:00",
            "reference_amount": "1000.00", "currency": "Soles", "seace_version": "3"}
        content = publication.digest(publication.registry_row(listing))
        outcome = {"convocation_id": conv, "procedure_id": 1000+conv, "description": listing["description"],
                   "object_type": "good", "status": status,
                   "accepted": status in {"needs_review", "llm1_complete", "selection_needs_review"}}
        path = tmp_path / str(conv) / "outcome.private.json"
        publication.immutable_write(path, outcome)
        checksum = publication.file_sha(path)
        inputs.append({"path": str(path), "sha256": checksum})
        cases.append({"listing": listing, "observed_at": "2026-10-20T07:00:00+00:00",
            "registry_content_hash": content, "reader_outcome_sha256": checksum,
            "reader_status": status, "accepted": outcome["accepted"]})
        candidates.append({"convocation_id": conv, "procedure_id": 1000+conv,
            "listing_content_hash": content, "title_sha256": hashlib.sha256(listing["description"].encode()).hexdigest()})
    selection_path, handoff_path = tmp_path / "selection.private.json", tmp_path / "handoff.private.json"
    publication.immutable_write(selection_path, {"scope_id": publication.SCOPE, "candidates": candidates})
    publication.immutable_write(handoff_path, {"candidate_ids_for_review": list(range(1, 158))})
    selection_sha, handoff_sha = publication.file_sha(selection_path), publication.file_sha(handoff_path)
    monkeypatch.setattr(publication, "SELECTION_SHA", selection_sha)
    monkeypatch.setattr(publication, "HANDOFF_SHA", handoff_sha)
    inputs += [{"path": str(selection_path), "sha256": selection_sha}, {"path": str(handoff_path), "sha256": handoff_sha}]
    return cases, inputs


@pytest.fixture
def pg():
    base = os.environ.get("PROD2_PUBLICATION_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Explicit isolated loopback scratch PG URL is required")
    if "127.0.0.1" not in base or not base.endswith("/commercial_taxonomy_qa"):
        pytest.fail("Publication integration tests require isolated loopback commercial_taxonomy_qa")
    schema = "selected_metadata_" + uuid.uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    dsn = make_conninfo(base, options=f"-c search_path={schema}")
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("""CREATE TABLE opportunities(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),record_kind TEXT NOT NULL,
            object_type TEXT NOT NULL,procurement_method TEXT NOT NULL,lifecycle_stage TEXT NOT NULL,
            participation_access TEXT NOT NULL,actionability TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE opportunity_sources(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            opportunity_id UUID NOT NULL REFERENCES opportunities(id),source_key TEXT NOT NULL,id_kind TEXT NOT NULL,
            external_id TEXT NOT NULL,native_values JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(source_key,id_kind,external_id));
            CREATE TABLE prod4_processes(id_procedimiento BIGINT PRIMARY KEY,id_convocatoria_pub BIGINT,
            opportunity_id UUID NOT NULL REFERENCES opportunities(id),object_type TEXT NOT NULL);
            CREATE TABLE prod2_listing_registry(convocation_id BIGINT PRIMARY KEY,procedure_id BIGINT NOT NULL,
            content_hash TEXT NOT NULL,listing_json JSONB NOT NULL,first_observed_at TIMESTAMPTZ NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL);
            CREATE TABLE prod2_details(id_convocatoria BIGINT PRIMARY KEY,raw_json JSONB NOT NULL);
            CREATE TABLE metadata_test_paid_evidence(id BIGINT PRIMARY KEY,raw_json JSONB NOT NULL)""")
        conn.execute((ROOT / "db/migrations/0035_prod2_discovery.sql").read_text())
    try:
        yield dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def conn_for(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def seed(conn, cases, existing=13):
    registry = [{"convocation_id": c["listing"]["convocation_id"], "procedure_id": c["listing"]["procedure_id"],
                 "content_hash": c["registry_content_hash"], "listing_json": publication.registry_row(c["listing"]),
                 "observed_at": c["observed_at"]} for c in cases]
    conn.execute("INSERT INTO prod2_listing_registry SELECT x.convocation_id,x.procedure_id,x.content_hash,x.listing_json,"
        "x.observed_at,x.observed_at FROM jsonb_to_recordset(%s) AS x(convocation_id BIGINT,procedure_id BIGINT,"
        "content_hash TEXT,listing_json JSONB,observed_at TIMESTAMPTZ)", (Jsonb(registry),))
    for case in cases[:existing]:
        row = case["listing"]
        canonical = str(uuid.uuid4())
        conn.execute("INSERT INTO opportunities(id,record_kind,object_type,procurement_method,lifecycle_stage,participation_access,actionability) "
            "VALUES(%s,'opportunity','good','selection_procedure','open','registration_required','actionable')", (canonical,))
        for kind, external in (("id_procedimiento", row["procedure_id"]), ("id_convocatoria", row["convocation_id"])):
            conn.execute("INSERT INTO opportunity_sources(opportunity_id,source_key,id_kind,external_id,native_values) "
                "VALUES(%s,'seace_prod2',%s,%s,%s)", (canonical, kind, str(external), Jsonb({"source_native_keep": True})))
        data = publication.discovery_row(case, canonical)
        # Reader metadata newer than the original map must remain byte-for-byte unchanged.
        data["last_seen_at"] = "2026-10-20T15:00:00+00:00"
        conn.execute("INSERT INTO prod2_discoveries SELECT x.* FROM jsonb_populate_record(NULL::prod2_discoveries,%s) x",
                     (Jsonb(data),))
        conn.execute("INSERT INTO prod2_details VALUES(%s,%s)", (row["convocation_id"], Jsonb({"official_ficha": "unaltered"})))
    conn.execute("INSERT INTO metadata_test_paid_evidence VALUES(1,%s)", (Jsonb({"paid_raw": "preserved"}),))


def plan_for(conn, cases):
    rows, inputs = cases
    return publication.build_plan(rows, publication.snapshot(conn, rows), inputs, "selected-metadata-test")


def test_pure_metadata_rows_preserve_source_instant_hash_and_no_fake_reading(cases):
    case = cases[0][0]
    row = publication.discovery_row(case, str(uuid.uuid4()))
    assert row["published_at"] == "2026-10-07T20:00:00+00:00"
    assert row["first_seen_at"] == row["last_seen_at"] == case["observed_at"]
    assert row["row_hash"] == publication.digest(case["listing"])
    assert not {"score", "llm1", "deadline", "schedule", "raw_extraction_json"} & set(row)


def test_pg_exact_157_atomic_publication_preserves_13_rows_and_paid_evidence(pg, cases):
    with conn_for(pg) as conn:
        seed(conn, cases[0])
        before = publication.snapshot(conn, cases[0])
        plan = plan_for(conn, cases)
        assert plan["new_discoveries"] == 144 and plan["preserved_discoveries"] == 13
        schema = publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")
        assert schema["applied"] and not publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")["applied"]
        result = publication.publish(conn, plan, publication.digest(plan))
        assert result["selected"] == 157 and result["created_discoveries"] == 144
        assert result["new_canonical_uuids"] == 144 and result["new_publication_markers"] == 157
        assert result["details_created"] == result["matches_created"] == result["source_calls"] == result["model_calls"] == 0
        after = publication.snapshot(conn, cases[0])
        assert len(after["discoveries"]) == len(after["publications"]) == 157
        for table, key in (("discoveries", "id_convocatoria"), ("sources", "id"), ("opportunities", "id")):
            indexed = {row[key]: row for row in after[table]}
            assert all(indexed[row[key]] == row for row in before[table])
        assert conn.execute("SELECT count(*) AS n FROM prod2_details").fetchone()["n"] == 13
        assert conn.execute("SELECT raw_json FROM metadata_test_paid_evidence").fetchone()["raw_json"] == {"paid_raw": "preserved"}
        assert conn.execute("SELECT count(*) AS n FROM opportunities WHERE lifecycle_stage='unknown' AND actionability='unknown'").fetchone()["n"] == 144
        assert conn.execute("SELECT reader_status,accepted FROM prod2_feed_publications WHERE id_convocatoria=1").fetchone() == {
            "reader_status": "rejected_below_60", "accepted": False}
        before_markers = after["publications"]
        replay = publication.publish(conn, plan, publication.digest(plan))
        assert replay["created_discoveries"] == replay["new_canonical_uuids"] == replay["new_publication_markers"] == 0
        assert replay["replayed_markers"] == 157
        assert publication.snapshot(conn, cases[0])["publications"] == before_markers


@pytest.mark.parametrize("change", ["registry", "existing_title", "canonical", "outcome_file", "scope"])
def test_pg_changed_frozen_source_or_identity_blocks_without_partial_publication(pg, cases, change):
    with conn_for(pg) as conn:
        seed(conn, cases[0])
        plan = plan_for(conn, cases)
        publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")
        if change == "registry":
            conn.execute("UPDATE prod2_listing_registry SET content_hash=%s WHERE convocation_id=157", ("f"*64,))
        elif change == "existing_title":
            conn.execute("UPDATE prod2_discoveries SET description='new title' WHERE id_convocatoria=1")
        elif change == "canonical":
            new = str(uuid.uuid4())
            conn.execute("INSERT INTO opportunities(id,record_kind,object_type,procurement_method,lifecycle_stage,participation_access,actionability) "
                "VALUES(%s,'opportunity','good','selection_procedure','unknown','unknown','unknown')", (new,))
            conn.execute("UPDATE opportunity_sources SET opportunity_id=%s WHERE id_kind='id_convocatoria' AND external_id='1'", (new,))
        elif change == "outcome_file":
            path = Path(plan["inputs"][0]["path"])
            path.write_text('{"changed":true}')
        else:
            plan["cases"][0]["listing"]["convocation_id"] = 999
        with pytest.raises(publication.PublicationError):
            publication.publish(conn, plan, publication.digest(plan))
        assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"] == 13
        assert conn.execute("SELECT count(*) AS n FROM prod2_feed_publications").fetchone()["n"] == 0


def test_pg_other_canonical_source_is_reused_without_new_uuid_or_native_mutation(pg, cases):
    with conn_for(pg) as conn:
        seed(conn, cases[0])
        canonical = str(uuid.uuid4())
        conn.execute("INSERT INTO opportunities(id,record_kind,object_type,procurement_method,lifecycle_stage,participation_access,actionability) "
            "VALUES(%s,'opportunity','good','selection_procedure','awarded','registration_required','informational')", (canonical,))
        conn.execute("INSERT INTO prod4_processes VALUES(1014,14,%s,'good')", (canonical,))
        conn.execute("INSERT INTO opportunity_sources(opportunity_id,source_key,id_kind,external_id,native_values) "
            "VALUES(%s,'seace_prod4','id_procedimiento','1014','{\"legacy\":true}')", (canonical,))
        plan = plan_for(conn, cases)
        publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")
        result = publication.publish(conn, plan, publication.digest(plan))
        assert result["new_canonical_uuids"] == 143
        assert str(conn.execute("SELECT opportunity_id FROM prod2_discoveries WHERE id_convocatoria=14").fetchone()["opportunity_id"]) == canonical
        assert conn.execute("SELECT lifecycle_stage,actionability FROM opportunities WHERE id=%s", (canonical,)).fetchone() == {
            "lifecycle_stage": "awarded", "actionability": "informational"}
        assert conn.execute("SELECT native_values FROM opportunity_sources WHERE source_key='seace_prod4'").fetchone()["native_values"] == {"legacy": True}


def test_pg_replay_sidecar_conflict_cannot_replace_or_publish_more_ids(pg, cases):
    with conn_for(pg) as conn:
        seed(conn, cases[0])
        plan = plan_for(conn, cases)
        publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")
        publication.publish(conn, plan, publication.digest(plan))
        conn.execute("UPDATE prod2_feed_publications SET publication_run_id='other_author' WHERE id_convocatoria=157")
        before = publication.frozen(conn.execute("SELECT * FROM prod2_feed_publications ORDER BY id_convocatoria").fetchall())
        with pytest.raises(publication.PublicationError, match="marker changed"):
            publication.publish(conn, plan, publication.digest(plan))
        assert publication.frozen(conn.execute("SELECT * FROM prod2_feed_publications ORDER BY id_convocatoria").fetchall()) == before


def test_pg_schema_fails_closed_on_registered_missing_table(pg, cases):
    with conn_for(pg) as conn:
        publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")
        conn.execute("DROP TABLE prod2_feed_publications")
        with pytest.raises(publication.PublicationError, match="disagree"):
            publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")


def test_pg_active_reader_executor_prevents_publication_without_wait_or_partial_rows(pg, cases):
    with conn_for(pg) as conn, conn_for(pg) as reader:
        seed(conn, cases[0])
        plan = plan_for(conn, cases)
        publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")
        with reader.transaction():
            reader.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_accepted_executor'),0)")
            with pytest.raises(publication.PublicationError, match="reader owns"):
                publication.publish(conn, plan, publication.digest(plan))
        assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"] == 13
        assert conn.execute("SELECT count(*) AS n FROM prod2_feed_publications").fetchone()["n"] == 0
