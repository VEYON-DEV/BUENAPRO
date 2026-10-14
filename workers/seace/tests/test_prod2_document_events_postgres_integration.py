"""Opt-in publication/resource SQL recovery in a rolled-back shadow schema.

No native portal/provider calls, no public writes or public sequence increments.
Backfill covers only the old observed row, preserving raw-detail hashes/downloads.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import repository, document_events
from buenapro_worker.prod2.client import SEARCH_URL
from buenapro_worker.prod2.runtime import production_settings
from test_prod2_batch_postgres_integration import _shadow_schema


@pytest.mark.skipif(os.environ.get("BUENAPRO_PG_SCRATCH_TEST") != "1", reason="Explicit scratch opt-in required")
def test_document_publication_backfill_replay_and_refresh_preserve_resources_downloads_and_legacy_hash():
    root = Path(__file__).resolve().parents[3]
    schema, conn, stage = "prod2_events_scratch_" + uuid4().hex, None, "tunnel"
    try:
        with production_settings(ssh_host="ubuntu@168.129.177.199",
                                 ssh_key=root / "credentials/ssh-key-2026-07-14.key") as settings:
            conn = psycopg.connect(settings.database_url, row_factory=dict_row)
            stage = "isolated_schema"
            public_sequences = _shadow_schema(conn, schema, root)
            conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            observed = datetime.now(timezone.utc)
            local = observed.astimezone(repository.LIMA)
            published = local.strftime("%d/%m/%Y %H:%M")
            conv, procedure, code = 9800000001, 8800000001, str(uuid4())
            row = {"procedure_id": procedure, "convocatoria_id": conv, "entity": "SYNTHETIC BUYER",
                   "published_at": published, "nomenclature": "SCRATCH-EVENTS", "object": "Servicio",
                   "description": "SYNTHETIC SOFTWARE", "amount_text": "100.00", "currency": "PEN", "seace_version": "3"}
            query = {"keyword": "SOFTWARE", "object_type": "service", "year": local.year,
                     "published_from": f"{local.year}-01-01", "published_to": local.date().isoformat(), "seace_version": "3"}
            repository.upsert_pilot_discovery(conn, row, query,
                batch_id="events-scratch-" + uuid4().hex, observed_at=observed)
            first = {"uuid": code, "filename": "Términos técnicos – integración.pdf", "seace_version": "3",
                     "stage": "Convocatoria", "type": "Bases administrativas", "published_at": published}
            detail = {**{key: row[key] for key in ("procedure_id", "convocatoria_id", "entity", "nomenclature", "object", "description")},
                      "seace_version": "3", "schedule": [], "item_states": [], "documents": [first],
                      "document_inventory_total": 1, "document_inventory_complete": True}
            repository.persist_detail(conn, detail, observed_at=observed, source_url=SEARCH_URL)
            manifest = {"document_uuid": code, "sha256": "a" * 64, "bytes": 1000,
                        "content_type": "application/pdf", "format": "pdf"}
            repository.record_document_download(conn, conv, manifest)
            old_detail = conn.execute("SELECT detail_hash,raw_detail FROM prod2_details").fetchone()
            old_download = conn.execute("SELECT * FROM prod2_document_downloads").fetchone()

            stage = "migration_backfill_of_legacy_resource"
            # Emulate the pre-0041 model inside this uncommitted scratch schema.
            conn.execute("DROP TABLE prod2_document_events")
            migration = root / "db/migrations" / document_events.MIGRATION
            assert document_events.prepare_schema(conn, migration)["status"] == "document_events_schema_ready"
            backfilled = conn.execute("SELECT * FROM prod2_document_events").fetchall()
            assert len(backfilled) == 1 and backfilled[0]["stage_name"] == "Convocatoria"
            prepared_first = repository.prepare_detail(detail)["documents"][0]
            assert backfilled[0]["event_hash"] == document_events.event_hash(prepared_first)
            assert backfilled[0]["first_seen_at"] == observed and backfilled[0]["last_seen_at"] == observed
            assert conn.execute("SELECT detail_hash,raw_detail FROM prod2_details").fetchone() == old_detail
            assert conn.execute("SELECT * FROM prod2_document_downloads").fetchone() == old_download
            assert document_events.prepare_schema(conn, migration)["status"] == "document_events_schema_ready"
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_events").fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == 1

            stage = "two_publications_one_resource_and_download_revision"
            second = {**first, "stage": "Otorgamiento de Buena Pro", "type": "Acta", "published_at": "06/10/2026 11:01"}
            expanded = {**detail, "document_events": [first, second], "document_inventory_total": 2}
            next_observed = observed + timedelta(seconds=1)
            result = repository.persist_detail(conn, expanded, observed_at=next_observed, source_url=SEARCH_URL)
            assert result["documents_count"] == 1 and result["document_events_count"] == 2
            assert conn.execute("SELECT count(*) AS n FROM prod2_documents").fetchone()["n"] == 1
            events = conn.execute("SELECT * FROM prod2_document_events ORDER BY first_seen_at,event_hash").fetchall()
            assert len(events) == 2 and all(event["is_listed"] for event in events)
            assert {event["event_hash"] for event in events} == {
                document_events.event_hash(item) for item in repository.prepare_detail(expanded)["document_events"]}
            saved = conn.execute("SELECT raw_detail,buena_pro_json FROM prod2_details").fetchone()
            assert len(saved["raw_detail"]["document_events"]) == 2
            assert saved["buena_pro_json"]["stage_documents_listed"] == [code]
            assert conn.execute("SELECT * FROM prod2_document_downloads").fetchone() == old_download

            stage = "partial_refresh_does_not_unlist_publications"
            partial = {**detail, "document_events": [first], "document_inventory_total": 2, "document_inventory_complete": False}
            repository.persist_detail(conn, partial, observed_at=observed + timedelta(seconds=2), source_url=SEARCH_URL)
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_events WHERE is_listed").fetchone()["n"] == 2

            stage = "complete_refresh_unlists_event_without_deleting_downloads"
            full = {**detail, "document_events": [first]}
            repository.persist_detail(conn, full, observed_at=observed + timedelta(seconds=3), source_url=SEARCH_URL)
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_events").fetchone()["n"] == 2
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_events WHERE is_listed").fetchone()["n"] == 1
            assert conn.execute("SELECT is_listed FROM prod2_documents").fetchone()["is_listed"] is True
            assert conn.execute("SELECT * FROM prod2_document_downloads").fetchone() == old_download

            stage = "ledger_replay_never_recreates_lost_table"
            conn.execute("DROP TABLE prod2_document_events")
            with pytest.raises(ValueError, match="incompatible columns"):
                document_events.prepare_schema(conn, migration)
            assert conn.execute("SELECT to_regclass('prod2_document_events') AS table").fetchone()["table"] is None
            for name, before in public_sequences.items():
                assert conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(sql.SQL(name))).fetchone() == before
            conn.rollback()
            assert conn.execute("SELECT to_regnamespace(%s) AS ns", (schema,)).fetchone()["ns"] is None
            conn.rollback()
    except Exception as exc:
        pytest.fail(f"Publication scratch failed at {stage}: {type(exc).__name__}; SQLSTATE={getattr(exc,'sqlstate',None)}", pytrace=False)
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except psycopg.Error:
                pass
            conn.close()
