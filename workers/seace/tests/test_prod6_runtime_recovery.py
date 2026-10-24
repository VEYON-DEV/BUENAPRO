"""Offline regression guards for free source recovery and paid fail-closed paths."""
from collections import deque
import copy
import json
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest

from buenapro_worker.prod6 import runtime
from buenapro_worker.prod6.reading import ReadingBlockedError
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
    jev = MagicMock(return_value={"status": "ok"})
    monkeypatch.setattr(runtime, "poll_prod6_profile", poll)
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

    def run(*, once=True):
        return runtime.run(MagicMock(), profile_ids=["veyon"], receipt_dir=tmp_path,
                           once=once, interval_seconds=1800, io_workers=2)

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


def test_download_403_is_still_held_for_manual_review_with_the_proxy_configured(offline_runtime, egress, monkeypatch):
    monkeypatch.setenv("SEACE_EGRESS_PROXY", "http://10.161.181.75:8888")
    h = offline_runtime
    h.queues(io=[Job(1, "download_file", "io", {"id_contrato": 42, "id_contrato_archivo": 31, "batch_id": "b"}, 1)])
    h.execute_io.side_effect = http_failure(403)
    assert h.run(once=True) == 2
    state = h.states[-1]
    assert state["status"] == "sweep_blocked"
    assert state["blocked_documents"][0]["http_status"] == 403 and "egress_deferred" not in state
    assert egress.alerts == []


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


def test_several_profiles_are_polled_each_cycle_and_scored_together(offline_runtime):
    h = offline_runtime
    h.poll.side_effect = [{"pages": 2, "capped_segments": 0, "limit_reached": 0},
                          {"pages": 3, "capped_segments": 1, "limit_reached": 0}]
    assert runtime.run(MagicMock(), profile_ids=["veyon", "din"], receipt_dir=h.folder, once=True,
                       max_jev_requests=9, jev_threshold=70) == 0
    assert [call.kwargs["profile_id"] for call in h.poll.call_args_list] == ["veyon", "din"]
    repo, active = h.jev.call_args.args[:2]
    assert repo.profile_id == "veyon" and active == ["veyon", "din"]  # The first profile scopes the queue.
    assert h.jev.call_args.kwargs["max_requests"] == 9 and h.jev.call_args.kwargs["threshold"] == 70
    state = json.loads((h.folder / "state.json").read_text())
    assert state["profile_ids"] == ["veyon", "din"]
    assert state["source"]["pages"] == 5 and state["source"]["capped_segments"] == 1
    assert state["source_by_profile"]["din"]["pages"] == 3


def test_a_paused_extra_profile_is_neither_polled_nor_scored(offline_runtime, monkeypatch):
    h = offline_runtime
    monkeypatch.setattr(runtime, "profile_enabled", lambda repo, profile_id=None: profile_id != "din")
    assert runtime.run(MagicMock(), profile_ids=["veyon", "din", "veyon"], receipt_dir=h.folder, once=True) == 0
    assert [call.kwargs["profile_id"] for call in h.poll.call_args_list] == ["veyon"]
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
