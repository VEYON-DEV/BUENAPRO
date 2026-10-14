"""Publication identity and isolated preparation; never download file contents."""
from __future__ import annotations

import hashlib
from pathlib import Path

MIGRATION = "0041_prod2_document_events.sql"
POLICY = "prod2_document_event_v1:"
EXPECTED_COLUMNS = {
    "id_convocatoria": ("bigint", True), "codigo_alfresco": ("uuid", True),
    "event_hash": ("text", True), "filename": ("text", True),
    "seace_version": ("text", True), "document_type": ("text", True),
    "stage_name": ("text", True), "published_at": ("timestamp with time zone", False),
    "published_at_text": ("text", True), "is_listed": ("boolean", True),
    "first_seen_at": ("timestamp with time zone", True),
    "last_seen_at": ("timestamp with time zone", True),
}


def event_hash(document: dict) -> str:
    """SHA256 over unambiguous UTF-8 length-prefixed normalized source fields.

    PostgreSQL's migration uses the same octet lengths and core sha256. Neither
    volatile observation times nor normalized (potentially absent) dates enter
    identity; two official stage/type/publication rows remain separate events.
    """
    values = (document["uuid"], "3", document["filename"], document["stage"],
              document["type"], document["published_at_text"])
    encoded = POLICY + "".join(f"{len(value.encode('utf-8'))}:{value}" for value in values)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_schema(conn) -> None:
    rows = conn.execute("""SELECT attname,format_type(atttypid,atttypmod) AS type,attnotnull
        FROM pg_attribute WHERE attrelid=to_regclass('prod2_document_events')
        AND attnum>0 AND NOT attisdropped""").fetchall()
    if {row["attname"]: (row["type"], row["attnotnull"]) for row in rows} != EXPECTED_COLUMNS:
        raise ValueError("Existing publication table has incompatible columns")
    constraints = conn.execute("""SELECT contype,pg_get_constraintdef(oid) AS definition
        FROM pg_constraint WHERE conrelid='prod2_document_events'::regclass""").fetchall()
    checks = [row["definition"] for row in constraints if row["contype"] == "c"]
    if (not any(row["contype"] == "p" and row["definition"] ==
                "PRIMARY KEY (id_convocatoria, codigo_alfresco, event_hash)" for row in constraints)
            or not any(row["contype"] == "f" and "FOREIGN KEY (id_convocatoria, codigo_alfresco)" in row["definition"]
                       and "REFERENCES prod2_documents(id_convocatoria, codigo_alfresco) ON DELETE CASCADE" in row["definition"]
                       for row in constraints)
            or not any("seace_version" in value and "'3'" in value for value in checks)
            or not any("last_seen_at >= first_seen_at" in value for value in checks)
            or not any("event_hash" in value and "^[0-9a-f]{64}$" in value for value in checks)
            or not any("event_hash" in value and "sha256" in value and POLICY in value for value in checks)):
        raise ValueError("Existing publication table has incompatible identity constraints")


def prepare_schema(conn, migration: Path) -> dict:
    """Apply only 0041 once under the migration ledger; validate every replay.

    Requires an existing prod2_documents table. An enclosing scratch transaction
    remains owned by its caller. Never replay legacy DDL or recreate missing
    objects after an already-recorded migration: that is a contract failure.
    """
    if migration.name != MIGRATION:
        raise ValueError("Expected the dedicated document publication migration")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_document_events_schema'),0)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        applied = conn.execute("SELECT version FROM schema_migrations WHERE version=%s", (MIGRATION,)).fetchone()
        if not applied:
            conn.execute(migration.read_text(encoding="utf-8"))
        validate_schema(conn)
        if not applied:
            conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (MIGRATION,))
    return {"status": "document_events_schema_ready", "migration": MIGRATION}
