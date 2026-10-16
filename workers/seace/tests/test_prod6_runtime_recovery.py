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


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_inaccessible_download_is_held_without_retry_and_other_contract_continues(offline_runtime, capsys, status):
    h = offline_runtime
    blocked = Job(1, "download_file", "io", {"id_contrato": 42,
        "id_contrato_archivo": 31, "batch_id": "original"}, 1)
    other = Job(2, "process_contract", "io", {"id_contrato": 43, "batch_id": "original"}, 1)
    local = Job(3, "derive_summary", "llm", {"id_contrato": 43, "batch_id": "original"}, 1)
    h.queues(io=[blocked, other], serial=[local])

    def execute(settings, profile_id, job, receipts):
        if job.id == 1:
            raise http_failure(status)
        return {"files_changed": True}

    h.execute_io.side_effect = execute
    assert h.run(once=True) == 2
    assert h.execute_io.call_count == 2
    assert h.dispatch.call_count == 1 and h.dispatch.call_args.args[2].id == 3
    state = h.states[-1]
    assert state["status"] == "sweep_blocked"
    assert state["done"] == {"process_contract": 1, "derive_summary": 1}
    assert state["blocked_documents"] == [{"job_id": 1, "contract_id": 42,
        "file_id": 31, "http_status": status, "reason": "manual_review_required"}]
    assert "deferred_io" not in state
    holds = [(call.args[0], call.args[1]) for call in h.conn.execute.call_args_list
             if "run_after='infinity'" in call.args[0]]
    assert len(holds) == 1
    sql, values = holds[0]
    assert "status='pending'" in sql and "status='claimed'" in sql and "claimed_by=%s" in sql
    assert "payload->>'source_runtime'=%s" in sql and "payload->>'scope_profile_id'=%s" in sql
    assert "interval" not in sql and "now()" not in sql
    assert json.loads(values[0]) == {"source_document_block": {
        "http_status": status, "reason": "manual_review_required"}}
    assert values[2:] == (1, "prod6-only-v1:veyon", runtime.NAMESPACE, "veyon")
    h.wait.assert_not_called()
    assert not (h.folder / "readings").exists()
    assert not list(h.folder.rglob("*started.private.json"))
    assert SECRET not in json.dumps(h.states) + str(values) + capsys.readouterr().out


@pytest.mark.parametrize("kind", ["download_file", "extract_tdr"])
def test_held_source_document_suppresses_new_batches_in_same_archive_contract_scope(monkeypatch, kind):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"id": 77}
    repo = runtime.ScopedRepository(conn, profile_id="veyon", batch_id="different-new-batch")
    enqueue = MagicMock(return_value=99)
    monkeypatch.setattr(runtime.JobRepository, "enqueue", enqueue)
    assert repo.enqueue(kind, {"id_contrato": 42, "id_contrato_archivo": 31,
                              "scope_profile_id": "foreign", "source_runtime": "foreign"}) is None
    enqueue.assert_not_called()
    assert repo.suppressed == {"source_document_blocked": 1}
    sql, values = conn.execute.call_args.args
    assert values == (runtime.NAMESPACE, "veyon", "42", "31")
    assert "status='pending'" in sql and "run_after='infinity'::timestamptz" in sql
    for field in ["source_runtime", "scope_profile_id", "id_contrato", "id_contrato_archivo"]:
        assert f"payload->>'{field}'=%s" in sql
    assert "payload->'source_document_block'->>'reason'='manual_review_required'" in sql
    assert "batch_id" not in sql


def test_document_hold_does_not_suppress_another_file_contract_or_profile(monkeypatch):
    conn = MagicMock()

    def execute(sql, values):
        cursor = MagicMock()
        cursor.fetchone.return_value = ({"id": 77} if values ==
            (runtime.NAMESPACE, "veyon", "42", "31") else None)
        return cursor

    conn.execute.side_effect = execute
    enqueue = MagicMock(return_value=99)
    monkeypatch.setattr(runtime.JobRepository, "enqueue", enqueue)
    repo = runtime.ScopedRepository(conn, profile_id="veyon", batch_id="new-batch")
    assert repo.enqueue("download_file", {"id_contrato": 42, "id_contrato_archivo": 31}) is None
    assert repo.enqueue("download_file", {"id_contrato": 42, "id_contrato_archivo": 32}) == 99
    assert repo.enqueue("extract_tdr", {"id_contrato": 43, "id_contrato_archivo": 31}) == 99
    another_profile = runtime.ScopedRepository(conn, profile_id="another-profile", batch_id="new-batch")
    assert another_profile.enqueue("download_file", {"id_contrato": 42, "id_contrato_archivo": 31}) == 99
    before = conn.execute.call_count
    assert repo.enqueue("process_contract", {"id_contrato": 42}) == 99
    assert conn.execute.call_count == before
    assert enqueue.call_count == 4 and repo.suppressed == {"source_document_blocked": 1}


def test_cycle_with_only_suppressed_document_is_blocked_even_without_claim(offline_runtime, monkeypatch):
    h = offline_runtime
    h.conn.execute.return_value.fetchone.return_value = {"locked": True, "id": 77}
    enqueue = MagicMock(return_value=99)
    monkeypatch.setattr(runtime.JobRepository, "enqueue", enqueue)

    def discover(settings, repo, **kwargs):
        assert repo.enqueue("download_file", {"id_contrato": 42, "id_contrato_archivo": 31}) is None
        return dict(SOURCE_STATS)

    h.poll.side_effect = discover
    assert h.run(once=True) == 2
    state = h.states[-1]
    assert state["status"] == "sweep_blocked"
    assert state["suppressed"] == {"source_document_blocked": 1}
    assert "blocked_documents" not in state
    enqueue.assert_not_called()
    h.execute_io.assert_not_called()
    h.dispatch.assert_not_called()
    h.wait.assert_not_called()


def test_403_for_process_contract_remains_fatal_not_a_document_hold(offline_runtime):
    h = offline_runtime
    h.queues(io=[Job(1, "process_contract", "io", {"id_contrato": 42, "batch_id": "original"}, 1)])
    h.execute_io.side_effect = http_failure(403)
    assert h.run(once=False) == 2
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["io_errors"] == [{"job_id": 1, "error_type": "HTTPStatusError", "http_status": 403}]
    assert "blocked_documents" not in h.states[-1]
    h.dispatch.assert_not_called()
    h.wait.assert_not_called()


def test_lost_ownership_does_not_claim_to_have_held_the_document(offline_runtime):
    h = offline_runtime
    h.conn.execute.return_value.rowcount = 0
    h.queues(io=[Job(1, "download_file", "io", {"id_contrato": 42,
        "id_contrato_archivo": 31, "batch_id": "original"}, 1)])
    h.execute_io.side_effect = http_failure(403)
    assert h.run(once=False) == 2
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["error_type"] == "RuntimeError"
    assert "blocked_documents" not in h.states[-1]
    h.dispatch.assert_not_called()
    h.wait.assert_not_called()
