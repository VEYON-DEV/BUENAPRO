"""Opt-in PostgreSQL contract test, isolated schema and unconditional rollback.

Run only with explicit authorization and BUENAPRO_PG_SCRATCH_TEST=1. Uses the
existing private SSH tunnel runtime; never prints connection configuration.
Copies structure only, including indexes/checks, and replaces copied serial
defaults with scratch sequences BEFORE inserts. No models or portal requests.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2.client import SEARCH_URL
from buenapro_worker.prod2 import repository as subject
from buenapro_worker.prod2.runtime import production_settings

ENABLED = os.environ.get("BUENAPRO_PG_SCRATCH_TEST") == "1"
BASE_TABLES = ("opportunities", "opportunity_sources", "prod4_processes", "company_profiles",
               "business_lines", "opportunity_matches")


@pytest.mark.skipif(not ENABLED, reason="Explicit opt-in required for SSH/PostgreSQL scratch test")
def test_prod2_full_persistence_contract_in_rolled_back_scratch_schema():
    root = Path(__file__).resolve().parents[3]
    stage = "tunnel"
    schema = "prod2_scratch_" + uuid4().hex
    conn = None
    try:
        with production_settings(ssh_host="ubuntu@168.129.177.199",
                                 ssh_key=root / "credentials/ssh-key-2026-07-14.key") as settings:
            conn = psycopg.connect(settings.database_url, row_factory=dict_row)
            stage = "shadow_schema"
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
            public_sequences = {}
            for table in BASE_TABLES:
                conn.execute(sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING ALL)").format(
                    sql.Identifier(table), sql.Identifier(table)))
                columns = conn.execute("""SELECT column_name, column_default FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s AND column_default LIKE 'nextval(%%'""",
                    (schema, table)).fetchall()
                for column in columns:
                    public_sequence = conn.execute("SELECT pg_get_serial_sequence(%s,%s) AS name",
                        ("public." + table, column["column_name"])).fetchone()["name"]
                    if public_sequence:
                        before = conn.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(
                            sql.SQL(public_sequence))).fetchone()
                        public_sequences[public_sequence] = before
                    sequence = table + "_" + column["column_name"] + "_scratch_seq"
                    conn.execute(sql.SQL("CREATE SEQUENCE {}").format(sql.Identifier(sequence)))
                    conn.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET DEFAULT nextval({}::regclass)").format(
                        sql.Identifier(table), sql.Identifier(column["column_name"]),
                        sql.Literal(schema + "." + sequence)))
            for filename in ("0035_prod2_discovery.sql", "0036_prod2_pilot_enrichment.sql", "0041_prod2_document_events.sql"):
                draft = root / "db/drafts" / filename
                migration = root / "db/migrations" / filename
                conn.execute((draft if draft.exists() else migration).read_text(encoding="utf-8"))
            # New FK bindings must point to scratch tables, never public data.
            public_fks = conn.execute("""SELECT count(*) AS n FROM pg_constraint c
                JOIN pg_class own ON own.oid=c.conrelid JOIN pg_namespace ns ON ns.oid=own.relnamespace
                JOIN pg_class target ON target.oid=c.confrelid JOIN pg_namespace tn ON tn.oid=target.relnamespace
                WHERE ns.nspname=%s AND c.contype='f' AND tn.nspname<>%s""", (schema, schema)).fetchone()["n"]
            assert public_fks == 0
            profile, tenant, line = uuid4(), uuid4(), uuid4()
            conn.execute("""INSERT INTO company_profiles(id,tenant_id,ruc,razon_social)
                VALUES (%s,%s,'SYNTHETIC_SCRATCH_ONLY','Synthetic integration profile')""", (profile, tenant))
            conn.execute("INSERT INTO business_lines(id,profile_id,nombre) VALUES (%s,%s,'Synthetic software')", (line, profile))
            observed = datetime.now(timezone.utc)
            year = observed.astimezone(subject.LIMA).year
            assert year == 2026, "Current bounded capture contract supports the 2026 pilot"
            published = observed.astimezone(subject.LIMA).strftime("%d/%m/%Y %H:%M")
            query = {"keyword": "SOFTWARE", "object_type": "service", "year": year,
                     "published_from": f"{year}-01-01", "published_to": observed.astimezone(subject.LIMA).date().isoformat(),
                     "seace_version": "3"}
            batch = "scratch-pilot-" + uuid4().hex
            rows = [{"procedure_id": 8000000000 + number, "convocatoria_id": 9000000000 + number,
                     "entity": "SYNTHETIC BUYER", "published_at": published,
                     "nomenclature": f"SCRATCH-2026-{number}", "object": "Servicio", "description": "SOFTWARE",
                     "amount_text": "100.00", "currency": "PEN", "seace_version": "3"} for number in range(11)]
            stage = "pilot_upsert_replay_limit"
            identities = [subject.upsert_pilot_discovery(conn, row, query, batch_id=batch, observed_at=observed)
                          for row in rows[:10]]
            replay = subject.upsert_pilot_discovery(conn, rows[0], query, batch_id=batch, observed_at=observed)
            assert replay["opportunity_id"] == identities[0]["opportunity_id"] and replay["created"] is False
            with pytest.raises(subject.Prod2PersistenceError, match="limit"):
                subject.upsert_pilot_discovery(conn, rows[10], query, batch_id=batch, observed_at=observed)
            assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"] == 10
            assert conn.execute("SELECT count(*) AS n FROM prod2_pilot_observations").fetchone()["n"] == 10
            assert conn.execute("SELECT count(*) AS n FROM opportunities").fetchone()["n"] == 10
            assert conn.execute("SELECT count(*) AS n FROM prod2_discovery_runs").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM prod2_discovery_pages").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM prod4_processes").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM opportunity_sources WHERE source_key<>'seace_prod2'").fetchone()["n"] == 0
            convocation, code, sha = rows[0]["convocatoria_id"], str(uuid4()), "a" * 64
            stage = "ficha_and_document_revision"
            detail = {"procedure_id": rows[0]["procedure_id"], "convocatoria_id": convocation,
                      "nomenclature": rows[0]["nomenclature"], "entity": rows[0]["entity"], "object": "Servicio",
                      "description": "SOFTWARE", "seace_version": "3", "schedule": [
                      {"stage": "Presentación de ofertas", "starts_at_text": "07/10/2026 08:00",
                       "ends_at_text": "07/10/2026 17:00"},
                      {"stage": "Otorgamiento de Buena Pro", "starts_at_text": "08/10/2026", "ends_at_text": "08/10/2026"}],
                      "documents": [{"uuid": code, "seace_version": "3", "filename": "synthetic-bases.pdf",
                      "stage": "Convocatoria", "type": "Bases", "published_at": published}],
                      "document_inventory_total": 1, "document_inventory_complete": True, "item_states": ["Adjudicado"]}
            subject.persist_detail(conn, detail, observed_at=observed, source_url=SEARCH_URL, source_session_dependent=True)
            ficha = conn.execute("SELECT * FROM prod2_details WHERE id_convocatoria=%s", (convocation,)).fetchone()
            assert ficha["source_url"] == SEARCH_URL and ficha["source_session_dependent"] is True
            assert ficha["buena_pro_json"]["winner"] is None and ficha["buena_pro_json"]["award_result"] == "unknown"
            manifest = {"document_uuid": code, "sha256": sha, "bytes": 1234, "content_type": "application/pdf", "format": "pdf"}
            subject.record_document_download(conn, convocation, manifest)
            subject.record_document_download(conn, convocation, {**manifest, "sha256": "b" * 64, "bytes": 1235})
            assert conn.execute("SELECT count(*) AS n FROM prod2_document_downloads").fetchone()["n"] == 2
            stage = "reservations_cache_and_uncertain"
            kwargs = {"kind": "jev_affinity", "request_hash": "c" * 64, "model": "synthetic-jev",
                      "policy_version": "scratch-v1", "profile_id": str(profile)}
            claimed = subject.claim_evaluation(conn, convocation, **kwargs)
            assert claimed["status"] == "claimed"
            assert subject.claim_evaluation(conn, convocation, **kwargs)["status"] == "busy"
            subject.complete_evaluation(conn, claimed["id"], claimed["claim_token"], {"label": "directa"}, input_tokens=100, cost_usd="0.000001")
            assert subject.claim_evaluation(conn, convocation, **kwargs)["status"] == "cached"
            expired = subject.claim_evaluation(conn, convocation, **{**kwargs, "request_hash": "d" * 64})
            conn.execute("UPDATE prod2_evaluations SET lease_until=now()-interval '1 second' WHERE id=%s", (expired["id"],))
            assert subject.claim_evaluation(conn, convocation, **{**kwargs, "request_hash": "d" * 64})["status"] == "uncertain"
            subject.complete_evaluation(conn, expired["id"], expired["claim_token"], {"label": "posible"})
            stage = "gemini_effective_fallback_extraction"
            primary, fallback = settings.gemini_model, settings.gemini_fallback_model
            reservation = subject.claim_evaluation(conn, convocation, kind="gemini_extraction", request_hash="e" * 64,
                          model=primary, policy_version="scratch-extraction-v1")
            subject.complete_evaluation(conn, reservation["id"], reservation["claim_token"], {
                "document_uuid": code, "document_sha256": sha, "model": fallback})
            result = SimpleNamespace(raw_json={"requirements": {"provider": {"specific_experience": [{
                     "descripcion": "Experiencia en soporte de software", "evidence": [{"page": 2}]}]}}},
                     model=fallback, prompt_version="scratch-v1", schema_version="v2", input_tokens=100,
                     output_tokens=80, cost_usd="0.00001", requires_human_review=False)
            extraction = subject.persist_extraction(conn, convocation, code, sha, evaluation_id=reservation["id"],
                         result=result, allowed_models=(primary, fallback))
            extraction_replay = subject.persist_extraction(conn, convocation, code, sha, evaluation_id=reservation["id"],
                                result=result, allowed_models=(primary, fallback))
            assert extraction_replay["cached"] and extraction_replay["extraction_id"] == extraction["extraction_id"]
            assert conn.execute("SELECT model FROM prod2_document_extractions").fetchone()["model"] == fallback
            assert conn.execute("SELECT count(*) AS n FROM prod2_requirement_facets").fetchone()["n"] == 1
            stage = "canonical_match_with_prod2_provenance"
            profile_hash = "f" * 64
            match_claim = subject.claim_evaluation(conn, convocation, kind="gemini_match", request_hash="1" * 64,
                           model=primary, policy_version="scratch-match-v1", profile_id=str(profile))
            subject.complete_evaluation(conn, match_claim["id"], match_claim["claim_token"], {
                "extraction_id": extraction["extraction_id"], "profile_hash": profile_hash, "model": fallback})
            matched = subject.persist_match(conn, convocation, profile_id=str(profile), business_line_id=str(line),
                      extraction_id=extraction["extraction_id"], evaluation_id=match_claim["id"], profile_hash=profile_hash,
                      score=85, verdict="verde", breakdown_json={"meta": {"member_profile_hashes": {str(profile): "synthetic"}}},
                      missing_actions_json=[])
            stored = conn.execute("SELECT * FROM opportunity_matches WHERE id=%s", (matched["match_id"],)).fetchone()
            assert stored["breakdown_json"]["meta"]["source_key"] == "seace_prod2"
            assert stored["breakdown_json"]["meta"]["document_sha256"] == sha
            assert conn.execute("SELECT count(*) AS n FROM prod2_match_provenance").fetchone()["n"] == 1
            stage = "public_sequence_invariance_and_rollback"
            for name, previous in public_sequences.items():
                assert conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(sql.SQL(name))).fetchone() == previous
            conn.rollback()
            assert conn.execute("SELECT to_regnamespace(%s) AS ns", (schema,)).fetchone()["ns"] is None
            conn.rollback()
    except Exception as exc:
        # Avoid pytest diagnostics displaying DSNs/configuration if transport fails.
        pytest.fail(f"PG scratch test failed at {stage}: {type(exc).__name__}; SQLSTATE={getattr(exc, 'sqlstate', None)}", pytrace=False)
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()
