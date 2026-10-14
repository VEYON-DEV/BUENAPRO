"""Retired operation must stop before credentials, DB, HTTP or paid models."""
from argparse import Namespace
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker import cli, local_prod4
from buenapro_worker.jobs.scheduler import enqueue_scheduled_jobs, run_scheduler_forever
from buenapro_worker.prod4.retirement import Prod4WorkerRetiredError
from buenapro_worker.queue.repository import Job
from buenapro_worker.queue.runner import QueueRunner
from buenapro_worker.settings import Settings


RETIRED_JOBS = (
    "poll_prod4", "extract_prod4_document", "route_prod4_profiles", "analyze_prod4_match",
)


def settings(**overrides):
    return Settings(database_url="postgresql://unused", gemini_api_key="unused",
                    _env_file=None, **overrides)


def test_prod4_defaults_off(monkeypatch):
    monkeypatch.delenv("PROD4_ENABLED", raising=False)
    assert settings().prod4_enabled is False


def test_legacy_env_cannot_restore_scheduler_or_handlers(monkeypatch):
    monkeypatch.setenv("PROD4_ENABLED", "true")
    configuration = settings()
    repo = MagicMock()
    enqueue_scheduled_jobs(configuration, repo, anio=2026)
    assert not any(call.args[0] in RETIRED_JOBS for call in repo.enqueue.call_args_list)
    with patch("buenapro_worker.queue.runner.QueueRunner") as runner:
        cli.command_run(Namespace(queues=["io", "llm", "match"]), configuration)
        handlers = runner.call_args.args[1]
        assert not (set(handlers) & set(RETIRED_JOBS))
        assert {"poll_search", "extract_tdr", "analyze_match"} <= set(handlers)


def test_repeated_scheduler_ignores_even_explicit_prod4_flag():
    repo = MagicMock()
    with patch("buenapro_worker.jobs.scheduler.time.monotonic", return_value=100_000), \
         patch("buenapro_worker.jobs.scheduler.time.sleep", side_effect=InterruptedError):
        with pytest.raises(InterruptedError):
            run_scheduler_forever(settings(prod4_enabled=True), lambda: nullcontext(repo), anio=2026)
    assert repo.enqueue.called
    assert not any(call.args[0] in RETIRED_JOBS for call in repo.enqueue.call_args_list)


@pytest.mark.parametrize("command", ["prod4-poll-once", "prod4-document-sweep"])
def test_cli_rejects_legacy_command_before_reading_settings(command):
    with patch("sys.argv", ["buenapro-worker", command]), patch.object(cli, "Settings") as config:
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        config.assert_not_called()


@pytest.mark.parametrize("source", ["prod4", "both"])
def test_cli_manual_refresh_cannot_restore_prod4(source):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["schedule-refresh", "--source", source, "--apply"])
    with patch("buenapro_worker.db.connection.connect") as connect:
        with pytest.raises(Prod4WorkerRetiredError):
            cli.command_schedule_refresh(Namespace(source=source), settings(prod4_enabled=True))
        connect.assert_not_called()


@pytest.mark.parametrize("operation", [
    cli.command_prod4_poll_once, cli.command_prod4_document_sweep,
    cli._handle_poll_prod4, cli._handle_extract_prod4_document,
    cli._handle_route_prod4_profiles, cli._handle_analyze_prod4_match,
])
def test_direct_operational_entry_is_also_retired(operation):
    with patch("buenapro_worker.db.connection.connect") as connect:
        with pytest.raises(Prod4WorkerRetiredError):
            operation(None, None)
        connect.assert_not_called()


@pytest.mark.parametrize("job_type", RETIRED_JOBS)
def test_old_queued_job_becomes_terminal_without_handler_retry(job_type):
    with patch("buenapro_worker.queue.runner.connect"), \
         patch("buenapro_worker.queue.runner.JobRepository") as repository:
        repo = repository.return_value
        repo.claim_next.return_value = Job(1, job_type, "llm", {}, 1)
        assert QueueRunner(settings(), {}).run_once()
        repo.dead.assert_called_once()
        repo.fail.assert_not_called()
        repo.complete.assert_not_called()


def test_refresh_handler_uses_prod6_even_with_legacy_flag():
    with patch("buenapro_worker.db.connection.connect"), \
         patch("buenapro_worker.jobs.refresh_schedule.refresh_schedules",
               return_value={"sources": {"prod6": {"failed": []}}}) as refresh:
        cli._handle_refresh_schedules(settings(prod4_enabled=True), None)
        assert refresh.call_args.kwargs["source"] == "prod6"


@pytest.mark.parametrize("flags", [[], ["--execute"], ["--execute", "--route-matches"],
                                   ["--execute", "--daily-limit", "100", "--max-cost-usd", "100"]])
def test_local_manual_modes_never_open_tunnel_or_model(flags):
    with patch("sys.argv", ["local_prod4", "--ssh-host", "unused", "--ssh-key", "unused", *flags]), \
         patch.object(local_prod4, "production_settings") as remote, \
         patch.object(local_prod4, "run") as run, \
         patch.object(local_prod4, "route_matches") as route:
        with pytest.raises(SystemExit) as error:
            local_prod4.main()
        assert error.value.code == 2
        remote.assert_not_called()
        run.assert_not_called()
        route.assert_not_called()


@pytest.mark.parametrize("operation", [local_prod4.run, local_prod4.route_matches])
def test_direct_local_runner_cannot_bypass_retirement(operation):
    with patch.object(local_prod4.psycopg, "connect") as connect:
        with pytest.raises(Prod4WorkerRetiredError):
            operation(None, None)
        connect.assert_not_called()


def test_direct_settings_context_never_reads_remote_credentials():
    with patch.object(local_prod4.subprocess, "run") as ssh, \
         patch.object(local_prod4.subprocess, "Popen") as tunnel:
        with pytest.raises(Prod4WorkerRetiredError):
            with local_prod4.production_settings(None):
                raise AssertionError("Retired context must not enter")
        ssh.assert_not_called()
        tunnel.assert_not_called()
