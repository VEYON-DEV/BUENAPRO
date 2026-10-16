"""Offline regression guards for free source recovery and paid fail-closed paths."""
from collections import deque
import copy
import json
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from buenapro_worker.prod6 import runtime
from buenapro_worker.prod6.reading import ReadingBlockedError
from buenapro_worker.queue.repository import Job


SECRET = "synthetic-private-token"
SOURCE_STATS = {"pages": 1, "capped_segments": 0, "limit_reached": 0}


def http_failure(status):
    request = httpx.Request("GET", f"https://example.invalid/private?token={SECRET}",
                            headers={"x-private": SECRET})
    response = httpx.Response(status, request=request, text=f"private body {SECRET}")
    return httpx.HTTPStatusError(f"private error {SECRET}", request=request, response=response)


@pytest.fixture
def offline_runtime(monkeypatch, tmp_path):
    """All database, source HTTP and dispatch boundaries are mocked before run."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"locked": True}
    conn.execute.return_value.rowcount = 1
    connection_context = MagicMock()
    connection_context.__enter__.return_value = conn
    connect = MagicMock(return_value=connection_context)
    monkeypatch.setattr(runtime, "connect", connect)
    monkeypatch.setattr(runtime, "profile_enabled", MagicMock(return_value=True))
    callbacks = {}
    monkeypatch.setattr(runtime.signal, "signal", lambda number, callback: callbacks.__setitem__(number, callback))
    poll = MagicMock(return_value=dict(SOURCE_STATS))
    dispatch = MagicMock(return_value={"outcome": "current_reading_preserved"})
    execute_io = MagicMock(return_value={"files_changed": True})
    claim = MagicMock(return_value=None)
    wait = MagicMock()
    monkeypatch.setattr(runtime, "poll_prod6_profile", poll)
    monkeypatch.setattr(runtime, "dispatch", dispatch)
    monkeypatch.setattr(runtime, "execute_io_job", execute_io)
    monkeypatch.setattr(runtime.ScopedRepository, "claim_scoped", claim)
    monkeypatch.setattr(runtime, "_wait_for_poll", wait)
    states = []
    real_write_state = runtime.write_state

    def write_state(path, value):
        states.append(copy.deepcopy(value))
        real_write_state(path, value)

    monkeypatch.setattr(runtime, "write_state", write_state)

    def run(*, once=True):
        return runtime.run(MagicMock(), profile_id="veyon", receipt_dir=tmp_path,
                           once=once, interval_seconds=1800, io_workers=2)

    def stop():
        callbacks[signal.SIGTERM](signal.SIGTERM, None)

    def queues(io=(), serial=()):
        io_jobs, serial_jobs = deque(io), deque(serial)

        def next_job(worker_id, job_types=runtime.JOB_TYPES):
            queue = io_jobs if job_types == runtime.IO_TYPES else serial_jobs
            return queue.popleft() if queue else None

        claim.side_effect = next_job

    return SimpleNamespace(conn=conn, connect=connect, poll=poll, dispatch=dispatch,
        execute_io=execute_io, claim=claim, wait=wait, states=states, run=run,
        stop=stop, queues=queues, folder=tmp_path)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_only_explicit_temporary_http_statuses_are_recoverable(status):
    assert runtime.free_http_failure(http_failure(status)) == {
        "error_type": "HTTPStatusError", "http_status": status,
    }


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501])
def test_permanent_http_errors_do_not_authorize_polling_retries(status):
    assert runtime.free_http_failure(http_failure(status)) is None


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError])
def test_remote_transport_failures_are_recoverable_without_exception_content(exception):
    error = exception(f"private {SECRET}")
    assert runtime.free_http_failure(error) == {"error_type": exception.__name__}


@pytest.mark.parametrize("error", [httpx.LocalProtocolError(SECRET), httpx.UnsupportedProtocol(SECRET),
                                    ValueError(SECRET), ReadingBlockedError(SECRET)])
def test_integrity_configuration_and_payment_guards_are_not_free_http_failures(error):
    assert runtime.free_http_failure(error) is None


def test_transient_discovery_waits_then_completes_a_later_cycle(offline_runtime, capsys):
    h = offline_runtime
    h.poll.side_effect = [http_failure(503), dict(SOURCE_STATS)]
    waits = []

    def wait(should_stop, interval_seconds):
        assert interval_seconds == 1800 and not should_stop()
        waits.append(interval_seconds)
        if len(waits) == 2:
            h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False) == 0
    assert h.poll.call_count == 2 and len(waits) == 2
    backoff = next(state for state in h.states if state["status"] == "source_backoff")
    assert backoff["source_error"] == {"error_type": "HTTPStatusError", "http_status": 503}
    assert backoff["retry_after_seconds"] == 1800
    assert any(state["status"] == "sweep_drained" for state in h.states)
    h.dispatch.assert_not_called()
    h.execute_io.assert_not_called()
    assert SECRET not in json.dumps(h.states) + capsys.readouterr().out


def test_one_shot_discovery_backoff_is_incomplete_not_success(offline_runtime):
    h = offline_runtime
    h.poll.side_effect = http_failure(429)
    assert h.run(once=True) == 2
    assert h.poll.call_count == 1
    h.wait.assert_not_called()
    h.claim.assert_not_called()
    assert h.states[-1]["status"] == "source_backoff"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_nonrecoverable_discovery_stops_with_status_but_no_private_details(offline_runtime, capsys, status):
    h = offline_runtime
    h.poll.side_effect = http_failure(status)
    assert h.run(once=False) == 2
    h.wait.assert_not_called()
    h.claim.assert_not_called()
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["source_error"] == {"error_type": "HTTPStatusError", "http_status": status}
    assert SECRET not in json.dumps(h.states) + capsys.readouterr().out


def test_free_io_is_deferred_once_with_ownership_and_other_work_preserved(offline_runtime, capsys):
    h = offline_runtime
    failing = Job(1, "download_file", "io", {"id_contrato": 42, "batch_id": "original"}, 1)
    succeeding = Job(2, "process_contract", "io", {"id_contrato": 43, "batch_id": "original"}, 1)
    local = Job(3, "derive_summary", "llm", {"id_contrato": 43, "batch_id": "original"}, 1)
    h.queues(io=[failing, succeeding], serial=[local])

    def execute(settings, profile_id, job, receipts):
        if job.id == 1:
            raise http_failure(503)
        return {"files_changed": True}

    h.execute_io.side_effect = execute
    assert h.run(once=True) == 2
    assert h.execute_io.call_count == 2
    assert h.dispatch.call_count == 1 and h.dispatch.call_args.args[2].id == 3
    assert h.dispatch.call_args.args[1].batch_id == "original"
    state = h.states[-1]
    assert state["status"] == "sweep_deferred"
    assert state["done"] == {"process_contract": 1, "derive_summary": 1}
    assert state["deferred_io"] == [{"job_id": 1, "job_type": "download_file",
        "error_type": "HTTPStatusError", "http_status": 503, "retry_after_seconds": 1800}]
    deferrals = [(call.args[0], call.args[1]) for call in h.conn.execute.call_args_list
                 if "run_after=now()" in call.args[0]]
    assert len(deferrals) == 1
    sql, values = deferrals[0]
    assert "claimed_by=%s" in sql and "payload->>'source_runtime'=%s" in sql
    assert "payload->>'scope_profile_id'=%s" in sql
    assert values[0] == 1800 and values[2:] == (1, "prod6-only-v1:veyon", runtime.NAMESPACE, "veyon")
    h.wait.assert_not_called()
    assert SECRET not in json.dumps(h.states) + str(values) + capsys.readouterr().out


def test_mixed_io_transient_and_integrity_failure_settle_then_stop(offline_runtime):
    h = offline_runtime
    h.queues(io=[Job(i, "download_file", "io", {"id_contrato": i, "batch_id": "batch"}, 1)
                 for i in (1, 2)])

    def execute(settings, profile_id, job, receipts):
        raise http_failure(503) if job.id == 1 else ValueError(SECRET)

    h.execute_io.side_effect = execute
    assert h.run(once=True) == 2 and h.execute_io.call_count == 2
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["io_errors"] == [{"job_id": 2, "error_type": "ValueError"}]
    assert h.states[-1]["deferred_io"][0]["job_id"] == 1
    h.dispatch.assert_not_called()
    h.wait.assert_not_called()


@pytest.mark.parametrize("kind,error", [("extract_tdr", http_failure(503)),
    ("extract_tdr", ReadingBlockedError(SECRET)), ("derive_summary", http_failure(503))])
def test_serial_or_paid_stage_never_uses_free_http_retry_policy(offline_runtime, kind, error):
    h = offline_runtime
    h.queues(serial=[Job(1, kind, "llm", {"id_contrato": 42, "batch_id": "original"}, 1)])
    h.dispatch.side_effect = error
    assert h.run(once=False) == 2 and h.dispatch.call_count == 1
    h.wait.assert_not_called()
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["job_type"] == kind
    assert "deferred_io" not in h.states[-1]


def test_changed_io_ownership_blocks_recovery_before_any_further_work(offline_runtime):
    h = offline_runtime
    h.conn.execute.return_value.rowcount = 0
    h.queues(io=[Job(1, "download_file", "io", {"id_contrato": 42, "batch_id": "batch"}, 1)])
    h.execute_io.side_effect = http_failure(503)
    assert h.run(once=False) == 2
    h.dispatch.assert_not_called()
    h.wait.assert_not_called()
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["error_type"] == "RuntimeError"
