from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.cli import build_parser
from buenapro_worker.jobs.refresh_schedule import (
    persist_schedule, refresh_schedules, schedule_instant, validate_schedule,
)
from buenapro_worker.jobs.poll_search import canonical_hash
from buenapro_worker.settings import Settings


def settings(**overrides):
    return Settings(database_url="postgresql://unused", gemini_api_key="unused",
                    _env_file=None, **overrides)


def test_no_invented_clock_and_strict_calendar():
    assert schedule_instant("01/10/2026") is None
    assert schedule_instant("31/02/2026", "09:00") is None
    assert schedule_instant("01/10/2026", "09:05").isoformat() == "2026-10-01T14:05:00+00:00"
    assert schedule_instant("2026-10-01T09:05:00-05:00").hour == 14


@pytest.mark.parametrize("payload", [{}, {"listaCronograma": None}, {"listaCronograma": []}, {"listaCronograma": ["bad"]}])
def test_missing_or_empty_schedule_does_not_clear_stored_data(payload):
    with pytest.raises(ValueError):
        validate_schedule(payload, "prod4", 1)


def test_prod6_identity_guard():
    with pytest.raises(ValueError, match="identity"):
        validate_schedule({"uitContratoCompletoProjection": {"idContrato": 2},
                           "uitContratoEtapaProjectionList": [{}]}, "prod6", 1)


def test_prod6_preserves_metadata_and_coherent_hash():
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"raw_detail_json": {"existing": "keep"}}
    stages = [{"idEtapaContrato": 2, "fecIni": "01/10/2026 09:00:00", "fecFin": "02/10/2026"}]
    persist_schedule(conn, "prod6", 1, stages)
    sql, params = conn.execute.call_args.args
    assert "detail_fetched_at" not in sql and "pipeline_state" not in sql
    assert params[2] == canonical_hash({"existing": "keep", "uitContratoEtapaProjectionList": stages})
    assert params[3].hour == 14 and params[4] is None


def test_prod4_retains_all_native_stages_and_missing_clock():
    conn = MagicMock()
    stages = [{"nombreEtapa": "PRESENTACIÓN DE OFERTAS", "fechaInicio": "01/10/2026",
               "horaInicio": "09:00", "fechaFin": "02/10/2026"},
              {"nombreEtapa": "INTEGRACIÓN DE BASES", "fechaInicio": "30/09/2026"}]
    persist_schedule(conn, "prod4", 4, stages)
    calls = conn.execute.call_args_list
    assert len(calls) == 5  # row lock, header, delete, two stages
    sql, params = calls[1].args
    assert "detail_fetched_at" not in sql and params[2] is None
    assert "INTEGRACIÓN DE BASES" == calls[-1].args[1][3]
    assert calls[-1].args[1][4] is None  # preserved day-only in raw_json


def test_dry_run_does_not_write_or_open_transactions():
    conn = MagicMock(autocommit=True)
    conn.execute.return_value.fetchall.return_value = [{"id": 1}]
    client = MagicMock()
    client.detail.return_value = {"listaCronograma": [{"nombreEtapa": "CONSULTAS"}]}
    result = refresh_schedules(settings(), conn,
                               source="prod4", prod4_client=client)
    assert result["sources"]["prod4"]["validated"] == 1
    assert result["sources"]["prod4"]["updated"] == 0
    assert conn.execute.call_count == 1
    conn.transaction.assert_not_called()


def test_failure_keeps_previous_schedule_and_continues_bounded_run():
    conn = MagicMock(autocommit=True)
    conn.execute.return_value.fetchall.return_value = [{"id": 1}, {"id": 2}]
    conn.transaction.side_effect = lambda: nullcontext()
    client = MagicMock()
    client.detail.side_effect = [{}, {"listaCronograma": [{"nombreEtapa": "CONSULTAS"}]}]
    result = refresh_schedules(settings(), conn,
                               source="prod4", limit=2, apply=True, prod4_client=client)
    assert result["sources"]["prod4"]["updated"] == 1
    assert result["sources"]["prod4"]["failed"] == [{"id": 1, "error": "ValueError"}]
    assert conn.transaction.call_count == 1


def test_cli_defaults_read_only():
    args = build_parser().parse_args(["schedule-refresh"])
    assert args.apply is False and args.source == "both" and args.limit == 100


def test_rejects_unbounded_or_ambiguous_scope():
    conn = MagicMock(autocommit=True)
    configuration = settings()
    with pytest.raises(ValueError):
        refresh_schedules(configuration, conn, limit=0)
    with pytest.raises(ValueError):
        refresh_schedules(configuration, conn, ids=[1])
    conn.execute.assert_not_called()


def test_automatic_refresh_is_explicit_opt_in():
    from buenapro_worker.jobs.scheduler import enqueue_scheduled_jobs
    repo = MagicMock()
    enqueue_scheduled_jobs(settings(), repo, anio=2026)
    assert not any(call.args[0] == "refresh_schedules" for call in repo.enqueue.call_args_list)
    repo.reset_mock()
    enqueue_scheduled_jobs(settings(schedule_refresh_enabled=True), repo, anio=2026)
    calls = [call for call in repo.enqueue.call_args_list if call.args[0] == "refresh_schedules"]
    assert len(calls) == 1 and calls[0].kwargs["queue_name"] == "io"


def test_schedule_interval_defaults_minutes_and_legacy_hours_compatibility():
    assert settings().schedule_refresh_interval_seconds == 1800
    assert settings(schedule_refresh_interval_hours=24).schedule_refresh_interval_seconds == 86400
    assert settings(schedule_refresh_interval_minutes=30, schedule_refresh_interval_hours=24).schedule_refresh_interval_seconds == 1800
    with pytest.raises(ValueError):
        settings(schedule_refresh_interval_minutes=0)


def test_scheduler_refreshes_at_start_and_every_thirty_minutes_with_one_stable_key():
    from buenapro_worker.jobs.scheduler import run_scheduler_forever
    repo = MagicMock()
    with patch("buenapro_worker.jobs.scheduler.time.monotonic", side_effect=[100, 1899, 1900]), \
         patch("buenapro_worker.jobs.scheduler.time.sleep", side_effect=[None, None, StopIteration]):
        with pytest.raises(StopIteration):
            run_scheduler_forever(settings(schedule_refresh_enabled=True), lambda: nullcontext(repo), anio=2026)
    calls = [call for call in repo.enqueue.call_args_list if call.args[0] == "refresh_schedules"]
    assert len(calls) == 2
    assert all(call.kwargs["dedup_key"] == "refresh_schedules" for call in calls)
    assert all(call.kwargs["queue_name"] == "io" and call.args[1] == {} for call in calls)


def test_prod6_scope_uses_text_segments_and_active_filter():
    conn = MagicMock(autocommit=True)
    conn.execute.return_value.fetchall.return_value = []
    refresh_schedules(settings(), conn, source="prod6", prod6_client=MagicMock())
    sql, params = conn.execute.call_args.args
    assert params[0] == ["43", "81"] and params[1] is False
    assert "objeto_codigo IN (1, 2)" in sql and "estado_codigo = 2" in sql
