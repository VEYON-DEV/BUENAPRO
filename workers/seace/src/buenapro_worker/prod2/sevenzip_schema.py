"""Dedicated 7z download-format migration and read-only readiness gate.

No source/provider clients. Preparation takes the shared PROD2 executor lock and
only changes the known format CHECK; existing revisions and other constraints
remain intact. An outer scratch transaction remains owned by its caller.
"""
from pathlib import Path
import re


MIGRATION = "0042_prod2_7z_downloads.sql"
CONSTRAINT = "prod2_document_downloads_format_check"


def _definition(formats):
    values = ",".join("'" + value + "'::text" for value in formats)
    return "CHECK((format=ANY(ARRAY[" + values + "])))"


def validate_schema(conn, *, allow_legacy=False):
    column = conn.execute("""SELECT format_type(atttypid,atttypmod) AS type,attnotnull
        FROM pg_attribute WHERE attrelid=to_regclass('prod2_document_downloads')
        AND attname='format' AND attnum>0 AND NOT attisdropped""").fetchone()
    if column != {"type": "text", "attnotnull": True}:
        raise ValueError("Download format column has an incompatible contract")
    constraints = conn.execute("""SELECT c.conname,c.convalidated,pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
        WHERE c.conrelid=to_regclass('prod2_document_downloads') AND c.contype='c' AND a.attname='format'""").fetchall()
    if (len(constraints) != 1 or constraints[0]["conname"] != CONSTRAINT
            or constraints[0]["convalidated"] is not True):
        raise ValueError("Download format constraint has an incompatible contract")
    definition = re.sub(r"\s+", "", constraints[0]["definition"])
    if definition == _definition(("pdf", "zip", "ole", "rar", "7z")):
        return "7z"
    if allow_legacy and definition == _definition(("pdf", "zip", "ole", "rar")):
        return "legacy"
    raise ValueError("Download format constraint does not permit the exact 7z contract")


def require_schema(conn):
    """Read-only prepayment gate for explicitly opted-in 7z runs."""
    ready = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS ready").fetchone()
    if not ready or ready["ready"] is not True:
        raise RuntimeError("Apply migration0042 before 7z source/provider calls")
    if not conn.execute("SELECT version FROM schema_migrations WHERE version=%s", (MIGRATION,)).fetchone():
        raise RuntimeError("Apply migration0042 before 7z source/provider calls")
    validate_schema(conn)


def prepare_schema(conn, migration: Path):
    if migration.name != MIGRATION:
        raise ValueError("Expected the dedicated 7z download migration")
    with conn.transaction():
        locked = conn.execute("SELECT pg_try_advisory_xact_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()
        if not locked or locked["locked"] is not True:
            raise RuntimeError("Another PROD2 coordinator owns the executor")
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_7z_download_schema'),0)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        applied = conn.execute("SELECT version FROM schema_migrations WHERE version=%s", (MIGRATION,)).fetchone()
        if applied:
            validate_schema(conn)
        else:
            if validate_schema(conn, allow_legacy=True) == "legacy":
                conn.execute(migration.read_text(encoding="utf-8"))
            validate_schema(conn)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (MIGRATION,))
    return {"status": "sevenzip_download_schema_ready", "migration": MIGRATION}
