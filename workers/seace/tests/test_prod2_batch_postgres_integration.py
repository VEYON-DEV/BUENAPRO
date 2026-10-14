"""Opt-in real PG orchestration contract with a wholly rolled-back scratch schema.

Requires explicit authorization and BUENAPRO_PG_SCRATCH_TEST=1. No HTTP/model
calls. Copied serial defaults are replaced before inserts, and every FK target
must be scratch. Transport errors are sanitized; connection data stays private.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import batch_repository as subject, repository
from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION, jev_request_hash
from buenapro_worker.prod2.client import SEARCH_URL
from buenapro_worker.prod2.discovery import validate_capture
from buenapro_worker.prod2.participation import decide
from buenapro_worker.prod2.runtime import production_settings

ENABLED = os.environ.get("BUENAPRO_PG_SCRATCH_TEST") == "1"
BASE_TABLES = ("opportunities", "opportunity_sources", "prod4_processes", "company_profiles",
               "business_lines", "opportunity_matches")


def _shadow_schema(conn, schema, root):
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
    public_sequences = {}
    for table in BASE_TABLES:
        conn.execute(sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING ALL)").format(
            sql.Identifier(table), sql.Identifier(table)))
        columns = conn.execute("""SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s AND column_default LIKE 'nextval(%%'""",
            (schema, table)).fetchall()
        for column in columns:
            public_sequence = conn.execute("SELECT pg_get_serial_sequence(%s,%s) AS name",
                ("public." + table, column["column_name"])).fetchone()["name"]
            if public_sequence:
                public_sequences[public_sequence] = conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(
                    sql.SQL(public_sequence))).fetchone()
            sequence = table + "_" + column["column_name"] + "_scratch_seq"
            conn.execute(sql.SQL("CREATE SEQUENCE {}").format(sql.Identifier(sequence)))
            conn.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET DEFAULT nextval({}::regclass)").format(
                sql.Identifier(table), sql.Identifier(column["column_name"]), sql.Literal(schema + "." + sequence)))
    for filename in ("0035_prod2_discovery.sql", "0036_prod2_pilot_enrichment.sql", "0037_prod2_batch_orchestration.sql",
                     "0041_prod2_document_events.sql"):
        draft = root / "db/drafts" / filename
        migration = root / "db/migrations" / filename
        conn.execute((draft if draft.exists() else migration).read_text(encoding="utf-8"))
    assert conn.execute("""SELECT count(*) AS n FROM pg_constraint c
        JOIN pg_class own ON own.oid=c.conrelid JOIN pg_namespace ns ON ns.oid=own.relnamespace
        JOIN pg_class target ON target.oid=c.confrelid JOIN pg_namespace tn ON tn.oid=target.relnamespace
        WHERE ns.nspname=%s AND c.contype='f' AND tn.nspname<>%s""", (schema, schema)).fetchone()["n"] == 0
    return public_sequences


def _capture(query, observed, rows, *, page=1, total=None):
    total = len(rows) if total is None else total
    return {"query": query, "observed_at": observed.isoformat(), "rows": rows,
            "page": {"number": page, "page_size": 15, "total_results": total,
                     "total_pages": max(1, (total + 14) // 15)}}


@pytest.mark.skipif(not ENABLED, reason="Explicit opt-in required for SSH/PostgreSQL scratch test")
def test_prod2_batch_real_pg_contract_and_unconditional_rollback():
    root, schema = Path(__file__).resolve().parents[3], "prod2_batch_scratch_" + uuid4().hex
    stage, conn = "tunnel", None
    try:
        with production_settings(ssh_host="ubuntu@168.129.177.199",
                                 ssh_key=root / "credentials/ssh-key-2026-07-14.key") as settings:
            conn = psycopg.connect(settings.database_url, row_factory=dict_row)
            stage = "shadow_schema"
            public_sequences = _shadow_schema(conn, schema, root)
            profile, tenant = uuid4(), uuid4()
            conn.execute("""INSERT INTO company_profiles(id,tenant_id,ruc,razon_social)
                VALUES (%s,%s,'SYNTHETIC_SCRATCH_ONLY','Synthetic batch profile')""", (profile, tenant))
            observed = datetime.now(timezone.utc)
            local = observed.astimezone(repository.LIMA)
            day = local.date().isoformat()
            config = {"published_from": (local.date()-timedelta(days=7)).isoformat(), "published_to": day,
                "procedure_years": [local.year-1], "keywords": ["SOFTWARE", "SISTEMAS"], "object_types": ["service"],
                "seace_version": "3", "profile_id": str(profile), "commercial_profiles": [
                    {"descripcion": "Soporte software", "lineas": ["software"]}],
                "jev_model": JEV_MODEL, "jev_policy_version": JEV_POLICY_VERSION}
            batch = "scratch-batch-" + uuid4().hex
            stage = "immutable_manifest_and_no_pilot_limit"
            assert subject.create_batch(conn, batch, config)["seeded_queries"] == 2
            assert subject.create_batch(conn, batch, config)["seeded_queries"] == 2
            with pytest.raises(subject.BatchError, match="configuration changed"):
                subject.create_batch(conn, batch, {**config, "keywords": ["CHANGED"]})
            first = subject.claim_task(conn, batch, owner_id="scratch-owner")
            token, tid, query = first["owner_token"], first["task_id"], first["query"]
            rows = [{"procedure_id": 8000000000+n, "convocation_id": 9000000000+n,
                "nomenclature": f"SCRATCH-{local.year}-{n}", "buyer_name": "SYNTHETIC BUYER",
                "published_at": local.strftime("%d/%m/%Y %H:%M"), "description": "SOFTWARE",
                "object_label": "Servicio", "reference_amount": "100.00", "currency": "PEN", "seace_version": "3"}
                for n in range(16)]
            page1 = _capture(query, observed, rows[:15], total=16)
            stage = "page_atomic_replay_and_restart"
            assert subject.persist_page(conn, tid, token, page1)["status"] == "partial"
            assert subject.persist_page(conn, tid, token, page1)["replayed"] is True
            saved = subject.discovery_checkpoint(conn, tid, token)
            assert saved["checkpoint"]["next_page"] == 2
            assert saved["saved_pass"]["first_page_hash"] == validate_capture(page1).page_hash
            assert subject.advance_phase(conn, batch)["phase"] == "discovery"
            assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"] == 15
            restarted = subject.restart_discovery_task(conn, tid, token)
            assert restarted["discovery_run_id"] != first["discovery_run_id"]
            assert conn.execute("SELECT count(*) AS n FROM prod2_batch_task_passes WHERE task_id=%s", (tid,)).fetchone()["n"] == 2
            assert subject.persist_page(conn, tid, token, page1)["status"] == "partial"
            page2 = _capture(query, observed, rows[15:], page=2, total=16)
            assert subject.persist_page(conn, tid, token, page2)["status"] == "complete"
            second = subject.claim_task(conn, batch, owner_id="scratch-owner")
            # Same 16 identities found by another keyword: dedup before Jev.
            subject.persist_page(conn, second["task_id"], second["owner_token"], _capture(second["query"], observed, rows[:15], total=16))
            subject.persist_page(conn, second["task_id"], second["owner_token"], _capture(second["query"], observed, rows[15:], page=2, total=16))
            assert conn.execute("SELECT count(*) AS n FROM prod2_batch_memberships WHERE run_id=%s", (batch,)).fetchone()["n"] == 16
            assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"] == 16
            assert conn.execute("SELECT count(*) AS n FROM prod2_pilot_batches").fetchone()["n"] == 0
            stage = "all_leaves_gate_and_unique_enrichment"
            gate = subject.advance_phase(conn, batch)
            assert gate["phase"] == "enrichment" and gate["unique_candidates"] == 16
            locator = subject.discovery_locator(conn, batch, rows[0]["convocation_id"])
            assert locator["row_manifest"]["procedure_id"] == rows[0]["procedure_id"]
            assert locator["page_number"] == 1
            enrich = subject.claim_task(conn, batch, owner_id="scratch-enrichment")
            conv = enrich["id_convocatoria"]
            row = next(r for r in rows if r["convocation_id"] == conv)
            stage = "unpaid_recovery_and_exact_reservation"
            conn.execute("UPDATE prod2_batch_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (enrich["task_id"],))
            assert subject.claim_task(conn, batch, owner_id="scratch-enrichment")["status"] == "blocked"
            pending = subject.recover_unpaid_enrichment_task(conn, enrich["task_id"])
            ledger = conn.execute("SELECT * FROM prod2_evaluations WHERE id=%s", (pending["jev_evaluation_id"],)).fetchone()
            assert ledger["status"] == "pending" and ledger["claim_token"] is None
            enrich = subject.claim_task(conn, batch, owner_id="scratch-enrichment")
            request = jev_request_hash("SOFTWARE", commercial_profiles=config["commercial_profiles"], model=JEV_MODEL)
            claim_args = {"kind": "jev_affinity", "request_hash": request, "model": JEV_MODEL,
                "policy_version": JEV_POLICY_VERSION, "profile_id": str(profile)}
            paid = repository.claim_evaluation(conn, conv, **claim_args)
            assert paid["id"] == pending["jev_evaluation_id"]
            conn.execute("UPDATE prod2_batch_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (enrich["task_id"],))
            subject.claim_task(conn, batch, owner_id="scratch-enrichment")
            with pytest.raises(subject.BatchError, match="billed"):
                subject.recover_unpaid_enrichment_task(conn, enrich["task_id"])
            repository.complete_evaluation(conn, paid["id"], paid["claim_token"], {"label": "fuera_de_rubro"}, input_tokens=20, cost_usd="0.000001")
            subject.recover_enrichment_task(conn, enrich["task_id"], paid["id"])
            enrich = subject.claim_task(conn, batch, owner_id="scratch-enrichment")
            assert repository.claim_evaluation(conn, conv, **claim_args)["status"] == "cached"
            stage = "official_ficha_and_participation_binding"
            # Ficha persists even when Jev rejects; no document bytes/Gemini.
            detail = {"procedure_id": row["procedure_id"], "convocatoria_id": conv,
                "nomenclature": row["nomenclature"], "entity": row["buyer_name"], "object": "Servicio",
                "description": "SOFTWARE", "selection_type": "Concurso público", "seace_version": "3",
                "schedule": [{"stage": "Registro de participantes", "starts_at_text": local.strftime("%d/%m/%Y 00:00"),
                              "ends_at_text": (local+timedelta(days=1)).strftime("%d/%m/%Y 17:00")},
                             {"stage": "Presentación de ofertas", "starts_at_text": (local+timedelta(days=2)).strftime("%d/%m/%Y 08:00"),
                              "ends_at_text": (local+timedelta(days=2)).strftime("%d/%m/%Y 17:00")}],
                "documents": [], "document_inventory_total": 0, "document_inventory_complete": True,
                "item_states": ["Convocado"]}
            repository.persist_detail(conn, detail, observed_at=observed, source_url=SEARCH_URL, source_session_dependent=True)
            ficha = conn.execute("SELECT * FROM prod2_details WHERE id_convocatoria=%s", (conv,)).fetchone()
            assessment = decide(ficha["raw_detail"], observed)
            assessment_id = subject.save_participation_assessment(conn, conv, detail_hash=ficha["detail_hash"], assessment=assessment)["id"]
            assert subject.save_participation_assessment(conn, conv, detail_hash=ficha["detail_hash"], assessment=assessment)["cached"]
            result = {"jev_evaluation_id": paid["id"], "participation_assessment_id": assessment_id, "detail_hash": ficha["detail_hash"]}
            wrong = repository.claim_evaluation(conn, conv, **{**claim_args, "request_hash": "b"*64})
            repository.complete_evaluation(conn, wrong["id"], wrong["claim_token"], {"label": "directa"})
            with pytest.raises(subject.BatchError, match="does not bind"):
                subject.complete_task(conn, enrich["task_id"], enrich["owner_token"], {**result, "jev_evaluation_id": wrong["id"]})
            assert subject.complete_task(conn, enrich["task_id"], enrich["owner_token"], result)["status"] == "complete"
            assert subject.advance_phase(conn, batch)["phase"] == "enrichment"
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_downloads").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM prod2_evaluations WHERE kind<>'jev_affinity'").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM opportunities WHERE lifecycle_stage<>'unknown'").fetchone()["n"] == 0
            stage = "adaptive_caps_and_controlled_source_recovery"
            capped_batch = "scratch-cap-" + uuid4().hex
            subject.create_batch(conn, capped_batch, {**config, "keywords": ["CAP"]})
            parent = subject.claim_task(conn, capped_batch, owner_id="scratch-owner")
            split = subject.split_capped_task(conn, parent["task_id"], parent["owner_token"], total_results=499)
            assert split["status"] == "split" and len(split["children"]) == 2
            assert subject.advance_phase(conn, capped_batch)["phase"] == "discovery"
            child = subject.claim_task(conn, capped_batch, owner_id="scratch-owner")
            subject.block_task(conn, child["task_id"], child["owner_token"], error_code="source_403")
            subject.recover_discovery_task(conn, child["task_id"])
            child = subject.claim_task(conn, capped_batch, owner_id="scratch-owner")
            subject.persist_page(conn, child["task_id"], child["owner_token"], _capture(child["query"], observed, []))
            child = subject.claim_task(conn, capped_batch, owner_id="scratch-owner")
            subject.persist_page(conn, child["task_id"], child["owner_token"], _capture(child["query"], observed, []))
            assert subject.advance_phase(conn, capped_batch)["status"] == "complete"
            stage = "report_public_sequence_invariance_and_rollback"
            assert subject.report_batch(conn, batch)["unique_candidates"] == 16
            for name, before in public_sequences.items():
                assert conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(sql.SQL(name))).fetchone() == before
            conn.rollback()
            assert conn.execute("SELECT to_regnamespace(%s) AS ns", (schema,)).fetchone()["ns"] is None
            conn.rollback()
    except Exception as exc:
        pytest.fail(f"PG batch scratch failed at {stage}: {type(exc).__name__}; SQLSTATE={getattr(exc, 'sqlstate', None)}", pytrace=False)
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()
