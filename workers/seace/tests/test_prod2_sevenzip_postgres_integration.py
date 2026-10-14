"""Opt-in isolated 7z migration contract; no source/model calls or public writes."""
from pathlib import Path
from uuid import uuid4
import os

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import sevenzip_schema as subject
from buenapro_worker.prod2.runtime import production_settings

pytestmark = pytest.mark.skipif(os.environ.get('BUENAPRO_PG_SCRATCH_TEST') != '1',
                                reason='Explicit SSH/PostgreSQL scratch opt-in required')


def test_sevenzip_migration_exact_idempotent_preserves_rows_and_rolls_back():
    root = Path(__file__).resolve().parents[3]
    schema = 'prod2_sevenzip_scratch_' + uuid4().hex
    stage = 'tunnel'
    with production_settings(ssh_host='ubuntu@168.129.177.199',
                             ssh_key=root / 'credentials/ssh-key-2026-07-14.key') as settings:
        with psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=15) as conn:
            try:
                stage = 'isolated_schema'
                conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
                conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
                conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
                conn.execute('''CREATE TABLE prod2_document_downloads(id_convocatoria BIGINT NOT NULL,
                    codigo_alfresco UUID NOT NULL, sha256_original TEXT NOT NULL,
                    bytes BIGINT NOT NULL CHECK(bytes>0), content_type TEXT NOT NULL,
                    format TEXT NOT NULL CHECK(format IN ('pdf','zip','ole','rar')),
                    downloaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY(id_convocatoria,codigo_alfresco,sha256_original))''')
                for number, format in enumerate(('pdf', 'zip', 'ole', 'rar'), 1):
                    conn.execute('''INSERT INTO prod2_document_downloads
                        (id_convocatoria,codigo_alfresco,sha256_original,bytes,content_type,format)
                        VALUES(%s,%s,%s,%s,%s,%s)''', (number, uuid4(), str(number) * 64, 10, 'synthetic', format))
                before = conn.execute('SELECT * FROM prod2_document_downloads ORDER BY id_convocatoria').fetchall()
                stage = 'readiness_before_migration'
                with pytest.raises(RuntimeError, match='migration0042'):
                    subject.require_schema(conn)
                assert subject.validate_schema(conn, allow_legacy=True) == 'legacy'
                stage = 'prepare_migration'
                migration = root / 'db/migrations' / subject.MIGRATION
                assert subject.prepare_schema(conn, migration)['status'] == 'sevenzip_download_schema_ready'
                subject.require_schema(conn)
                ledger = conn.execute('SELECT * FROM schema_migrations').fetchall()
                stage = 'replay_and_unchanged_revisions'
                subject.prepare_schema(conn, migration)
                assert conn.execute('SELECT * FROM schema_migrations').fetchall() == ledger
                assert conn.execute('SELECT * FROM prod2_document_downloads ORDER BY id_convocatoria').fetchall() == before
                assert len(ledger) == 1 and ledger[0]['version'] == migration.name
                stage = 'format_acceptance_and_rejection'
                conn.execute('''INSERT INTO prod2_document_downloads
                    (id_convocatoria,codigo_alfresco,sha256_original,bytes,content_type,format)
                    VALUES(5,%s,%s,10,'synthetic','7z')''', (uuid4(), '5' * 64))
                with pytest.raises(psycopg.errors.CheckViolation):
                    with conn.transaction():
                        conn.execute('''INSERT INTO prod2_document_downloads
                            (id_convocatoria,codigo_alfresco,sha256_original,bytes,content_type,format)
                            VALUES(6,%s,%s,10,'synthetic','exe')''', (uuid4(), '6' * 64))
                assert conn.execute('SELECT count(*) AS n FROM prod2_document_downloads').fetchone()['n'] == 5
                stage = 'rollback'
            except Exception as exc:
                pytest.fail(f'7z scratch failed at {stage}: {type(exc).__name__}; SQLSTATE={getattr(exc,"sqlstate",None)}',
                            pytrace=False)
            finally:
                conn.rollback()
                assert conn.execute('SELECT to_regnamespace(%s) AS ns', (schema,)).fetchone()['ns'] is None
                conn.rollback()
