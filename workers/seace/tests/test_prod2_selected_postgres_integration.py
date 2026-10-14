"""Opt-in selected scope SQL contract in an unconditionally rolled-back shadow.

No portal/provider calls or public-table writes. A second session checks the
shared advisory lock; its only operations are lock probes followed by rollback.
All receipt/cache/facet writes stay in the first session's uncommitted schema.
"""
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import document_selection, repository, selected_repository
from buenapro_worker.prod2.client import SEARCH_URL
from buenapro_worker.prod2.llm1_completion import persist_completed_llm1, Llm1CompletionError
from buenapro_worker.prod2.runtime import production_settings
from test_prod2_batch_postgres_integration import BASE_TABLES, _shadow_schema
from test_prod2_selected_completion import selected_bundle, DESCRIPTION, CODE, OTHER_CODE, SHA

ENABLED = os.environ.get("BUENAPRO_PG_SCRATCH_TEST") == "1"


def _public_invariants(conn):
    """Only counts/sequence state: no public payload or personal-data reads."""
    tables = [row["table_name"] for row in conn.execute("""SELECT table_name
        FROM information_schema.tables WHERE table_schema='public'
        AND table_type='BASE TABLE' AND (table_name=ANY(%s) OR starts_with(table_name,'prod2_'))
        ORDER BY table_name""", (list(BASE_TABLES),)).fetchall()]
    counts = {name: conn.execute(sql.SQL("SELECT count(*) AS n FROM public.{}").format(
        sql.Identifier(name))).fetchone()["n"] for name in tables}
    sequences = {}
    for row in conn.execute("""SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=ANY(%s)
        AND (column_default LIKE 'nextval(%%' OR is_identity='YES')""", (tables,)).fetchall():
        name = conn.execute("SELECT pg_get_serial_sequence(%s,%s) AS name",
                            ("public." + row["table_name"], row["column_name"])).fetchone()["name"]
        if name:
            sequences[name] = conn.execute(sql.SQL("SELECT last_value,is_called FROM {}")
                                           .format(sql.SQL(name))).fetchone()
    return {"counts": counts, "sequences": sequences}


@pytest.mark.skipif(not ENABLED, reason="Explicit SSH/PostgreSQL scratch opt-in required")
@pytest.mark.parametrize("policy_version", [document_selection.DOCUMENT_SELECTION_POLICY,
                                          document_selection.DOCUMENT_SELECTION_POLICY_V2,
                                          document_selection.DOCUMENT_SELECTION_POLICY_V3], ids=["v1", "v2", "v3"])
def test_selected_paid_completion_global_selector_claims_and_revision_in_rolled_back_pg(policy_version):
    root = Path(__file__).resolve().parents[3]
    schema, conv, procedure = "prod2_selected_scratch_" + uuid4().hex, 9600000001, 8600000001
    conn, observer, stage = None, None, "tunnel"
    try:
        with production_settings(ssh_host="ubuntu@168.129.177.199",
                                 ssh_key=root / "credentials/ssh-key-2026-07-14.key") as settings:
            conn = psycopg.connect(settings.database_url, row_factory=dict_row)
            observer = psycopg.connect(settings.database_url, row_factory=dict_row)
            before_public = _public_invariants(conn)
            stage = "isolated_schema"
            public_sequences = _shadow_schema(conn, schema, root)
            conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            migration = root / "db/migrations/0039_prod2_document_selection_cache.sql"
            assert selected_repository.prepare_schema(conn, migration)["status"] == "selector_schema_ready"
            assert selected_repository.prepare_schema(conn, migration)["status"] == "selector_schema_ready"
            assert conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == 1
            assert conn.execute("""SELECT count(*) AS n FROM pg_constraint c
                JOIN pg_class own ON own.oid=c.conrelid JOIN pg_namespace ns ON ns.oid=own.relnamespace
                JOIN pg_class target ON target.oid=c.confrelid JOIN pg_namespace tn ON tn.oid=target.relnamespace
                WHERE ns.nspname=%s AND c.contype='f' AND tn.nspname<>%s""", (schema, schema)).fetchone()["n"] == 0
            observed = datetime.now(timezone.utc)
            local = observed.astimezone(repository.LIMA)
            published = local.strftime("%d/%m/%Y %H:%M")
            row = {"procedure_id": procedure, "convocatoria_id": conv, "entity": "SYNTHETIC BUYER",
                "published_at": published, "nomenclature": "SCRATCH-SELECTED", "object": "Servicio",
                "description": DESCRIPTION, "amount_text": "100.00", "currency": "PEN", "seace_version": "3"}
            query = {"keyword": "SOFTWARE", "object_type": "service", "year": local.year,
                "published_from": f"{local.year}-01-01", "published_to": local.date().isoformat(), "seace_version": "3"}
            discovery = repository.upsert_pilot_discovery(conn, row, query,
                batch_id="selected-scratch-" + uuid4().hex, observed_at=observed)
            extraction, selector = selected_bundle(archive=True, nested=True, published_at=published,
                                                   policy_version=policy_version)
            assert selector["policy_version"] == policy_version
            parents = extraction["selection_context"]["official_inventory"]
            candidates = extraction["selection_context"]["candidates"]
            assert parents[0]["type"] == "Bases" and parents[0]["original_sha256"] == SHA
            assert all("original_sha256" not in candidate for candidate in candidates)
            assert all(candidate["type"] == "Bases" and candidate["inventory_status"] in {"candidate", "pending_nested_archive"}
                       for candidate in candidates if candidate["source_kind"] == "archive_member")
            assert candidates[2]["source_kind"] == "loose" and "original_sha256" not in candidates[2]
            if policy_version == document_selection.DOCUMENT_SELECTION_POLICY_V2:
                assert all(candidate["preview_text"] == "SYNTHETIC SAMPLE OF DOCUMENT CONTENT"
                           for candidate in candidates)
                assert any(decision["role"] == "administrative_record" for decision in selector["decisions"])
            assert all(row["member_sha256"] == row["original_sha256"] and row["archive_sha256"] == SHA
                       for row in extraction["document_set"] if row["source_kind"] == "archive_member")
            docs = [{"uuid": p["document_uuid"], "filename": p["filename"], "type": p["type"], "stage": p["stage"],
                     "published_at": p["published_at"], "seace_version": "3"} for p in parents]
            detail = {**{key: row[key] for key in ("procedure_id", "convocatoria_id", "entity", "nomenclature", "object", "description")},
                "seace_version": "3", "schedule": [], "item_states": [], "documents": docs,
                "document_inventory_total": len(docs), "document_inventory_complete": True}
            repository.persist_detail(conn, detail, observed_at=observed, source_url=SEARCH_URL)
            for parent in parents:
                if "original_sha256" in parent:
                    archive = parent["filename"].endswith(".zip")
                    repository.record_document_download(conn, conv, {"document_uuid": parent["document_uuid"],
                        "sha256": parent["original_sha256"], "bytes": 1000, "content_type": "application/zip" if archive else "application/pdf",
                        "format": "zip" if archive else "pdf"})
            stage = "durable_global_claim_and_concurrent_lock"
            args = {"convocation_id": conv, "request_hash": selector["request_hash"], "model": selector["model"],
                    "policy_version": selector["policy_version"]}
            claim = selected_repository.claim_selection(conn, **args)
            assert claim["state"] == "claimed"
            assert selected_repository.claim_selection(conn, **args) == {"state": "blocked", "reason": "provider_running"}
            # A real second session cannot acquire this request's claim lock
            # while the first transaction is open, without seeing/writing DDL.
            lock_sql = "SELECT pg_try_advisory_xact_lock(hashtext('prod2_document_selection_cache'),hashtext(%s)) AS obtained"
            assert observer.execute(lock_sql, (args["request_hash"],)).fetchone()["obtained"] is False
            observer.rollback()
            with pytest.raises(selected_repository.SelectionPersistenceError, match="owner mismatch"):
                selected_repository.complete_selection(conn, request_hash=selector["request_hash"],
                    claim_token=str(uuid4()), result=selector)
            conn.execute("UPDATE prod2_document_selection_cache SET lease_until=clock_timestamp()-interval '1 second' WHERE request_hash=%s",
                         (selector["request_hash"],))
            assert selected_repository.claim_selection(conn, **args) == {"state": "blocked", "reason": "provider_uncertain"}
            selected_repository.complete_selection(conn, request_hash=selector["request_hash"],
                                                   claim_token=claim["claim_token"], result=selector)
            assert selected_repository.claim_selection(conn, **{**args, "convocation_id": conv + 1})["state"] == "cached"
            assert selected_repository.complete_selection(conn, request_hash=selector["request_hash"],
                claim_token=claim["claim_token"], result=selector) == selector
            changed = {**selector, "input_tokens": selector["input_tokens"] + 1}
            with pytest.raises(selected_repository.SelectionPersistenceError, match="immutable"):
                selected_repository.complete_selection(conn, request_hash=selector["request_hash"],
                    claim_token=claim["claim_token"], result=changed)
            stage = "paid_selected_completion"
            extraction["model"] = settings.gemini_model
            extraction.pop("evaluation_id")
            extraction.pop("allowed_models")
            extraction["input_tokens"] = extraction["output_tokens"] = 0
            extraction["cost_usd"] = 0.0
            paid = repository.claim_evaluation(conn, conv, kind="gemini_extraction",
                request_hash=extraction["cache_key"].removeprefix("prod2:llm1:"), model=settings.gemini_model,
                policy_version="prod2_document_extraction_v1")
            repository.complete_evaluation(conn, paid["id"], paid["claim_token"], extraction,
                                           input_tokens=0, output_tokens=0, cost_usd=0)
            extraction.update(evaluation_id=paid["id"], allowed_models=[settings.gemini_model])
            def finish():
                return persist_completed_llm1(conn, convocation_id=conv, document_uuid=CODE,
                    extraction=extraction, document_sha256=SHA, source_url=parents[0]["source_url"])
            first, replay = finish(), finish()
            assert first["extraction_id"] == replay["extraction_id"] and first["projection_pending"]
            stored = conn.execute("SELECT raw_extraction_json FROM prod2_document_extractions WHERE id=%s", (first["extraction_id"],)).fetchone()
            assert stored["raw_extraction_json"] == extraction["raw_extraction_json"]
            canonical = conn.execute("SELECT participation_terms_json,subcontracting_status,consortium_status FROM opportunities WHERE id=%s",
                                     (discovery["opportunity_id"],)).fetchone()
            assert canonical["subcontracting_status"] == canonical["consortium_status"] == "not_identified"
            assert canonical["participation_terms_json"]["coverage"] == "selected_documents_only"
            assert canonical["participation_terms_json"]["selection_context"] == extraction["selection_context"]
            assert canonical["participation_terms_json"]["source_document_manifest"] == extraction["raw_extraction_json"]["source_document_manifest"]
            if policy_version == document_selection.DOCUMENT_SELECTION_POLICY_V2:
                assert all(candidate["preview_text"] == "SYNTHETIC SAMPLE OF DOCUMENT CONTENT"
                           for candidate in canonical["participation_terms_json"]["selection_context"]["candidates"])
                receipt = conn.execute("SELECT policy_version,result_json FROM prod2_document_selection_cache WHERE request_hash=%s",
                                       (selector["request_hash"],)).fetchone()
                assert receipt["policy_version"] == policy_version and receipt["result_json"] == selector
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_extractions").fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_selection_cache").fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM prod2_evaluations WHERE kind='gemini_match'").fetchone()["n"] == 0
            stage = "root_revision_invalidates_paid_member_subset"
            repository.record_document_download(conn, conv, {"document_uuid": OTHER_CODE, "sha256": "f" * 64,
                "bytes": 1001, "content_type": "application/pdf", "format": "pdf"})
            with pytest.raises(Llm1CompletionError, match="downloaded revision"):
                finish()
            stage = "public_sequence_invariance_and_unconditional_rollback"
            for name, before in public_sequences.items():
                assert conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(sql.SQL(name))).fetchone() == before
            assert _public_invariants(conn) == before_public
            conn.rollback()
            assert conn.execute("SELECT to_regnamespace(%s) AS ns", (schema,)).fetchone()["ns"] is None
            assert _public_invariants(conn) == before_public
            conn.rollback()
            assert observer.execute(lock_sql, (args["request_hash"],)).fetchone()["obtained"] is True
            observer.rollback()
    except Exception as exc:
        pytest.fail(f"Selected scratch failed at {stage}: {type(exc).__name__}; SQLSTATE={getattr(exc, 'sqlstate', None)}", pytrace=False)
    finally:
        for connection in (observer, conn):
            if connection is not None:
                try:
                    connection.rollback()
                except psycopg.Error:
                    pass
                connection.close()
