import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from buenapro_worker.prod6.runtime import (
    HashBoundExtractor, IO_TYPES, JOB_TYPES, NAMESPACE, ScopedRepository, dispatch,
    enqueue_unread_documents, execute_io_job, profile_enabled, run, write_state,
)
from buenapro_worker.queue.repository import Job


def repository():
    return ScopedRepository(MagicMock(), profile_id="veyon", batch_id="batch-one")


def test_ingestion_does_not_enqueue_cross_profile_llm2_or_notifications():
    repo = repository()
    for kind in ("analyze_match", "prod4_extract", "route_contract_profiles", "send_alert"):
        assert repo.enqueue(kind, {"profile_id": "dinaut"}) is None
    repo.conn.execute.assert_not_called()
    assert sum(repo.suppressed.values()) == 4


@patch("buenapro_worker.queue.repository.JobRepository.enqueue", return_value=10)
def test_all_ingestion_jobs_carry_explicit_ownership_and_single_attempt(enqueue):
    repo = repository()
    repo.enqueue("download_file", {"id_contrato": 42}, queue_name="io", dedup_key="file:3")
    payload = enqueue.call_args.args[1]
    assert payload["source_runtime"] == NAMESPACE
    assert payload["scope_profile_id"] == "veyon"
    assert payload["batch_id"] == "batch-one"
    assert enqueue.call_args.kwargs["max_attempts"] == 1
    assert enqueue.call_args.kwargs["dedup_key"].startswith(f"{NAMESPACE}:veyon:")


def test_claim_requires_namespace_profile_and_the_allowed_job_types():
    repo = repository()
    repo.conn.execute.return_value.fetchone.return_value = None
    assert repo.claim_scoped("worker") is None
    sql, params = repo.conn.execute.call_args.args
    assert "payload->>'source_runtime'=%s" in sql
    assert "payload->>'scope_profile_id'=%s" in sql
    assert params[1:] == (NAMESPACE, "veyon", list(JOB_TYPES))
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_parallel_claim_is_restricted_to_free_io_types():
    repo = repository()
    repo.conn.execute.return_value.fetchone.return_value = None
    assert repo.claim_scoped("worker", ("process_contract", "download_file")) is None
    assert repo.conn.execute.call_args.args[1][-1] == ["process_contract", "download_file"]
    with pytest.raises(ValueError):
        repo.claim_scoped("worker", ("route_contract_profiles",))
    # LLM2 is claimable (serially, by the coordinator) but never by the free IO executor.
    assert "analyze_match" in JOB_TYPES and "analyze_match" not in IO_TYPES


@patch("buenapro_worker.prod6.runtime.connect")
def test_parallel_executor_cannot_send_paid_requests(connect):
    job = Job(1, "extract_tdr", "llm", {"id_contrato": 42}, 1)
    with pytest.raises(ValueError, match="free HTTP"):
        execute_io_job(MagicMock(), "veyon", job, Path("unused"))
    connect.assert_not_called()


@patch("buenapro_worker.prod6.runtime.dispatch", return_value={"files_changed": True})
@patch("buenapro_worker.prod6.runtime.profile_enabled", return_value=True)
@patch("buenapro_worker.prod6.runtime.connect")
def test_each_parallel_io_job_owns_a_connection_and_persists_before_reporting(connect, enabled, dispatch):
    conn = connect.return_value.__enter__.return_value
    job = Job(1, "process_contract", "io", {"id_contrato": 42, "batch_id": "original"}, 1)
    assert execute_io_job(MagicMock(), "veyon", job, Path("unused")) == {"files_changed": True}
    conn.commit.assert_called_once()
    assert conn.autocommit is True
    repo = dispatch.call_args.args[1]
    assert repo.profile_id == "veyon" and repo.batch_id == "original"
    sql_calls = [call.args[0] for call in conn.execute.call_args_list]
    assert any("runtime_result" in str(call.args[1]) for call in conn.execute.call_args_list)
    assert any("SET status = 'done'" in sql for sql in sql_calls)


@patch("buenapro_worker.prod6.runtime.signal.signal")
@patch("buenapro_worker.prod6.runtime.connect")
@patch("buenapro_worker.prod6.runtime.jev_cycle", return_value={"status": "ok"})
@patch("buenapro_worker.prod6.runtime.poll_prod6_profiles", return_value={"pages": 1})
@patch("buenapro_worker.prod6.runtime.profile_enabled", return_value=True)
@patch("buenapro_worker.prod6.runtime.ScopedRepository.claim_scoped")
@patch("buenapro_worker.prod6.runtime.execute_io_job")
@patch("buenapro_worker.prod6.runtime.dispatch")
def test_parallel_io_error_settles_reserved_jobs_and_stops_before_paid_reading(
        dispatch, execute_io, claim, enabled, poll, jev, connect, signal, tmp_path):
    jobs = [Job(i, "process_contract", "io", {"id_contrato": i, "batch_id": "batch"}, 1)
            for i in (1, 2)]
    claim.side_effect = jobs

    def outcome(settings, profile_id, job, receipts):
        if job.id == 1:
            raise psycopg.OperationalError("database gone")  # Only the database or a payment doubt stops.
        return {"files_changed": True}

    execute_io.side_effect = outcome
    assert run(MagicMock(), profile_ids=["veyon"], receipt_dir=tmp_path, once=True) == 2
    assert execute_io.call_count == 2
    dispatch.assert_not_called()
    state = __import__("json").loads((tmp_path / "state.json").read_text())
    assert state["status"] == "attention_required"
    assert state["done"] == {"process_contract": 1}
    assert state["io_errors"] == [{"job_id": 1, "error_type": "OperationalError"}]


def test_profile_pause_is_observed_before_work():
    repo = repository()
    for row in (None, {"is_active": False, "enabled": None}, {"is_active": True, "enabled": "false"}):
        repo.conn.execute.return_value.fetchone.return_value = row
        assert not profile_enabled(repo)
    assert "identity_json->>'profile_ingestion_enabled'" in repo.conn.execute.call_args.args[0]


def test_unread_files_recover_even_when_process_file_hash_did_not_change():
    repo = repository()
    repo.conn.execute.return_value.fetchall.return_value = [{"id_contrato_archivo": 31}]
    with patch.object(repo, "enqueue", return_value=7) as enqueue:
        assert enqueue_unread_documents(repo, 42) == 1
    assert "e.is_current AND e.quality<>'failed'" in repo.conn.execute.call_args.args[0]
    assert enqueue.call_args.args[1] == {"id_contrato": 42, "id_contrato_archivo": 31}


@patch("buenapro_worker.prod6.runtime.eligible_contract", return_value=False)
@patch("buenapro_worker.prod6.runtime.process_contract")
def test_closed_contracts_preserve_data_without_network_or_reading(process, eligible):
    job = Job(1, "process_contract", "io", {"id_contrato": 42}, 1)
    assert dispatch(MagicMock(), repository(), job, Path("unused")) == {"outcome": "closed_preserved"}
    process.assert_not_called()


@patch("buenapro_worker.prod6.runtime.eligible_contract", return_value=True)
@patch("buenapro_worker.prod6.runtime.enqueue_unread_documents", return_value=0)
@patch("buenapro_worker.prod6.runtime.process_contract", return_value={"files_changed": False})
def test_metadata_refresh_restores_completed_pipeline_only_if_all_primary_docs_read(process, unread, eligible):
    repo = repository()
    repo.conn.execute.return_value.fetchone.return_value = {"pipeline_state": "matched"}
    job = Job(1, "process_contract", "io", {"id_contrato": 42}, 1)
    dispatch(MagicMock(), repo, job, Path("unused"))
    sql, params = repo.conn.execute.call_args.args
    assert params == ("matched", 42)
    assert "d.categoria=1 AND NOT EXISTS" in sql
    assert "e.is_current AND e.quality<>'failed'" in sql


@patch("buenapro_worker.prod6.runtime.eligible_contract", return_value=True)
@patch("buenapro_worker.prod6.runtime.current_extraction", return_value={"id": 12})
@patch("buenapro_worker.prod6.runtime.extract_tdr_job")
def test_same_current_extraction_is_reused_without_constructing_provider(extract, current, eligible):
    repo = repository()
    job = Job(1, "extract_tdr", "llm", {"id_contrato": 42, "id_contrato_archivo": 31}, 1)
    result = dispatch(MagicMock(), repo, job, Path("unused"))
    assert result["outcome"] == "current_reading_preserved"
    extract.assert_not_called()


@patch("buenapro_worker.prod6.runtime.eligible_contract", return_value=True)
@patch("buenapro_worker.prod6.runtime.download_file_job", return_value={"sha256_original": "new"})
def test_changed_pdf_invalidates_old_current_binding(download, eligible):
    repo = repository()
    repo.conn.execute.return_value.fetchone.return_value = {"id": 8, "sha256_original": "old"}
    job = Job(1, "download_file", "io", {"id_contrato": 42, "id_contrato_archivo": 31}, 1)
    assert dispatch(MagicMock(), repo, job, Path("unused"))["previous_reading_invalidated"]
    assert repo.conn.execute.call_args.args[1] == (8,)


def test_hash_change_during_download_blocks_provider():
    provider = MagicMock()
    bound = HashBoundExtractor(provider, hashlib.sha256(b"original").hexdigest())
    with pytest.raises(ValueError, match="changed"):
        bound.extract(b"replacement", mime="application/pdf")
    provider.extract.assert_not_called()


@patch("buenapro_worker.prod6.runtime.eligible_contract", return_value=True)
@patch("buenapro_worker.prod6.runtime.current_extraction", return_value=None)
@patch("buenapro_worker.prod6.runtime.extract_tdr_job")
def test_legacy_attempted_failures_do_not_authorize_new_paid_read(extract, current, eligible):
    repo = repository()
    repo.conn.execute.return_value.fetchone.return_value = {"id": 77}
    job = Job(1, "extract_tdr", "llm", {"id_contrato": 42, "id_contrato_archivo": 31}, 1)
    result = dispatch(MagicMock(), repo, job, Path("unused"))
    assert result == {"outcome": "historical_attempt_preserved", "review_required": True,
                      "historical_job_id": 77}
    extract.assert_not_called()


def test_state_has_private_permissions_and_no_original(tmp_path):
    path = tmp_path / "receipts" / "state.json"
    write_state(path, {"status": "processing"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_suffix(".tmp").exists()
