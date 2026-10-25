"""The queue statements of the PROD6 source pause, against PostgreSQL (always rolled back)."""
import json
import os
import random
from datetime import datetime, timedelta, timezone

import pytest

from buenapro_worker.prod6 import runtime

DATABASE_URL = os.environ.get("BUENAPRO_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="BUENAPRO_TEST_DATABASE_URL not set")
PROFILE = "source-block-test-profile"
WORKER = f"{runtime.NAMESPACE}:{PROFILE}"


@pytest.fixture
def db():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def claimed_download(db):
    """One download job enqueued by the runtime's repository and claimed by it."""
    repo = runtime.ScopedRepository(db, profile_id=PROFILE, batch_id="batch-pause")
    contract = random.randint(9_000_000_000, 9_900_000_000)
    job_id = repo.enqueue("download_file", {"id_contrato": contract, "id_contrato_archivo": 31},
                          queue_name="io", priority=2, dedup_key=f"download_file:{contract}")
    assert job_id is not None
    # now() is the start of this rolled-back transaction, earlier than the enqueue instant.
    db.execute("UPDATE worker_jobs SET run_after=now() - interval '1 second' WHERE id=%s", (job_id,))
    job = repo.claim_scoped(WORKER, runtime.IO_TYPES)
    assert job.id == job_id and job.attempts == 1
    return repo, job


def row(db, job_id):
    return db.execute("SELECT status, attempts, claimed_by, run_after, payload, last_error "
                      "FROM worker_jobs WHERE id=%s", (job_id,)).fetchone()


@needs_db
def test_deferred_job_waits_for_the_pause_gives_back_its_attempt_and_keeps_its_count(db):
    repo, job = claimed_download(db)
    until = datetime.now(timezone.utc) + timedelta(hours=3)
    changed = db.execute(runtime.DEFER_FOR_SOURCE_SQL, (
        until, json.dumps({"source_block_count": 1}), "paused", job.id, WORKER,
        runtime.NAMESPACE, PROFILE))
    assert changed.rowcount == 1
    saved = row(db, job.id)
    assert saved["status"] == "pending" and saved["claimed_by"] is None
    assert saved["attempts"] == 0 and abs(saved["run_after"] - until) < timedelta(seconds=1)
    assert saved["payload"]["source_block_count"] == 1 and saved["payload"]["id_contrato_archivo"] == 31
    assert repo.claim_scoped(WORKER, runtime.IO_TYPES) is None  # Not before the pause ends.
    db.execute("UPDATE worker_jobs SET run_after=now() - interval '1 second' WHERE id=%s", (job.id,))
    again = repo.claim_scoped(WORKER, runtime.IO_TYPES)
    assert again.id == job.id and again.attempts == 1  # A fresh first attempt, with its count.
    assert again.payload["source_block_count"] == 1


@needs_db
def test_attempts_never_go_below_zero_and_other_owners_are_untouched(db):
    repo, job = claimed_download(db)
    db.execute("UPDATE worker_jobs SET attempts=0 WHERE id=%s", (job.id,))
    until = datetime.now(timezone.utc)
    assert db.execute(runtime.DEFER_FOR_SOURCE_SQL, (until, "{}", "paused", job.id, "someone-else",
                                                     runtime.NAMESPACE, PROFILE)).rowcount == 0
    assert db.execute(runtime.DEFER_FOR_SOURCE_SQL, (until, "{}", "paused", job.id, WORKER,
                                                     runtime.NAMESPACE, PROFILE)).rowcount == 1
    assert row(db, job.id)["attempts"] == 0


@needs_db
def test_third_denial_hold_keeps_the_job_out_of_the_queue_and_suppresses_new_ones(db):
    repo, job = claimed_download(db)
    block = {"http_status": 403, "reason": "manual_review_required"}
    changed = db.execute(runtime.HOLD_DOCUMENT_SQL, (
        json.dumps({"source_document_block": block, "source_block_count": 3}), "held",
        job.id, WORKER, runtime.NAMESPACE, PROFILE))
    assert changed.rowcount == 1
    saved = db.execute("SELECT status, payload, run_after='infinity'::timestamptz AS held "
                       "FROM worker_jobs WHERE id=%s", (job.id,)).fetchone()
    assert saved["status"] == "pending" and saved["held"]
    assert saved["payload"]["source_block_count"] == 3
    assert repo.claim_scoped(WORKER, runtime.IO_TYPES) is None
    again = runtime.ScopedRepository(db, profile_id=PROFILE, batch_id="a-later-batch")
    assert again.enqueue("download_file", {"id_contrato": job.payload["id_contrato"],
                                          "id_contrato_archivo": 31}) is None
    assert again.suppressed == {"source_document_blocked": 1}


def claimed_reading(db):
    repo = runtime.ScopedRepository(db, profile_id=PROFILE, batch_id="batch-reading")
    contract = random.randint(9_000_000_000, 9_900_000_000)
    job_id = repo.enqueue("extract_tdr", {"id_contrato": contract, "id_contrato_archivo": 31},
                          queue_name="llm", priority=2, dedup_key=f"extract_tdr:{contract}")
    db.execute("UPDATE worker_jobs SET run_after=now() - interval '1 second' WHERE id=%s", (job_id,))
    job = repo.claim_scoped(WORKER, ("extract_tdr",))
    assert job.id == job_id and job.attempts == 1
    return repo, job


@needs_db
def test_rejected_reading_waits_gives_back_its_attempt_and_counts_the_rejection(db):
    repo, job = claimed_reading(db)
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    assert db.execute(runtime.DEFER_FOR_SOURCE_SQL, (
        until, json.dumps({"provider_error_count": 1}), "Gemini HTTP 402", job.id, WORKER,
        runtime.NAMESPACE, PROFILE)).rowcount == 1
    saved = row(db, job.id)
    assert saved["status"] == "pending" and saved["attempts"] == 0
    assert saved["payload"]["provider_error_count"] == 1 and saved["payload"]["id_contrato_archivo"] == 31
    # While Gemini is paused the runtime asks for every type but the reading.
    reading_paused = tuple(kind for kind in runtime.JOB_TYPES if kind != "extract_tdr")
    assert repo.claim_scoped(WORKER, reading_paused) is None
    db.execute("UPDATE worker_jobs SET run_after=now() - interval '1 second' WHERE id=%s", (job.id,))
    assert repo.claim_scoped(WORKER, reading_paused) is None
    again = repo.claim_scoped(WORKER)
    assert again.id == job.id and again.attempts == 1 and again.payload["provider_error_count"] == 1


@needs_db
def test_third_gemini_rejection_hold_suppresses_new_readings_of_that_document(db):
    repo, job = claimed_reading(db)
    block = {"provider_http_status": 503, "provider_status": "UNAVAILABLE",
             "reason": "manual_review_required"}
    assert db.execute(runtime.HOLD_DOCUMENT_SQL, (
        json.dumps({"source_document_block": block, "provider_error_count": 3}), "held",
        job.id, WORKER, runtime.NAMESPACE, PROFILE)).rowcount == 1
    assert repo.claim_scoped(WORKER) is None
    again = runtime.ScopedRepository(db, profile_id=PROFILE, batch_id="a-later-batch")
    assert again.enqueue("extract_tdr", {"id_contrato": job.payload["id_contrato"],
                                         "id_contrato_archivo": 31}) is None
    assert again.suppressed == {"source_document_blocked": 1}
