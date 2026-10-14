"""Publication fingerprints and migration replay control without external calls."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from buenapro_worker.prod2 import document_events as subject


def metadata(**changes):
    return {"uuid": "00000000-0000-4000-8000-000000000001", "filename": "Términos de referencia.pdf",
            "stage": "Convocatoria", "type": "Bases", "published_at_text": "06/10/2026 10:00",
            "published_at": "2026-10-06T10:00:00-05:00", **changes}


def test_publication_fingerprint_binds_file_stage_type_and_original_date_not_observation():
    original = subject.event_hash(metadata())
    assert original == subject.event_hash(metadata(published_at=None))
    assert len({original, *(subject.event_hash(metadata(**{key: value})) for key, value in
        (("uuid", "00000000-0000-4000-8000-000000000002"), ("filename", "TDR.pdf"),
         ("stage", "Bases integradas"), ("type", "Anexo"), ("published_at_text", "06/10/2026 10:01")))}) == 6
    assert subject.event_hash(metadata(filename="ab", stage="c")) != subject.event_hash(metadata(filename="a", stage="bc"))


def test_migration_replay_validates_ledger_but_never_replays_ddl_or_backfill(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"version": subject.MIGRATION}
    validate = MagicMock()
    monkeypatch.setattr(subject, "validate_schema", validate)
    path = MagicMock(spec=Path)
    path.name = subject.MIGRATION
    assert subject.prepare_schema(conn, path)["status"] == "document_events_schema_ready"
    path.read_text.assert_not_called()
    validate.assert_called_once_with(conn)
    assert not any("INSERT INTO schema_migrations" in call.args[0] for call in conn.execute.call_args_list)


def test_first_preparation_applies_only_dedicated_migration_then_records_verified_contract(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    validate = MagicMock()
    monkeypatch.setattr(subject, "validate_schema", validate)
    path = MagicMock(spec=Path)
    path.name = subject.MIGRATION
    path.read_text.return_value = "SYNTHETIC DEDICATED SQL"
    subject.prepare_schema(conn, path)
    path.read_text.assert_called_once_with(encoding="utf-8")
    assert any(call.args[0] == "SYNTHETIC DEDICATED SQL" for call in conn.execute.call_args_list)
    assert conn.execute.call_args.args == ("INSERT INTO schema_migrations(version) VALUES (%s)", (subject.MIGRATION,))
    validate.assert_called_once_with(conn)


def test_preparation_rejects_unrelated_migration_before_database_calls():
    conn = MagicMock()
    with pytest.raises(ValueError, match="dedicated"):
        subject.prepare_schema(conn, Path("0036_prod2_pilot_enrichment.sql"))
    conn.execute.assert_not_called()
