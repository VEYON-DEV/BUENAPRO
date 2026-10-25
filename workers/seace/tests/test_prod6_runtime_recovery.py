"""Offline regression guards for free source recovery and paid fail-closed paths."""
from collections import deque
import copy
from datetime import datetime, timedelta, timezone
import json
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest

from buenapro_worker.prod6 import runtime
from buenapro_worker.prod6.reading import GuardedReadingError, ProviderRejectedError, ReadingBlockedError
from buenapro_worker.seace import pacing
from buenapro_worker.queue.repository import Job


REAL_JEV_CYCLE = runtime.jev_cycle  # offline_runtime replaces it with a stub
SECRET = "synthetic-private-token"
SOURCE_STATS = {"pages": 1, "capped_segments": 0, "limit_reached": 0}


def http_failure(status):
    request = httpx.Request("GET", f"https://example.invalid/private?token={SECRET}",
                            headers={"x-private": SECRET})
    response = httpx.Response(status, request=request, text=f"private body {SECRET}")
    return httpx.HTTPStatusError(f"private error {SECRET}", request=request, response=response)


@pytest.fixture
def offline_runtime(monkeypatch, tmp_path):
    """All database, source HTTP and dispatch boundaries are mocked before run.

    The search interval is 0 here (the search runs in every cycle, as before it existed);
    the scheduling tests pass their own interval.
    """
    monkeypatch.setenv("PROD6_DISCOVERY_INTERVAL_SECONDS", "0")
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
    jev = MagicMock(return_value={"status": "ok"})
    monkeypatch.setattr(runtime, "poll_prod6_profiles", poll)
    monkeypatch.setattr(runtime, "jev_cycle", jev)
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

    def run(*, once=True, **options):
        return runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=tmp_path,
                           once=once, interval_seconds=1800, io_workers=2, **options)

    def stop():
        callbacks[signal.SIGTERM](signal.SIGTERM, None)

    def queues(io=(), serial=()):
        io_jobs, serial_jobs = deque(io), deque(serial)

        def next_job(worker_id, job_types=runtime.JOB_TYPES):
            queue = io_jobs if job_types == runtime.IO_TYPES else serial_jobs
            return queue.popleft() if queue else None

        claim.side_effect = next_job

    return SimpleNamespace(conn=conn, connect=connect, poll=poll, jev=jev, dispatch=dispatch,
        execute_io=execute_io, claim=claim, wait=wait, states=states, run=run,
        stop=stop, queues=queues, folder=tmp_path)


@pytest.mark.parametrize("error", [psycopg.OperationalError(SECRET),
                                  psycopg.errors.AdminShutdown(SECRET)])
def test_initial_profile_db_failure_is_recorded_before_source_or_paid_work(
        offline_runtime, monkeypatch, capsys, error):
    h = offline_runtime
    enabled = MagicMock(side_effect=error)
    monkeypatch.setattr(runtime, "profile_enabled", enabled)
    assert h.run(once=False) == 2
    enabled.assert_called_once()
    h.connect.assert_called_once()
    h.poll.assert_not_called()
    h.claim.assert_not_called()
    h.dispatch.assert_not_called()
    h.execute_io.assert_not_called()
    h.wait.assert_not_called()
    state = json.loads((h.folder / "state.json").read_text())
    assert state["status"] == "attention_required"
    assert state["phase"] == "profile_check"
    assert state["error_type"] == type(error).__name__
    assert state["failed_at"] >= state["started_at"]
    if error.sqlstate:
        assert state["sqlstate"] == "57P01"
    else:
        assert "sqlstate" not in state
    assert json.loads((h.folder / f"{state['batch_id']}.json").read_text()) == state
    assert not (h.folder / "readings").exists()
    assert SECRET not in json.dumps(h.states) + capsys.readouterr().out


@pytest.mark.parametrize("failure_at,phase", [
    ("connect", "database_connect"), ("setup", "database_connect"),
    ("lock", "coordinator_lock"), ("recovery", "claim_recovery"),
])
def test_startup_db_failure_preserves_other_coordinator_state_without_retry(
        offline_runtime, monkeypatch, capsys, failure_at, phase):
    h = offline_runtime
    active_state = b'{"status":"processing","owner":"another-coordinator"}\n'
    (h.folder / "state.json").write_bytes(active_state)
    error = psycopg.errors.AdminShutdown(SECRET)
    if failure_at == "connect":
        h.connect.return_value.__enter__.side_effect = error
    elif failure_at == "setup":
        h.conn.commit.side_effect = error
    elif failure_at == "lock":
        h.conn.execute.side_effect = error
    else:
        cursor = MagicMock()
        cursor.fetchone.return_value = {"locked": True}
        h.conn.execute.side_effect = [cursor, error]
    enabled = MagicMock()
    monkeypatch.setattr(runtime, "profile_enabled", enabled)
    assert h.run(once=False) == 2
    h.connect.assert_called_once()
    enabled.assert_not_called()
    h.poll.assert_not_called()
    h.claim.assert_not_called()
    h.dispatch.assert_not_called()
    h.execute_io.assert_not_called()
    h.wait.assert_not_called()
    assert (h.folder / "state.json").read_bytes() == active_state
    receipts = list(h.folder.glob("startup-*.json"))
    assert len(receipts) == 1
    state = json.loads(receipts[0].read_text())
    assert state["status"] == "attention_required" and state["phase"] == phase
    assert state["sqlstate"] == "57P01"
    assert state["error_type"] == "AdminShutdown" and state["failed_at"]
    assert not (h.folder / "readings").exists()
    assert SECRET not in json.dumps(h.states) + capsys.readouterr().out


def test_db_failure_after_a_completed_cycle_never_polls_or_claims_again(
        offline_runtime, monkeypatch):
    h = offline_runtime
    enabled = MagicMock(side_effect=[True, True, psycopg.OperationalError(SECRET)])
    monkeypatch.setattr(runtime, "profile_enabled", enabled)
    assert h.run(once=False) == 2
    assert any(state["status"] == "sweep_drained" for state in h.states)
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["phase"] == "profile_check"
    assert h.poll.call_count == 1 and h.wait.call_count == 1
    h.connect.assert_called_once()
    h.dispatch.assert_not_called()
    h.execute_io.assert_not_called()


def test_lock_contention_still_blocks_recovery_and_keeps_existing_state(offline_runtime):
    h = offline_runtime
    h.conn.execute.return_value.fetchone.return_value = {"locked": False}
    active_state = b'{"status":"processing","owner":"another-coordinator"}\n'
    (h.folder / "state.json").write_bytes(active_state)
    with pytest.raises(RuntimeError, match="Another PROD6 coordinator"):
        h.run(once=False)
    assert h.conn.execute.call_count == 1
    h.poll.assert_not_called()
    h.claim.assert_not_called()
    h.dispatch.assert_not_called()
    assert (h.folder / "state.json").read_bytes() == active_state
    assert not list(h.folder.glob("startup-*.json"))


@pytest.mark.parametrize("code", [SECRET, "57p01", "57P01\n", "５７Ｐ０１", None])
def test_failure_diagnostic_discards_invalid_sqlstate_and_private_messages(code):
    class InvalidStateError(psycopg.OperationalError):
        sqlstate = code

    assert runtime.failure_diagnostic(InvalidStateError(SECRET)) == {
        "error_type": "InvalidStateError",
    }


@pytest.mark.parametrize("status", [408, 425, 500, 502, 503, 504])
def test_only_explicit_temporary_http_statuses_are_recoverable(status):
    assert runtime.free_http_failure(http_failure(status)) == {
        "error_type": "HTTPStatusError", "http_status": status,
    }


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 501])
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
    h.poll.side_effect = http_failure(503)
    assert h.run(once=True) == 2
    assert h.poll.call_count == 1
    h.wait.assert_not_called()
    h.claim.assert_not_called()
    assert h.states[-1]["status"] == "source_backoff"


@pytest.mark.parametrize("status", [401, 404])
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


@pytest.mark.parametrize("status", [401, 404, 410])
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


@pytest.mark.parametrize("status", [401, 403])
def test_lost_ownership_does_not_claim_to_have_held_the_document(offline_runtime, status):
    h = offline_runtime
    h.conn.execute.return_value.rowcount = 0
    h.queues(io=[Job(1, "download_file", "io", {"id_contrato": 42,
        "id_contrato_archivo": 31, "batch_id": "original"}, 1)])
    h.execute_io.side_effect = http_failure(status)
    assert h.run(once=False) == 2
    assert h.states[-1]["status"] == "attention_required"
    assert h.states[-1]["error_type"] == "RuntimeError"
    assert "blocked_documents" not in h.states[-1]
    h.dispatch.assert_not_called()
    h.wait.assert_not_called()


def proxy_failure():
    request = httpx.Request("GET", "https://prod6.seace.gob.pe/v1/private")
    return httpx.ProxyError("502 Bad Gateway", request=request)


@pytest.fixture
def egress(monkeypatch):
    """Scripted egress checks (False = proxy down) and recorded ops alerts."""
    outcomes, alerts = [], []

    def check():
        if outcomes and outcomes.pop(0) is False:
            raise runtime.EgressUnavailable("proxy unreachable")

    monkeypatch.setattr(runtime, "check_egress", check)
    monkeypatch.setattr(runtime, "notify_ops", alerts.append)
    return SimpleNamespace(outcomes=outcomes, alerts=alerts)


def test_egress_check_failure_backs_off_before_any_source_call_and_alerts_once(offline_runtime, egress):
    h = offline_runtime
    egress.outcomes[:] = [False, False, True]
    waits = []

    def wait(should_stop, interval_seconds):
        waits.append(interval_seconds)
        if len(waits) == 3:
            h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False) == 0
    backoffs = [state for state in h.states if state["status"] == "egress_backoff"]
    assert backoffs and all(state["egress_error"] == "EgressUnavailable" and state["retry_after_seconds"] == 1800
                            for state in backoffs)
    assert not any(state["status"] in {"source_backoff", "attention_required"} for state in h.states)
    assert h.poll.call_count == 1  # Only the cycle after the proxy came back.
    assert len(egress.alerts) == 2 and "unavailable" in egress.alerts[0] and "recovered" in egress.alerts[1]


def test_one_shot_egress_check_failure_is_incomplete_without_source_work(offline_runtime, egress):
    h = offline_runtime
    egress.outcomes[:] = [False]
    assert h.run(once=True) == 2
    h.poll.assert_not_called()
    h.claim.assert_not_called()
    h.wait.assert_not_called()
    assert h.states[-1]["status"] == "egress_backoff"
    assert len(egress.alerts) == 1


def test_proxy_error_in_discovery_is_egress_backoff_not_source_backoff(offline_runtime, egress):
    h = offline_runtime
    h.poll.side_effect = [proxy_failure(), dict(SOURCE_STATS)]
    waits = []

    def wait(should_stop, interval_seconds):
        waits.append(interval_seconds)
        if len(waits) == 2:
            h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False) == 0
    backoff = next(state for state in h.states if state["status"] == "egress_backoff")
    assert backoff["egress_error"] == "ProxyError" and "source_error" not in backoff
    assert not any(state["status"] == "source_backoff" for state in h.states)
    assert len(egress.alerts) == 2 and "recovered" in egress.alerts[1]


def test_io_egress_failure_defers_without_manual_review_and_stops_further_io(offline_runtime, egress):
    h = offline_runtime
    failing = Job(1, "download_file", "io", {"id_contrato": 42, "id_contrato_archivo": 31, "batch_id": "b"}, 1)
    same_batch = Job(2, "process_contract", "io", {"id_contrato": 43, "batch_id": "b"}, 1)
    later = Job(3, "download_file", "io", {"id_contrato": 44, "id_contrato_archivo": 32, "batch_id": "b"}, 1)
    h.queues(io=[failing, same_batch, later])
    h.execute_io.side_effect = proxy_failure()
    assert h.run(once=True) == 2
    assert h.execute_io.call_count == 2  # io_workers=2: the third job is not claimed during the outage.
    state = h.states[-1]
    assert state["status"] == "egress_backoff"
    assert sorted((row["job_id"], row["job_type"], row["error_type"]) for row in state["egress_deferred"]) == [
        (1, "download_file", "ProxyError"), (2, "process_contract", "ProxyError")]
    assert not {"blocked_documents", "deferred_io", "io_errors"} & set(state)
    deferrals = [call.args for call in h.conn.execute.call_args_list if "run_after=now()" in call.args[0]]
    holds = [call for call in h.conn.execute.call_args_list if "run_after='infinity'" in call.args[0]]
    assert len(deferrals) == 2 and deferrals[0][1][0] == 1800 and holds == []
    assert len(egress.alerts) == 1


def test_download_403_with_the_proxy_configured_is_a_source_block_not_an_egress_outage(
        offline_runtime, egress, monkeypatch):
    monkeypatch.setenv("SEACE_EGRESS_PROXY", "http://10.161.181.75:8888")
    h = offline_runtime
    h.queues(io=[Job(1, "download_file", "io", {"id_contrato": 42, "id_contrato_archivo": 31, "batch_id": "b"}, 1)])
    h.execute_io.side_effect = http_failure(403)
    assert h.run(once=True) == 2
    state = h.states[-1]
    assert state["status"] == "source_blocked" and state["source_block"]["status"] == 403
    assert "egress_deferred" not in state and "blocked_documents" not in state
    assert len(egress.alerts) == 1 and "HTTP 403" in egress.alerts[0]


@pytest.mark.parametrize("error", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
def test_plain_network_errors_without_a_proxy_keep_the_source_backoff(offline_runtime, egress, error):
    h = offline_runtime
    h.poll.side_effect = error
    assert h.run(once=True) == 2
    assert h.states[-1]["status"] == "source_backoff" and egress.alerts == []


def unavailable_jev(h, monkeypatch, *, key=None):
    """Run the real JEV step with a missing key; storage and the provider must stay untouched."""
    monkeypatch.setattr(runtime, "jev_cycle", REAL_JEV_CYCLE)
    monkeypatch.setattr(runtime, "jev_api_key", lambda: key)
    scored, admitted = MagicMock(), MagicMock()
    monkeypatch.setattr(runtime, "score_pending", scored)
    monkeypatch.setattr(runtime, "admit_accepted", admitted)
    return scored, admitted


def test_missing_jev_key_admits_nothing_records_state_and_alerts_once(offline_runtime, egress, monkeypatch):
    h = offline_runtime
    scored, admitted = unavailable_jev(h, monkeypatch)
    waits = []

    def wait(should_stop, interval_seconds):
        waits.append(interval_seconds)
        if len(waits) == 2:
            h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False) == 0
    scored.assert_not_called()
    admitted.assert_not_called()
    assert h.poll.call_count == 2  # Discovery keeps recording decisions.
    final = h.states[-1]
    assert final["status"] == "jev_unavailable"
    assert final["jev"] == {"status": "jev_unavailable", "reason": "missing_api_key"}
    assert len(egress.alerts) == 1 and "JEV" in egress.alerts[0] and "missing_api_key" in egress.alerts[0]
    assert json.loads((h.folder / "state.json").read_text())["status"] == "jev_unavailable"


def test_one_shot_run_without_a_jev_key_exits_incomplete(offline_runtime, egress, monkeypatch):
    h = offline_runtime
    unavailable_jev(h, monkeypatch)
    assert h.run(once=True) == 2
    assert h.states[-1]["status"] == "jev_unavailable"


def test_jev_recovery_alerts_once_and_the_rest_of_the_cycle_still_runs(offline_runtime, egress, monkeypatch):
    h = offline_runtime
    unavailable_jev(h, monkeypatch)
    h.queues(io=[Job(1, "process_contract", "io", {"id_contrato": 1, "batch_id": "b"}, 1)])
    waits = []

    def wait(should_stop, interval_seconds):
        waits.append(interval_seconds)
        if len(waits) == 1:
            monkeypatch.setattr(runtime, "jev_cycle", MagicMock(return_value={"status": "ok"}))
        else:
            h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False) == 0
    assert h.execute_io.call_count == 1  # Unavailable JEV did not abort the IO processing.
    assert len(egress.alerts) == 2 and "recovered" in egress.alerts[1]


def test_jev_cycle_scores_then_admits_new_and_recovered_acceptances_together(monkeypatch):
    repo = MagicMock()
    stats = {"requests": 2, "accepted": 1, "rejected": 1, "failed": 0, "aborted": False}
    scored = MagicMock(return_value=(stats, [{"id_contrato": 1}]))
    admitted = MagicMock(return_value={"admitted": 2})
    client = MagicMock()
    monkeypatch.setattr(runtime, "jev_api_key", lambda: "test-key")
    monkeypatch.setattr(runtime, "JevScreeningClient", MagicMock(return_value=client))
    monkeypatch.setattr(runtime, "score_pending", scored)
    monkeypatch.setattr(runtime, "unadmitted_accepted", MagicMock(return_value=[{"id_contrato": 2}]))
    monkeypatch.setattr(runtime, "admit_accepted", admitted)
    result = REAL_JEV_CYCLE(repo, ["a", "b"], max_requests=7, threshold=65, batch_id="batch")
    assert result["status"] == "ok" and result["admission"] == {"admitted": 2}
    assert scored.call_args.args[1] is client.__enter__.return_value
    assert scored.call_args.kwargs == {"profile_ids": ["a", "b"], "threshold": 65, "max_requests": 7}
    assert admitted.call_args.args[1] == [{"id_contrato": 1}, {"id_contrato": 2}]


@pytest.mark.parametrize("stats, expected", [
    ({"failed": 3, "accepted": 0, "rejected": 0, "aborted": True}, "jev_unavailable"),
    ({"failed": 2, "accepted": 0, "rejected": 0, "aborted": False}, "jev_unavailable"),
    ({"failed": 2, "accepted": 1, "rejected": 0, "aborted": False}, "ok"),
    ({"failed": 0, "accepted": 0, "rejected": 0, "aborted": False}, "ok"),
])
def test_jev_cycle_reports_unavailable_only_when_nothing_could_be_scored(monkeypatch, stats, expected):
    monkeypatch.setattr(runtime, "jev_api_key", lambda: "test-key")
    monkeypatch.setattr(runtime, "JevScreeningClient", MagicMock())
    monkeypatch.setattr(runtime, "score_pending", MagicMock(return_value=(dict(stats), [])))
    monkeypatch.setattr(runtime, "unadmitted_accepted", MagicMock(return_value=[]))
    monkeypatch.setattr(runtime, "admit_accepted", MagicMock(return_value={}))
    assert REAL_JEV_CYCLE(MagicMock(), ["a"], max_requests=1, threshold=60, batch_id="b")["status"] == expected


def test_api_key_comes_from_the_environment_before_the_ignored_dotenv(monkeypatch):
    monkeypatch.setattr(runtime, "dotenv_values", lambda path: {"TYPESAFE_API_KEY": " from-file "})
    monkeypatch.setenv("TYPESAFE_API_KEY", " from-env ")
    assert runtime.jev_api_key() == "from-env"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert runtime.jev_api_key() == "from-file"
    monkeypatch.setattr(runtime, "dotenv_values", lambda path: {})
    assert runtime.jev_api_key() is None


def test_several_profiles_share_one_search_sweep_and_are_scored_together(offline_runtime):
    h = offline_runtime
    h.poll.return_value = {"pages": 5, "capped_segments": 1, "limit_reached": 0, "http_requests": 5,
        "by_profile": {"veyon": {"seen": 4, "decisions_recorded": 4},
                       "din": {"seen": 9, "decisions_recorded": 9}},
        "cursor": {"next_stream_index": 0, "next_stream": [1, 43], "stream_count": 4, "complete": True}}
    assert runtime.run(MagicMock(), profile_ids=["veyon", "din"], receipt_dir=h.folder, once=True,
                       max_jev_requests=9, jev_threshold=70) == 0
    h.poll.assert_called_once()  # One sweep, not one per company.
    assert h.poll.call_args.kwargs["profile_ids"] == ["veyon", "din"]
    repo, active = h.jev.call_args.args[:2]
    assert repo.profile_id == "veyon" and active == ["veyon", "din"]  # The first profile scopes the queue.
    assert h.jev.call_args.kwargs["max_requests"] == 9 and h.jev.call_args.kwargs["threshold"] == 70
    state = json.loads((h.folder / "state.json").read_text())
    assert state["profile_ids"] == ["veyon", "din"]
    assert state["source"]["pages"] == 5 and state["source"]["capped_segments"] == 1
    assert "by_profile" not in state["source"] and "cursor" not in state["source"]
    assert state["source_by_profile"]["din"] == {"seen": 9, "decisions_recorded": 9}
    assert state["discovery"]["next_stream_index"] == 0 and state["discovery"]["max_requests"] == 24


def test_a_paused_extra_profile_is_neither_polled_nor_scored(offline_runtime, monkeypatch):
    h = offline_runtime
    monkeypatch.setattr(runtime, "profile_enabled", lambda repo, profile_id=None: profile_id != "din")
    assert runtime.run(MagicMock(), profile_ids=["veyon", "din", "veyon"], receipt_dir=h.folder, once=True) == 0
    assert h.poll.call_args.kwargs["profile_ids"] == ["veyon"]
    assert h.jev.call_args.args[1] == ["veyon"]


def test_profile_option_is_repeatable_and_single_use_still_works(monkeypatch, tmp_path):
    run = MagicMock(return_value=0)
    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "Settings", MagicMock())
    for arguments, expected in ((["--profile-id", "a"], ["a"]),
                                (["--profile-id", "a", "--profile-id", "b"], ["a", "b"])):
        monkeypatch.setattr("sys.argv", ["prod6-runtime", *arguments, "--receipt-dir", str(tmp_path)])
        with pytest.raises(SystemExit):
            runtime.main()
        assert run.call_args.kwargs["profile_ids"] == expected
        assert run.call_args.kwargs["max_jev_requests"] == 200 and run.call_args.kwargs["jev_threshold"] == 60
    monkeypatch.setattr("sys.argv", ["prod6-runtime", "--profile-id", "a", "--receipt-dir", str(tmp_path),
                                     "--max-jev-requests", "5", "--jev-threshold", "75"])
    with pytest.raises(SystemExit):
        runtime.main()
    assert run.call_args.kwargs["max_jev_requests"] == 5 and run.call_args.kwargs["jev_threshold"] == 75


@pytest.mark.parametrize("options", [{"max_jev_requests": -1}, {"jev_threshold": 101}, {"jev_threshold": -1}])
def test_invalid_jev_limits_are_rejected_before_the_database(options, tmp_path):
    with pytest.raises(ValueError):
        runtime.run(MagicMock(), profile_ids=["a"], receipt_dir=tmp_path, **options)


# --- Slow pace, request budget and the 403/429 circuit breaker -----------------------

def requesting_poll(stats=SOURCE_STATS, requests=1):
    """A discovery stand-in that asks the pacer for its slots like the real client does."""
    def poll(*_args, **_kwargs):
        for _ in range(requests):
            pacing.PACER.acquire()
        return dict(stats)
    return poll


def denied_download(status=403):
    """What the client does on a denial: open the breaker, then raise the HTTP error."""
    def execute(settings, profile_id, job, receipts):
        pacing.PACER.trip(status)
        raise http_failure(status)
    return execute


def deferrals(h):
    return [call.args for call in h.conn.execute.call_args_list if "attempts=GREATEST" in call.args[0]]


def holds(h):
    return [call.args for call in h.conn.execute.call_args_list if "run_after='infinity'" in call.args[0]]


def poll_job(job_id, kind="download_file", **payload):
    base = {"id_contrato": 40 + job_id, "batch_id": "b", **payload}
    if kind != "process_contract":
        base.setdefault("id_contrato_archivo", 30 + job_id)
    return Job(job_id, kind, "io" if kind in runtime.IO_TYPES else "llm", base, 1)


@pytest.mark.parametrize("status", [403, 429])
def test_search_denial_opens_the_breaker_alerts_once_and_keeps_local_work(offline_runtime, egress, status):
    h = offline_runtime
    h.poll.side_effect = http_failure(status)
    h.queues(io=[poll_job(1, "process_contract")], serial=[poll_job(3, "derive_summary")])
    assert h.run(once=True) == 2
    state = h.states[-1]
    assert state["status"] == "source_blocked"
    pause = datetime.fromisoformat(state["source_blocked_until"]) - datetime.fromisoformat(state["started_at"])
    assert timedelta(seconds=10800) <= pause < timedelta(seconds=10805)
    assert state["source_block"]["status"] == status and f"HTTP {status}" in state["source_block"]["reason"]
    assert "attention_required" not in {saved["status"] for saved in h.states}
    h.jev.assert_called_once()  # Pending JEV screening is local work and goes on.
    h.execute_io.assert_not_called()  # IO needs PROD6.
    assert all(call.args[1:] != (runtime.IO_TYPES,) for call in h.claim.call_args_list)  # IO never claimed
    assert h.dispatch.call_count == 1 and h.dispatch.call_args.args[2].id == 3
    assert len(egress.alerts) == 1 and f"HTTP {status}" in egress.alerts[0]
    assert json.loads((h.folder / "state.json").read_text())["source_blocked_until"] == state["source_blocked_until"]
    assert pacing.PACER.blocked


def test_partial_discovery_that_hit_a_denial_also_opens_the_breaker(offline_runtime, egress):
    h = offline_runtime
    h.poll.side_effect = [dict(SOURCE_STATS, source_blocked=1)]
    assert h.run(once=True) == 2
    state = h.states[-1]
    assert state["status"] == "source_blocked" and state["source"]["source_blocked"] == 1
    assert state["source_coverage"] == "bounded_partial"
    assert len(egress.alerts) == 1


def test_the_blocked_sweep_keeps_the_rotation_for_after_the_pause(offline_runtime, egress):
    h = offline_runtime
    h.poll.side_effect = [dict(SOURCE_STATS, source_blocked=1, cursor={
        "next_stream_index": 3, "next_stream": [2, 43], "stream_count": 4, "complete": False})]
    assert runtime.run(MagicMock(), profile_ids=["veyon", "din"], receipt_dir=h.folder, once=True) == 2
    assert h.poll.call_count == 1
    h.poll.side_effect = None
    h.poll.return_value = dict(SOURCE_STATS)
    assert runtime.run(MagicMock(), profile_ids=["veyon", "din"], receipt_dir=h.folder, once=True) == 2
    h.poll.assert_called_once()  # Paused: no sweep, and the position survives in state.json.
    assert json.loads((h.folder / "state.json").read_text())["discovery"]["next_stream_index"] == 3


def sweep(index, *, complete=False, **extra):
    return dict(SOURCE_STATS, http_requests=5, cursor={"next_stream_index": index,
        "next_stream": [1, 43], "stream_count": 6, "complete": complete}, **extra)


def test_each_cycle_resumes_the_sweep_where_the_last_one_stopped_even_after_a_restart(offline_runtime):
    h = offline_runtime
    h.poll.side_effect = [sweep(4, discovery_limit_reached=1), sweep(0, complete=True), sweep(2)]
    assert h.run(once=True) == 0
    assert h.poll.call_args.kwargs["start_stream"] == 0
    state = json.loads((h.folder / "state.json").read_text())
    assert state["discovery"]["next_stream_index"] == 4
    assert state["source_coverage"] == "bounded_partial"
    assert h.run(once=True) == 0  # A new process reads the position from state.json.
    assert h.poll.call_args.kwargs["start_stream"] == 4
    assert h.run(once=True) == 0
    assert h.poll.call_args.kwargs["start_stream"] == 0  # A full pass starts over.
    assert json.loads((h.folder / "state.json").read_text())["discovery"]["next_stream_index"] == 2


def test_discovery_cannot_take_the_whole_cycle_budget(offline_runtime):
    h = offline_runtime
    assert h.run(once=True) == 0
    assert h.poll.call_args.kwargs["max_requests"] == 24 and h.poll.call_args.kwargs["max_pages_per_segment"] == 5
    runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=h.folder, once=True,
                max_source_requests=40, max_discovery_requests=10, max_pages=3)
    assert h.poll.call_args.kwargs["max_requests"] == 10
    runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=h.folder, once=True,
                max_source_requests=8)  # Never more than the cycle itself allows.
    assert h.poll.call_args.kwargs["max_requests"] == 8


def test_discovery_leaves_the_rest_of_the_budget_to_io(offline_runtime):
    h = offline_runtime
    # The sweep spent its 24 requests of 40; two free jobs of three requests each still fit.
    h.poll.side_effect = requesting_poll(dict(SOURCE_STATS, discovery_limit_reached=1), requests=24)
    h.queues(io=[poll_job(1, "process_contract"), poll_job(2, "process_contract")])
    assert h.run(once=True) == 0
    assert h.execute_io.call_count == 2
    assert h.states[-1]["source_requests"]["used"] == 24 and h.states[-1]["status"] == "sweep_drained"
    assert h.states[-1]["source"]["discovery_limit_reached"] == 1


@pytest.mark.parametrize("options", [{"max_discovery_requests": 0}, {"max_discovery_requests": 4, "max_pages": 5}])
def test_invalid_discovery_limit_is_rejected_before_the_database(options, tmp_path):
    with pytest.raises(ValueError, match="discovery"):
        runtime.run(MagicMock(), profile_ids=["a"], receipt_dir=tmp_path, **options)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake.now if tz else fake.now.replace(tzinfo=None)

    monkeypatch.setattr(runtime, "datetime", FakeDatetime)
    return fake


def run_cycles(h, clock, cycles, *, step=1800, **options):
    waits = []

    def wait(should_stop, interval_seconds):
        waits.append(interval_seconds)
        clock.now += timedelta(seconds=step)
        if len(waits) == cycles:
            h.stop()

    h.wait.side_effect = wait
    return h.run(once=False, **options)


def test_no_prod6_request_until_the_pause_ends_then_one_probe_and_one_recovery_alert(
        offline_runtime, egress, clock):
    h = offline_runtime
    ok = requesting_poll()

    def poll(*args, **kwargs):
        if h.poll.call_count == 1:
            raise http_failure(403)
        return ok()

    h.poll.side_effect = poll
    h.queues(io=[poll_job(1, "process_contract")])
    assert run_cycles(h, clock, 7) == 0  # Seven cycles, thirty minutes apart: the pause is three hours.
    assert h.poll.call_count == 2  # The first (denied) and the probe; none in cycles 2-6.
    statuses = [state["status"] for state in h.states if state["status"] in {"source_blocked", "sweep_drained"}]
    assert statuses.count("source_blocked") >= 6 and statuses[-1] == "sweep_drained"
    assert h.jev.call_count == 7  # Local screening ran every cycle.
    assert h.execute_io.call_count == 1  # The IO job only after the pause ended.
    assert len(egress.alerts) == 2 and "blocked" in egress.alerts[0] and "recovered" in egress.alerts[1]
    final = json.loads((h.folder / "state.json").read_text())
    assert "source_blocked_until" not in final and "source_block" not in final


def test_a_denied_probe_extends_the_pause_without_a_new_alert(offline_runtime, egress, clock):
    h = offline_runtime
    ok = requesting_poll()

    def poll(*args, **kwargs):
        if h.poll.call_count <= 2:
            raise http_failure(403)
        return ok()

    h.poll.side_effect = poll
    assert run_cycles(h, clock, 13) == 0
    assert h.poll.call_count == 3  # Denied, denied probe three hours later, probe after a second pause.
    assert len(egress.alerts) == 2 and "recovered" in egress.alerts[1]


def test_the_pause_survives_a_restart_and_a_bad_state_file_is_ignored(offline_runtime, egress):
    h = offline_runtime
    h.poll.side_effect = http_failure(403)
    assert h.run(once=True) == 2
    until = json.loads((h.folder / "state.json").read_text())["source_blocked_until"]
    h.poll.reset_mock(side_effect=True)
    h.poll.return_value = dict(SOURCE_STATS)
    h.states.clear()
    assert h.run(once=True) == 2  # A new process, same receipt folder.
    h.poll.assert_not_called()
    assert h.states[-1]["status"] == "source_blocked" and h.states[-1]["source_blocked_until"] == until
    assert len(egress.alerts) == 1  # No second alert for the same block.
    # Once the recorded time has passed the same file lets the probe through.
    saved = json.loads((h.folder / "state.json").read_text())
    saved["source_block"]["until"] = saved["source_blocked_until"] = "2020-01-01T00:00:00+00:00"
    (h.folder / "state.json").write_text(json.dumps(saved))
    h.poll.side_effect = requesting_poll()
    assert h.run(once=True) == 0
    assert h.poll.call_count == 1 and len(egress.alerts) == 2 and "recovered" in egress.alerts[1]


def test_load_source_block_reads_only_valid_records(tmp_path):
    path = tmp_path / "state.json"
    assert runtime.load_source_block(path) is None
    path.write_text("not json")
    assert runtime.load_source_block(path) is None
    path.write_text(json.dumps({"source_block": {"until": "tomorrow"}}))
    assert runtime.load_source_block(path) is None
    path.write_text(json.dumps({"source_blocked_until": "2026-10-24T15:00:00+00:00"}))
    assert runtime.load_source_block(path)["until"] == "2026-10-24T15:00:00+00:00"
    path.write_text(json.dumps({"source_block": {"until": "2026-10-24T15:00:00+00:00", "status": 429}}))
    assert runtime.load_source_block(path)["status"] == 429


def test_io_denial_returns_jobs_to_pending_after_the_pause_and_counts_only_its_own_denial(
        offline_runtime, egress):
    h = offline_runtime
    denied = poll_job(1, "download_file")
    refused = poll_job(2, "process_contract", source_block_count=1)
    never_started = poll_job(3, "download_file")
    h.queues(io=[denied, refused, never_started], serial=[poll_job(4, "derive_summary")])

    def execute(settings, profile_id, job, receipts):
        if job.id == 1:
            return denied_download()(settings, profile_id, job, receipts)
        raise pacing.SourceBlocked(403)  # The breaker refused it before any request.

    h.execute_io.side_effect = execute
    assert h.run(once=True) == 2
    assert h.execute_io.call_count == 2  # Job 3 is never claimed while the source is blocked.
    state = h.states[-1]
    assert state["status"] == "source_blocked" and not {"io_errors", "blocked_documents"} & set(state)
    until = datetime.fromisoformat(state["source_blocked_until"])
    by_job = {args[1][3]: args for args in deferrals(h)}
    assert set(by_job) == {1, 2}
    for job_id, count in ((1, {"source_block_count": 1}), (2, {})):
        sql, values = by_job[job_id]
        assert values[0] == until and json.loads(values[1]) == count
        assert "status='pending'" in sql and "status='claimed'" in sql and "claimed_by=%s" in sql
        assert "payload->>'source_runtime'=%s" in sql and "infinity" not in sql
        assert values[4:] == ("prod6-only-v1:veyon", runtime.NAMESPACE, "veyon")
    assert holds(h) == []
    assert sorted(item["job_id"] for item in state["source_deferred"]) == [1, 2]
    assert state["done"] == {"derive_summary": 1}  # Local work after the block still runs.
    assert len(egress.alerts) == 1


@pytest.mark.parametrize("kind", ["download_file", "process_contract"])
def test_third_denial_of_the_same_document_goes_to_manual_review(offline_runtime, egress, kind):
    h = offline_runtime
    h.queues(io=[poll_job(1, kind, source_block_count=2)])
    h.execute_io.side_effect = denied_download()
    assert h.run(once=True) == 2
    assert deferrals(h) == []
    (sql, values), = holds(h)
    assert json.loads(values[0]) == {"source_document_block": {
        "http_status": 403, "reason": "manual_review_required"}, "source_block_count": 3}
    assert "interval" not in sql and values[2:] == (1, "prod6-only-v1:veyon", runtime.NAMESPACE, "veyon")
    state = h.states[-1]
    assert state["status"] == "source_blocked"  # The source itself stays paused.
    assert state["blocked_documents"][0]["job_id"] == 1 and state["blocked_documents"][0]["http_status"] == 403
    assert len(egress.alerts) == 1


def test_second_denial_still_waits_for_the_pause(offline_runtime, egress):
    h = offline_runtime
    h.queues(io=[poll_job(1, "download_file", source_block_count=1)])
    h.execute_io.side_effect = denied_download()
    assert h.run(once=True) == 2
    assert holds(h) == [] and json.loads(deferrals(h)[0][1][1]) == {"source_block_count": 2}


@pytest.mark.parametrize("error,patch", [(http_failure(403), {"source_block_count": 1}),
                                         (pacing.SourceBlocked(403), {})])
def test_reading_that_needs_a_new_download_is_deferred_not_failed_or_counted_as_an_attempt(
        offline_runtime, egress, error, patch):
    h = offline_runtime
    h.queues(serial=[poll_job(1, "extract_tdr")])
    h.dispatch.side_effect = error
    assert h.run(once=True) == 2
    state = h.states[-1]
    assert state["status"] == "source_blocked" and "job_type" not in state
    (sql, values), = deferrals(h)
    assert "attempts=GREATEST(attempts-1,0)" in sql  # The paid-attempt guard must not see a try.
    assert json.loads(values[1]) == patch and values[3] == 1
    assert datetime.fromisoformat(state["source_blocked_until"]) == values[0]


def test_other_serial_failures_still_stop_the_cycle(offline_runtime):
    h = offline_runtime
    h.queues(serial=[poll_job(1, "derive_summary")])
    h.dispatch.side_effect = http_failure(403)  # derive_summary never talks to PROD6.
    assert h.run(once=True) == 2
    assert h.states[-1]["status"] == "attention_required"


def test_budget_ends_discovery_without_error_and_leaves_io_for_the_next_cycle(offline_runtime, egress):
    h = offline_runtime
    h.poll.side_effect = requesting_poll(dict(SOURCE_STATS, budget_reached=1), requests=3)
    h.queues(io=[poll_job(1, "process_contract")], serial=[poll_job(2, "derive_summary")])
    assert runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=h.folder, once=True,
                       max_source_requests=3) == 0
    state = h.states[-1]
    assert state["status"] == "source_budget_reached" and state["source_coverage"] == "bounded_partial"
    assert state["source_requests"] == {"used": 3, "max": 3, "budget_reached": True}
    assert "attention_required" not in {saved["status"] for saved in h.states}
    h.execute_io.assert_not_called()
    assert h.dispatch.call_count == 1  # Local work is not limited by the budget.
    assert egress.alerts == []


def test_a_job_that_would_not_fit_in_the_remaining_budget_is_not_started(offline_runtime):
    h = offline_runtime
    h.poll.side_effect = requesting_poll(requests=3)  # Leaves 2 of 5: a contract needs 3.
    h.queues(io=[poll_job(1, "process_contract")])
    assert runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=h.folder, once=True,
                       max_source_requests=5) == 0
    h.execute_io.assert_not_called()
    assert h.states[-1]["status"] == "source_budget_reached"
    assert h.states[-1]["source_requests"] == {"used": 3, "max": 5, "budget_reached": True}


def test_free_budget_runs_io_and_reports_usage(offline_runtime):
    h = offline_runtime
    h.queues(io=[poll_job(1, "process_contract")])
    assert h.run(once=True) == 0
    h.execute_io.assert_called_once()
    assert h.states[-1]["status"] == "sweep_drained"
    assert h.states[-1]["source_requests"] == {"used": 0, "max": 40, "budget_reached": False}
    assert h.states[-1]["source_pacing"] == {"min_interval_seconds": 0.0, "max_requests": 40}


def test_reading_refused_by_a_spent_budget_waits_for_the_next_cycle(offline_runtime, egress):
    h = offline_runtime
    h.queues(serial=[poll_job(1, "extract_tdr")])
    h.dispatch.side_effect = pacing.SourceBudgetReached()
    assert h.run(once=True) == 0
    state = h.states[-1]
    assert state["status"] == "source_budget_reached" and "source_blocked_until" not in state
    (sql, values), = deferrals(h)
    assert json.loads(values[1]) == {} and values[0] > datetime.now(timezone.utc) + timedelta(minutes=29)
    assert egress.alerts == [] and not pacing.PACER.blocked


def test_request_budget_comes_from_the_argument_then_the_environment(offline_runtime, monkeypatch):
    h = offline_runtime
    monkeypatch.setenv("PROD6_MAX_SOURCE_REQUESTS", "7")
    assert h.run(once=True) == 0
    assert h.states[-1]["source_pacing"]["max_requests"] == 7 and pacing.PACER.max_requests == 7
    assert runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=h.folder, once=True,
                       max_source_requests=5) == 0
    assert h.states[-1]["source_pacing"]["max_requests"] == 5


@pytest.mark.parametrize("options", [{"max_source_requests": 0}, {"max_source_requests": -3}])
def test_invalid_request_budget_is_rejected_before_the_database(options, tmp_path):
    with pytest.raises(ValueError):
        runtime.run(MagicMock(), profile_ids=["a"], receipt_dir=tmp_path, **options)


@pytest.mark.parametrize("name,value", [("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "-2"),
                                        ("PROD6_BLOCK_BACKOFF_SECONDS", "never"),
                                        ("PROD6_READING_BACKOFF_SECONDS", "0"),
                                        ("PROD6_MAX_SOURCE_REQUESTS", "0")])
def test_invalid_pacing_environment_is_rejected_before_the_database(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv(name, value)
    connect = MagicMock()
    monkeypatch.setattr(runtime, "connect", connect)
    with pytest.raises(ValueError, match=name):
        runtime.run(MagicMock(), profile_ids=["a"], receipt_dir=tmp_path)
    connect.assert_not_called()


def test_source_request_option_reaches_the_runtime(monkeypatch, tmp_path):
    run = MagicMock(return_value=0)
    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "Settings", MagicMock())
    for arguments, expected, discovery in (([], None, 24), (["--max-source-requests", "9",
                                             "--max-discovery-requests", "6"], 9, 6)):
        monkeypatch.setattr("sys.argv", ["prod6-runtime", "--profile-id", "a",
                                         "--receipt-dir", str(tmp_path), *arguments])
        with pytest.raises(SystemExit):
            runtime.main()
        assert run.call_args.kwargs["max_source_requests"] == expected
        assert run.call_args.kwargs["max_discovery_requests"] == discovery
        assert run.call_args.kwargs["max_pages"] == 5


def test_manual_review_removes_the_temporary_source_copy(offline_runtime):
    h = offline_runtime
    sha = "a" * 64
    copy = h.folder / "source-cache" / sha
    copy.parent.mkdir()
    copy.write_bytes(b"%PDF-1.7")
    h.conn.execute.return_value.fetchone.return_value = {"locked": True, "sha256_original": sha}
    h.queues(io=[poll_job(1, "download_file")])
    h.execute_io.side_effect = http_failure(404)
    assert h.run(once=True) == 2
    assert not copy.exists()
    assert holds(h)


def test_cleanup_runs_only_after_the_job_committed(offline_runtime):
    h = offline_runtime
    ran = []
    h.queues(serial=[poll_job(1, "extract_tdr"), poll_job(2, "extract_tdr")])

    def dispatch(settings, repo, job, receipts):
        repo.after_commit.append(lambda: ran.append(job.id))
        if job.id == 2:
            raise ValueError("local failure")
        return {"outcome": "read_or_reused"}

    h.dispatch.side_effect = dispatch
    assert h.run(once=True) == 2
    assert ran == [1]  # The failed job keeps its copy.


# --- Gemini HTTP errors, reading pause and stop alerts -----------------------------------

def typed_claims(h, jobs):
    """A queue that honors the job types a claim asks for; ``asked`` records the non-IO claims."""
    waiting, asked = list(jobs), []

    def next_job(worker_id, job_types=runtime.JOB_TYPES):
        if tuple(job_types) != runtime.IO_TYPES:
            asked.append(tuple(job_types))
        for job in waiting:
            if job.job_type in job_types:
                waiting.remove(job)
                return job
        return None

    h.claim.side_effect = next_job
    return asked


def dispatching(h, failing, outcome=None):
    """dispatch raises ``failing[job_id]`` (an exception) and otherwise answers ``outcome``."""
    def dispatch(settings, repo, job, receipts):
        if job.id in failing:
            raise failing[job.id]
        return dict(outcome or {"outcome": "read_or_reused"})

    h.dispatch.side_effect = dispatch


def reading_deferrals(h):
    return [(values[0], json.loads(values[1]), values[3]) for _, values in deferrals(h)]


ACCOUNT_ERRORS = [(401, "Gemini: llave inválida o sin permiso (401/403)"),
                  (402, "Gemini: créditos agotados (402)"),
                  (403, "Gemini: llave inválida o sin permiso (401/403)"),
                  (429, "Gemini: límite de uso (429)")]


@pytest.mark.parametrize("code,reason", ACCOUNT_ERRORS)
def test_gemini_account_error_pauses_paid_readings_without_penalizing_the_document(offline_runtime, egress, clock, code, reason):
    h = offline_runtime
    asked = typed_claims(h, [poll_job(1, "extract_tdr"), poll_job(2, "extract_tdr"),
                             poll_job(3, "derive_summary")])
    dispatching(h, {1: ProviderRejectedError(code, "RESOURCE_EXHAUSTED")})
    assert h.run(once=True) == 2
    until = clock.now + timedelta(seconds=3600)
    # Searching and scoring ran, the rejected job is back to pending, other work went on.
    h.poll.assert_called_once()
    h.jev.assert_called_once()
    assert [call.args[2].id for call in h.dispatch.call_args_list] == [1, 3]
    assert reading_deferrals(h) == [(until, {}, 1)]  # The document's count is untouched.
    assert not any("extract_tdr" in types for types in asked[1:])
    state = h.states[-1]
    assert state["status"] == "reading_paused" and state["done"] == {"derive_summary": 1}
    assert state["reading_pause"]["code"] == code and state["reading_pause"]["reason"] == reason
    assert datetime.fromisoformat(state["reading_paused_until"]) == until
    assert state["reading_deferred"][0]["http_status"] == code and "job_type" not in state
    assert state["reading_deferred"][0]["provider_error_count"] == 0
    assert json.loads((h.folder / "state.json").read_text())["reading_pause"]["reason"] == reason
    assert len(egress.alerts) == 1 and reason in egress.alerts[0]
    assert holds(h) == []


def test_pause_length_comes_from_the_environment(offline_runtime, egress, clock, monkeypatch):
    h = offline_runtime
    monkeypatch.setenv("PROD6_READING_BACKOFF_SECONDS", "120")
    h.queues(serial=[poll_job(1, "extract_tdr")])
    dispatching(h, {1: ProviderRejectedError(402, "RESOURCE_EXHAUSTED")})
    assert h.run(once=True) == 2
    assert reading_deferrals(h)[0][0] == clock.now + timedelta(seconds=120)


@pytest.mark.parametrize("code,status", [(400, "INVALID_ARGUMENT"), (404, "NOT_FOUND"),
                                         (413, None), (422, None), (500, "INTERNAL"), (503, "UNAVAILABLE")])
def test_document_or_transient_gemini_errors_only_reschedule_and_count_against_that_job(offline_runtime, egress, clock, code, status):
    h = offline_runtime
    asked = typed_claims(h, [poll_job(1, "extract_tdr"), poll_job(2, "extract_tdr")])
    dispatching(h, {1: ProviderRejectedError(code, status)})
    assert h.run(once=True) == 0
    assert reading_deferrals(h) == [(clock.now + timedelta(seconds=1800), {"provider_error_count": 1}, 1)]
    assert [call.args[2].id for call in h.dispatch.call_args_list] == [1, 2]  # The next document is read.
    assert all("extract_tdr" in types for types in asked)
    state = h.states[-1]
    assert state["status"] == "sweep_drained" and "reading_pause" not in state
    assert state["done"] == {"extract_tdr": 1}
    assert egress.alerts == []


def test_third_rejection_of_the_same_document_goes_to_manual_review_and_the_cycle_continues(
        offline_runtime, egress, clock):
    h = offline_runtime
    typed_claims(h, [poll_job(1, "extract_tdr", provider_error_count=2), poll_job(2, "derive_summary")])
    dispatching(h, {1: ProviderRejectedError(503, "UNAVAILABLE")})
    assert h.run(once=True) == 2
    assert deferrals(h) == []
    (sql, values), = holds(h)
    assert json.loads(values[0]) == {"source_document_block": {"provider_http_status": 503,
        "provider_status": "UNAVAILABLE", "reason": "manual_review_required"}, "provider_error_count": 3}
    assert "Gemini reading HTTP 503" in values[1] and values[2:] == (1, "prod6-only-v1:veyon", runtime.NAMESPACE, "veyon")
    state = h.states[-1]
    assert state["status"] == "sweep_blocked" and state["blocked_documents"][0]["job_id"] == 1
    assert state["done"] == {"derive_summary": 1}
    assert egress.alerts == []


@pytest.mark.parametrize("code,reason", ACCOUNT_ERRORS)
def test_repeated_account_errors_never_send_the_document_to_manual_review(offline_runtime, egress, clock, code, reason):
    h = offline_runtime
    for _ in range(3):  # Three probes in a row, each after the pause ended.
        typed_claims(h, [poll_job(1, "extract_tdr", provider_error_count=0)])
        dispatching(h, {1: ProviderRejectedError(code, "PERMISSION_DENIED")})
        h.run(once=True)
        clock.now += timedelta(seconds=3601)
    assert holds(h) == []
    assert [patch for _, patch, _ in reading_deferrals(h)] == [{}, {}, {}]
    assert len(egress.alerts) == 1 and reason in egress.alerts[0]
    # Even a document already near its limit is not held by an account error.
    typed_claims(h, [poll_job(2, "extract_tdr", provider_error_count=2)])
    dispatching(h, {2: ProviderRejectedError(code, "PERMISSION_DENIED")})
    h.run(once=True)
    assert holds(h) == []
    assert reading_deferrals(h)[-1][1] == {"provider_error_count": 2}


def test_document_errors_count_even_when_an_account_error_paused_readings_before(offline_runtime, egress, clock):
    h = offline_runtime
    typed_claims(h, [poll_job(1, "extract_tdr", provider_error_count=2)])
    dispatching(h, {1: ProviderRejectedError(400, "INVALID_ARGUMENT")})
    h.run(once=True)
    (_, values), = holds(h)
    assert json.loads(values[0])["provider_error_count"] == 3 and egress.alerts == []


def test_reading_pause_survives_a_restart_then_one_probe_and_one_recovery_alert(
        offline_runtime, egress, clock):
    h = offline_runtime
    typed_claims(h, [poll_job(1, "extract_tdr")])
    dispatching(h, {1: ProviderRejectedError(402, "RESOURCE_EXHAUSTED")})
    assert h.run(once=True) == 2
    clock.now += timedelta(seconds=1800)  # A new process, inside the pause.
    asked = typed_claims(h, [poll_job(1, "extract_tdr", provider_error_count=1)])
    assert h.run(once=True) == 2
    assert not any("extract_tdr" in types for types in asked)
    assert h.states[-1]["status"] == "reading_paused" and len(egress.alerts) == 1
    clock.now += timedelta(seconds=1801)  # The pause ended: the next reading is the probe.
    typed_claims(h, [poll_job(1, "extract_tdr", provider_error_count=1)])
    dispatching(h, {}, {"outcome": "read_or_reused", "provider_answered": True})
    assert h.run(once=True) == 0
    assert len(egress.alerts) == 2 and "reanudadas" in egress.alerts[1]
    final = json.loads((h.folder / "state.json").read_text())
    assert "reading_pause" not in final and "reading_paused_until" not in final
    assert final["status"] == "sweep_drained"


def test_failed_probe_renews_the_pause_without_a_new_alert(offline_runtime, egress, clock):
    h = offline_runtime
    typed_claims(h, [poll_job(1, "extract_tdr")])
    dispatching(h, {1: ProviderRejectedError(429, "RESOURCE_EXHAUSTED"),})
    h.run(once=True)
    clock.now += timedelta(seconds=3601)
    typed_claims(h, [poll_job(1, "extract_tdr", provider_error_count=1)])
    h.run(once=True)
    state = json.loads((h.folder / "state.json").read_text())
    assert datetime.fromisoformat(state["reading_paused_until"]) == clock.now + timedelta(seconds=3600)
    assert len(egress.alerts) == 1
    assert reading_deferrals(h)[-1][1] == {"provider_error_count": 1}


def test_a_reading_served_without_asking_gemini_does_not_end_the_pause(offline_runtime, egress, clock):
    h = offline_runtime
    typed_claims(h, [poll_job(1, "extract_tdr")])
    dispatching(h, {1: ProviderRejectedError(402, "RESOURCE_EXHAUSTED")})
    h.run(once=True)
    clock.now += timedelta(seconds=3601)
    typed_claims(h, [poll_job(2, "extract_tdr")])
    dispatching(h, {}, {"outcome": "read_or_reused"})  # Reused or replayed locally.
    h.run(once=True)
    assert len(egress.alerts) == 1
    assert "reading_pause" in json.loads((h.folder / "state.json").read_text())


def test_reading_pause_loader_reads_only_valid_records(tmp_path):
    path = tmp_path / "state.json"
    assert runtime.load_reading_pause(path) is None
    path.write_text("not json")
    assert runtime.load_reading_pause(path) is None
    path.write_text(json.dumps({"reading_pause": {"until": "soon"}}))
    assert runtime.load_reading_pause(path) is None
    path.write_text(json.dumps({"reading_pause": {"until": "2026-10-25T12:00:00+00:00", "code": 402}}))
    assert runtime.load_reading_pause(path)["code"] == 402
    assert runtime.load_attention_alert(path) is None
    path.write_text(json.dumps({"attention_alert": "GuardedReadingError|extract_tdr"}))
    assert runtime.load_attention_alert(path) == "GuardedReadingError|extract_tdr"
    path.write_text(json.dumps({"attention_alert": 5}))
    assert runtime.load_attention_alert(path) is None


def test_uncertain_reading_stops_the_service_and_alerts_once_per_stop(offline_runtime, egress):
    h = offline_runtime
    h.queues(serial=[poll_job(1, "extract_tdr")])
    h.dispatch.side_effect = GuardedReadingError(SECRET)
    assert h.run(once=False) == 2
    assert h.states[-1]["status"] == "attention_required"
    assert egress.alerts == ["BuenaPro PROD6: servicio detenido (attention_required): "
                             "GuardedReadingError en extract_tdr; requiere revisión."]
    assert json.loads((h.folder / "state.json").read_text())["attention_alert"] == "GuardedReadingError|extract_tdr"
    # Restarted and stopped by the same cause: no second alert.
    h.queues(serial=[poll_job(1, "extract_tdr")])
    assert h.run(once=False) == 2
    assert len(egress.alerts) == 1
    # Another cause is a new stop.
    h.queues(serial=[poll_job(1, "extract_tdr")])
    h.dispatch.side_effect = ValueError(SECRET)
    assert h.run(once=False) == 2
    assert len(egress.alerts) == 2 and "ValueError en extract_tdr" in egress.alerts[1]
    # A cycle that ends normally closes the episode, so the old cause alerts again.
    h.dispatch.side_effect = None
    h.queues(serial=[poll_job(1, "derive_summary")])
    assert h.run(once=True) == 0
    h.queues(serial=[poll_job(1, "extract_tdr")])
    h.dispatch.side_effect = GuardedReadingError(SECRET)
    assert h.run(once=True) == 2
    assert len(egress.alerts) == 3
    assert SECRET not in json.dumps(egress.alerts)


def test_io_failure_alerts_with_the_error_and_job_types(offline_runtime, egress):
    h = offline_runtime
    h.queues(io=[poll_job(1, "download_file")])
    h.execute_io.side_effect = RuntimeError(SECRET)
    assert h.run(once=True) == 2
    assert egress.alerts == ["BuenaPro PROD6: servicio detenido (attention_required): "
                             "RuntimeError en download_file; requiere revisión."]


def test_cycle_level_failure_and_startup_failure_alert_without_details(offline_runtime, egress):
    h = offline_runtime
    h.poll.side_effect = RuntimeError(SECRET)
    assert h.run(once=True) == 2
    assert egress.alerts == ["BuenaPro PROD6: servicio detenido (attention_required): "
                             "RuntimeError en fase source_discovery; requiere revisión."]
    h.connect.side_effect = psycopg.OperationalError(SECRET)
    assert h.run(once=True) == 2
    assert len(egress.alerts) == 2 and "OperationalError en el arranque (database_connect)" in egress.alerts[1]
    assert SECRET not in json.dumps(egress.alerts)


def test_stops_that_are_not_attention_required_do_not_alert(offline_runtime, egress):
    h = offline_runtime
    runtime.profile_enabled.return_value = False
    assert h.run(once=True) == 2
    assert h.states[-1]["status"] == "profile_paused" and egress.alerts == []


def test_dispatch_lets_a_rejected_reading_retry_even_as_a_replay_only_job(monkeypatch, tmp_path):
    """Real extractor behind the runtime's hash adapter; only Gemini and the database are mocked."""
    from google.genai import errors as genai_errors
    from buenapro_worker.prod6 import reading
    pdf = b"%PDF-1.7 rejected then read"
    sha = reading._sha(pdf)
    client = MagicMock()
    client.models.generate_content.side_effect = [
        genai_errors.ClientError(429, {"error": {"code": 429, "message": SECRET, "status": "RESOURCE_EXHAUSTED"}}),
        SimpleNamespace(model_dump=lambda mode: {"candidates": [{"content": {"parts": [{"text": '{"summary": {"x": "y"}}'}]},
            "finish_reason": "STOP"}], "usage_metadata": {"prompt_token_count": 10, "candidates_token_count": 5}}),
    ]
    monkeypatch.setattr(reading.genai, "Client", MagicMock(return_value=client))
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=True))
    monkeypatch.setattr(runtime, "current_extraction", MagicMock(return_value=None))
    settings = SimpleNamespace(gemini_model="gemini-3.1-flash-lite", gemini_api_key=SECRET)

    def read(settings, repo, **kwargs):
        kwargs["extractor"].extract(pdf, mime="application/pdf", doc_class="tdr")
        return 99

    def dispatch(attempts):
        repo = runtime.ScopedRepository(MagicMock(), profile_id="veyon", batch_id="b")
        repo.conn.execute.return_value.fetchone.side_effect = [None, None, {"sha256_original": sha}]
        monkeypatch.setattr(runtime, "extract_tdr_job", read)
        job = Job(7, "extract_tdr", "llm", {"id_contrato": 1, "id_contrato_archivo": 2}, attempts)
        return runtime.dispatch(settings, repo, job, tmp_path)

    with pytest.raises(ProviderRejectedError) as rejected:
        dispatch(1)
    assert rejected.value.code == 429 and not (tmp_path / "readings" / "guard.json").exists()
    # Even a job whose attempt count says "replay only" may retry after a documented rejection.
    outcome = dispatch(2)
    assert outcome == {"outcome": "read_or_reused", "extraction_id": 99, "provider_answered": True}
    assert client.models.generate_content.call_count == 2


# --- Search cadence: the search runs once per lap interval, IO gets the cycles in between -------

INTERVAL = 7200


def lap(index, searched, *, complete=False, requests=22, count=6):
    """A search that covered ``searched`` streams and left the cursor at ``index``."""
    return dict(SOURCE_STATS, http_requests=requests, streams_searched=searched, cursor={
        "next_stream_index": index, "next_stream": [1, 43], "stream_count": count, "complete": complete})


def searching(h, *results, requests=22):
    """A search stand-in that spends its requests on the pacer like the real client."""
    queue = deque(results)

    def poll(*_args, **_kwargs):
        for _ in range(requests):
            pacing.PACER.acquire()
        return queue.popleft()

    h.poll.side_effect = poll


def io_costing(h, requests):
    def execute(settings, profile_id, job, receipts, **_kwargs):
        for _ in range(requests):
            pacing.PACER.acquire()
        return {"files_changed": True}

    h.execute_io.side_effect = execute


def finished(h):
    """The final state of each cycle, in order."""
    return list({state["batch_id"]: state for state in h.states if "finished_at" in state}.values())


def test_the_first_cycle_searches_and_a_complete_lap_starts_the_wait(offline_runtime, clock):
    h = offline_runtime
    searching(h, lap(0, 6, complete=True))
    assert h.run(once=True, discovery_interval_seconds=INTERVAL) == 0
    h.poll.assert_called_once()
    assert h.poll.call_args.kwargs["start_stream"] == 0 and h.poll.call_args.kwargs["max_streams"] is None
    state = json.loads((h.folder / "state.json").read_text())
    assert state["last_discovery_completed_at"] == clock.now.isoformat()
    discovery = state["discovery"]
    assert discovery["due"] is True and discovery["skipped_reason"] is None
    assert discovery["next_due_at"] == (clock.now + timedelta(seconds=INTERVAL)).isoformat()
    assert discovery["interval_seconds"] == INTERVAL and discovery["complete"] is True
    assert state["source_coverage"] == "configured_scope_complete"


def test_cycles_inside_the_interval_skip_the_search_and_io_takes_the_whole_budget(
        offline_runtime, egress, clock):
    h = offline_runtime
    searching(h, lap(0, 6, complete=True), lap(0, 6, complete=True))
    io_costing(h, 4)
    queued = deque(poll_job(number, "process_contract") for number in range(1, 40))
    h.claim.side_effect = lambda worker_id, job_types=runtime.JOB_TYPES: (
        queued.popleft() if job_types == runtime.IO_TYPES and queued else None)
    assert run_cycles(h, clock, 5, discovery_interval_seconds=INTERVAL) == 0
    cycles = finished(h)
    assert [state["discovery"]["due"] for state in cycles] == [True, False, False, False, True]
    assert h.poll.call_count == 2  # First cycle and, two hours after the lap ended, the fifth.
    assert h.jev.call_count == 5  # Screening and admission run in every cycle.
    first, second = cycles[0], cycles[1]
    # Searching cycle: 22 of 40 requests went to the search, so only four 4-request jobs fit.
    assert first["source_requests"] == {"used": 38, "max": 40, "budget_reached": True}
    assert first["source_pacing"]["max_requests"] == 40
    # Cycle without search: all 40 requests are for IO (ten jobs).
    assert second["source_requests"] == {"used": 40, "max": 40, "budget_reached": True}
    assert second["source"] == {} and "source_seconds" not in second
    assert second["discovery"]["skipped_reason"] == (
        "not_due_until " + (datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
                            + timedelta(seconds=INTERVAL)).isoformat())
    assert second["discovery"]["next_due_at"] == second["discovery"]["skipped_reason"].split(" ")[1]
    assert second["source_coverage"] == "bounded_partial"  # The IO queue is longer than the budget.
    assert h.execute_io.call_count == 4 + 10 + 10 + 10 + 4
    assert cycles[4]["discovery"]["skipped_reason"] is None
    assert egress.alerts == []


def test_cycle_without_search_and_with_nothing_to_do_reports_discovery_not_due(offline_runtime, clock):
    h = offline_runtime
    searching(h, lap(0, 6, complete=True))
    assert run_cycles(h, clock, 2, discovery_interval_seconds=INTERVAL) == 0
    second = finished(h)[1]
    assert second["status"] == "sweep_drained" and second["source_coverage"] == "discovery_not_due"
    assert second["discovery"]["due"] is False
    assert second["source_requests"] == {"used": 0, "max": 40, "budget_reached": False}


def test_an_unfinished_lap_continues_in_the_next_cycle_without_waiting_for_the_interval(offline_runtime, clock):
    h = offline_runtime
    searching(h, lap(2, 2, requests=24), lap(0, 4, complete=True, requests=12), requests=0)
    assert run_cycles(h, clock, 4, discovery_interval_seconds=INTERVAL) == 0
    cycles = finished(h)
    assert [state["discovery"]["due"] for state in cycles] == [True, True, False, False]
    first = cycles[0]
    assert "last_discovery_completed_at" not in first  # The lap is not complete: the wait has not started.
    assert first["discovery"]["complete"] is False and first["discovery"]["next_due_at"] is None
    assert (first["discovery"]["pass_origin_index"], first["discovery"]["pass_streams_searched"]) == (0, 2)
    # Next cycle: resumes at the cursor and only searches what the lap still lacks (4 of 6 streams).
    second_call = h.poll.call_args_list[1].kwargs
    assert second_call["start_stream"] == 2 and second_call["max_streams"] == 4
    second = cycles[1]
    assert second["last_discovery_completed_at"] == (datetime(2026, 10, 24, 12, 30, tzinfo=timezone.utc)).isoformat()
    assert second["discovery"]["pass_streams_searched"] == 6 and second["discovery"]["complete"] is True
    # Third cycle: the lap closed, so the interval counts from the end of the second.
    assert cycles[2]["discovery"]["skipped_reason"].startswith("not_due_until 2026-10-24T14:30:00")
    assert h.poll.call_count == 2


def test_a_cursor_from_the_previous_version_without_lap_counters_still_continues(offline_runtime, clock):
    h = offline_runtime
    # What the version before this one left behind: a cursor, but no lap counters and no stamp.
    (h.folder / "state.json").write_text(json.dumps({"discovery": {
        "next_stream_index": 3, "next_stream": [2, 43], "stream_count": 6, "complete": False}}))
    searching(h, lap(0, 6, complete=True))
    assert h.run(once=True, discovery_interval_seconds=INTERVAL) == 0
    assert h.poll.call_args.kwargs["start_stream"] == 3 and h.poll.call_args.kwargs["max_streams"] is None


def test_after_the_interval_the_search_runs_again_and_resumes_at_the_cursor(offline_runtime, clock):
    h = offline_runtime
    searching(h, lap(0, 6, complete=True), lap(0, 6, complete=True))
    assert run_cycles(h, clock, 5, discovery_interval_seconds=INTERVAL, step=1800) == 0
    cycles = finished(h)
    assert cycles[4]["discovery"]["due"] is True and cycles[4]["discovery"]["skipped_reason"] is None
    assert cycles[4]["last_discovery_completed_at"] == (
        datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc).isoformat())
    assert cycles[3]["last_discovery_completed_at"] == datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc).isoformat()


def test_the_wait_survives_a_restart_and_a_fresh_folder_searches(offline_runtime, clock, tmp_path):
    h = offline_runtime
    searching(h, lap(0, 6, complete=True), lap(0, 6, complete=True), lap(0, 6, complete=True))
    assert h.run(once=True, discovery_interval_seconds=INTERVAL) == 0
    assert h.poll.call_count == 1
    clock.now += timedelta(seconds=INTERVAL - 60)
    assert h.run(once=True, discovery_interval_seconds=INTERVAL) == 0  # A new process reads state.json.
    assert h.poll.call_count == 1
    assert json.loads((h.folder / "state.json").read_text())["discovery"]["skipped_reason"].startswith("not_due_until")
    clock.now += timedelta(seconds=60)
    assert h.run(once=True, discovery_interval_seconds=INTERVAL) == 0
    assert h.poll.call_count == 2
    other = tmp_path / "other"
    assert runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=other, once=True,
                       discovery_interval_seconds=INTERVAL) == 0
    assert h.poll.call_count == 3  # No state: the one-shot run searches.


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"last_discovery_completed_at": "ayer"}),
                                     json.dumps({"last_discovery_completed_at": 5}), None])
def test_an_unreadable_stamp_means_no_complete_lap_on_record(tmp_path, content):
    path = tmp_path / "state.json"
    if content is not None:
        path.write_text(content)
    assert runtime.load_discovery_completed_at(path) is None
    path.write_text(json.dumps({"last_discovery_completed_at": "2026-10-24T12:00:00+00:00"}))
    assert runtime.load_discovery_completed_at(path) == datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def test_schedule_decisions():
    now = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
    done = {"complete": True, "next_stream_index": 0}
    due = runtime.discovery_schedule
    assert due(None, None, now, 7200) == {"due": True, "reason": "never_completed", "next_due_at": None}
    assert due(done, None, now, 7200)["reason"] == "never_completed"
    assert due({"complete": False}, now, now, 7200) == {
        "due": True, "reason": "partial_pass_pending", "next_due_at": None}
    assert due(done, now - timedelta(seconds=7199), now, 7200)["due"] is False
    assert due(done, now - timedelta(seconds=7200), now, 7200)["reason"] == "interval_elapsed"
    assert due(done, now, now, 0)["due"] is True  # 0 searches in every cycle.
    future = due(done, now + timedelta(days=3), now, 7200)  # A stamp from the future never stalls the search.
    assert future["due"] is False and future["next_due_at"] == (now + timedelta(seconds=7200)).isoformat()


def test_search_denied_while_due_reports_the_pause_and_the_search_waits_for_it(offline_runtime, egress, clock):
    h = offline_runtime
    h.poll.side_effect = [http_failure(403), lap(0, 6, complete=True)]
    assert run_cycles(h, clock, 7, discovery_interval_seconds=INTERVAL) == 0
    cycles = finished(h)
    assert h.poll.call_count == 2  # The denied one and one probe once the three hours passed (cycle 7).
    assert cycles[1]["discovery"]["due"] is True and cycles[1]["discovery"]["skipped_reason"] == "source_blocked"
    assert "last_discovery_completed_at" not in cycles[1]
    assert cycles[-1]["discovery"]["skipped_reason"] is None and "last_discovery_completed_at" in cycles[-1]


def test_a_cycle_without_search_probes_the_source_with_io_and_closes_the_pause(offline_runtime, egress, clock):
    h = offline_runtime
    searching(h, lap(0, 6, complete=True), requests=0)
    pending = deque()
    h.claim.side_effect = lambda worker_id, job_types=runtime.JOB_TYPES: (
        pending.popleft() if job_types == runtime.IO_TYPES and pending else None)
    calls = []

    def execute(settings, profile_id, job, receipts, **_kwargs):
        calls.append(job.id)
        pacing.PACER.acquire()
        if job.id == 1:
            denied_download(403)(settings, profile_id, job, receipts)
        return {"files_changed": True}

    h.execute_io.side_effect = execute
    waits = []

    def wait(should_stop, interval_seconds):
        waits.append(interval_seconds)
        clock.now += timedelta(seconds=1800)
        if len(waits) == 1:
            pending.append(poll_job(1))  # Cycle 2: its download is denied.
        if len(waits) == 7:
            pending.append(poll_job(2))  # Cycle 8: the pause (three hours) is over.
        if len(waits) == 8:
            h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False, discovery_interval_seconds=10 * INTERVAL) == 0
    assert h.poll.call_count == 1  # Never due again in these four hours.
    assert calls == [1, 2]
    cycles = finished(h)
    assert cycles[1]["status"] == "source_blocked"
    assert cycles[7]["status"] == "sweep_drained" and "source_block" not in cycles[7]
    assert len(egress.alerts) == 2 and "blocked" in egress.alerts[0] and "recovered" in egress.alerts[1]
    final = json.loads((h.folder / "state.json").read_text())
    assert "source_blocked_until" not in final and "source_block" not in final


def test_discovery_interval_comes_from_the_argument_then_the_environment(offline_runtime, monkeypatch):
    h = offline_runtime
    monkeypatch.delenv("PROD6_DISCOVERY_INTERVAL_SECONDS")
    assert h.run(once=True) == 0
    assert h.states[-1]["discovery"]["interval_seconds"] == 7200  # The default.
    monkeypatch.setenv("PROD6_DISCOVERY_INTERVAL_SECONDS", "5400")
    assert h.run(once=True) == 0
    assert h.states[-1]["discovery"]["interval_seconds"] == 5400
    assert h.run(once=True, discovery_interval_seconds=60) == 0
    assert h.states[-1]["discovery"]["interval_seconds"] == 60


def test_invalid_discovery_interval_is_rejected_before_the_database(monkeypatch, tmp_path):
    connect = MagicMock()
    monkeypatch.setattr(runtime, "connect", connect)
    with pytest.raises(ValueError, match="discovery interval"):
        runtime.run(MagicMock(), profile_ids=["a"], receipt_dir=tmp_path, discovery_interval_seconds=-1)
    for value in ("-5", "soon"):
        monkeypatch.setenv("PROD6_DISCOVERY_INTERVAL_SECONDS", value)
        with pytest.raises(ValueError, match="PROD6_DISCOVERY_INTERVAL_SECONDS"):
            runtime.run(MagicMock(), profile_ids=["a"], receipt_dir=tmp_path)
    connect.assert_not_called()


def test_discovery_interval_option_reaches_the_runtime(monkeypatch, tmp_path):
    run = MagicMock(return_value=0)
    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "Settings", MagicMock())
    for arguments, expected in (([], None), (["--discovery-interval-seconds", "3600"], 3600)):
        monkeypatch.setattr("sys.argv", ["prod6-runtime", "--profile-id", "a",
                                         "--receipt-dir", str(tmp_path), *arguments])
        with pytest.raises(SystemExit):
            runtime.main()
        assert run.call_args.kwargs["discovery_interval_seconds"] == expected
