"""Schema replay/gates without services; real PostgreSQL contract is opt-in."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from buenapro_worker.prod2 import sevenzip_schema as subject


def constraint(*formats, **changes):
    return {'conname': subject.CONSTRAINT, 'convalidated': True,
            'definition': subject._definition(formats), **changes}


def test_format_schema_validation_is_exact_and_unknown_extra_constraints_fail():
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {'type': 'text', 'attnotnull': True}
    conn.execute.return_value.fetchall.return_value = [constraint('pdf', 'zip', 'ole', 'rar', '7z')]
    assert subject.validate_schema(conn) == '7z'
    conn.execute.return_value.fetchall.return_value = [constraint('pdf', 'zip', 'ole', 'rar')]
    assert subject.validate_schema(conn, allow_legacy=True) == 'legacy'
    with pytest.raises(ValueError):
        subject.validate_schema(conn)
    for rows in ([], [constraint('pdf', 'zip', 'ole', 'rar', '7z', 'exe')],
        [constraint('pdf', 'zip', 'ole', 'rar', '7z', convalidated=False)],
        [constraint('pdf', 'zip', 'ole', 'rar', '7z'), constraint('pdf')]):
        conn.execute.return_value.fetchall.return_value = rows
        with pytest.raises(ValueError):
            subject.validate_schema(conn, allow_legacy=True)


def test_prepare_dedicated_migration_blocked_by_active_coordinator_before_any_ddl():
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {'locked': False}
    with pytest.raises(RuntimeError, match='coordinator'):
        subject.prepare_schema(conn, Path(subject.MIGRATION))
    assert len(conn.execute.call_args_list) == 1
    assert 'pg_try_advisory_xact_lock' in conn.execute.call_args.args[0]


def test_prepare_replay_checks_contract_without_loading_or_executing_migration(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [{'locked': True}, {'version': subject.MIGRATION}]
    validate = MagicMock()
    monkeypatch.setattr(subject, 'validate_schema', validate)
    path = MagicMock(spec=Path)
    path.name = subject.MIGRATION
    assert subject.prepare_schema(conn, path)['status'] == 'sevenzip_download_schema_ready'
    validate.assert_called_once_with(conn)
    path.read_text.assert_not_called()
    assert not any('INSERT INTO schema_migrations' in call.args[0] for call in conn.execute.call_args_list)


def test_prepare_legacy_contract_then_migration_then_verified_ledger(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [{'locked': True}, None]
    validate = MagicMock(side_effect=['legacy', '7z'])
    monkeypatch.setattr(subject, 'validate_schema', validate)
    path = MagicMock(spec=Path)
    path.name = subject.MIGRATION
    path.read_text.return_value = 'ONLY_0042_SQL'
    assert subject.prepare_schema(conn, path)['migration'] == subject.MIGRATION
    validate.assert_any_call(conn, allow_legacy=True)
    validate.assert_any_call(conn)
    assert any(call.args == ('ONLY_0042_SQL',) for call in conn.execute.call_args_list)
    assert conn.execute.call_args.args == ('INSERT INTO schema_migrations(version) VALUES (%s)', (subject.MIGRATION,))


def test_prepare_already_changed_contract_without_ledger_does_not_reapply_ddl(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [{'locked': True}, None]
    monkeypatch.setattr(subject, 'validate_schema', MagicMock(return_value='7z'))
    path = MagicMock(spec=Path)
    path.name = subject.MIGRATION
    subject.prepare_schema(conn, path)
    path.read_text.assert_not_called()
    assert 'INSERT INTO schema_migrations' in conn.execute.call_args.args[0]


def test_preparation_rejects_other_migration_before_database():
    conn = MagicMock()
    with pytest.raises(ValueError, match='dedicated'):
        subject.prepare_schema(conn, Path('0036_prod2_pilot_enrichment.sql'))
    conn.execute.assert_not_called()


@pytest.mark.parametrize('response', [None, {'ready': False}])
def test_read_only_gate_blocks_without_ledger_table(response):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = response
    with pytest.raises(RuntimeError, match='migration0042'):
        subject.require_schema(conn)
    assert len(conn.execute.call_args_list) == 1


def test_read_only_gate_validates_applied_contract(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [{'ready': True}, {'version': subject.MIGRATION}]
    validate = MagicMock()
    monkeypatch.setattr(subject, 'validate_schema', validate)
    subject.require_schema(conn)
    validate.assert_called_once_with(conn)
    assert all(not any(word in call.args[0] for word in ('INSERT', 'ALTER', 'CREATE'))
               for call in conn.execute.call_args_list)
