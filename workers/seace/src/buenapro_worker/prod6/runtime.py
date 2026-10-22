"""PROD6 discovery and document ingestion without legacy schedulers or LLM2.

The existing queues are retained, but claims require this runtime's namespace,
explicit profile and four allowed job types. A session advisory lock prevents
two instances. Provider responses are durably cached before database writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg

from buenapro_worker.db.connection import connect
from buenapro_worker.jobs.derive_summary import derive_summary_job
from buenapro_worker.jobs.download_file import download_file_job
from buenapro_worker.jobs.extract_tdr import enqueue_derive_summary, extract_tdr_job
from buenapro_worker.jobs.poll_prod6_profile import poll_prod6_profile
from buenapro_worker.jobs.process_contract import process_contract
from buenapro_worker.queue.repository import Job, JobRepository
from buenapro_worker.seace.egress import EgressUnavailable, check_egress, is_egress_failure, notify_ops
from buenapro_worker.settings import Settings
from buenapro_worker.extraction.commercial_classification import POLICY_KEY, TAXONOMY_VERSION, get_commercial_policy
from buenapro_worker.prod6.reading import (
    ReadingPolicyError, bind_reading_policy, reading_policy_context,
)


NAMESPACE = "prod6-only-v1"
JOB_TYPES = ("process_contract", "download_file", "extract_tdr", "derive_summary")
IO_TYPES = ("process_contract", "download_file")
LOCK = (2061013, 6)


def free_http_failure(error: Exception) -> dict[str, Any] | None:
    """Only temporary source HTTP failures may wait for another free polling cycle."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {408, 425, 429, 500, 502, 503, 504}:
            return {"error_type": type(error).__name__, "http_status": status}
    elif isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return {"error_type": type(error).__name__}
    return None


def _wait_for_poll(should_stop, interval_seconds: int) -> None:
    deadline = time.monotonic() + interval_seconds
    while time.monotonic() < deadline and not should_stop():
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def failure_diagnostic(error: Exception) -> dict[str, str]:
    """Keep structured failure codes without provider messages or connection secrets."""
    diagnostic = {"error_type": type(error).__name__}
    sqlstate = error.sqlstate if isinstance(error, psycopg.Error) else None
    if (isinstance(sqlstate, str) and len(sqlstate) == 5
            and all(character in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for character in sqlstate)):
        diagnostic["sqlstate"] = sqlstate
    return diagnostic


class ScopedRepository(JobRepository):
    def __init__(self, conn, *, profile_id: str, batch_id: str, reading_policy=None):
        super().__init__(conn)
        self.profile_id, self.batch_id = profile_id, batch_id
        self.reading_policy = reading_policy_context(reading_policy)
        self.suppressed: dict[str, int] = {}

    def enqueue(self, job_type, payload=None, **kwargs):
        if job_type not in JOB_TYPES:
            # Ingestion does not authorize historical LLM2 or notifications.
            self.suppressed[job_type] = self.suppressed.get(job_type, 0) + 1
            return None
        payload = dict(payload or {}) | {
            "source_runtime": NAMESPACE, "scope_profile_id": self.profile_id,
            "batch_id": self.batch_id,
            **self.reading_policy,
        }
        if job_type in {"download_file", "extract_tdr"} and payload.get("id_contrato_archivo") is not None:
            blocked = self.conn.execute("""SELECT id FROM worker_jobs WHERE status='pending'
                AND run_after='infinity'::timestamptz AND payload->>'source_runtime'=%s
                AND payload->>'scope_profile_id'=%s AND payload->>'id_contrato'=%s
                AND payload->>'id_contrato_archivo'=%s
                AND payload->'source_document_block'->>'reason'='manual_review_required' LIMIT 1""",
                (NAMESPACE, self.profile_id, str(payload.get("id_contrato")),
                 str(payload["id_contrato_archivo"]))).fetchone()
            if blocked:
                self.suppressed["source_document_blocked"] = self.suppressed.get("source_document_blocked", 0) + 1
                return None
        kwargs["dedup_key"] = f"{NAMESPACE}:{self.profile_id}:{kwargs.get('dedup_key') or job_type}"
        kwargs["max_attempts"] = 1
        return super().enqueue(job_type, payload, **kwargs)

    def claim_scoped(self, worker_id: str, job_types: tuple[str, ...] = JOB_TYPES) -> Job | None:
        if not job_types or not set(job_types).issubset(JOB_TYPES):
            raise ValueError("Claims must stay inside the ingestion whitelist")
        row = self.conn.execute(
            """UPDATE worker_jobs SET status='claimed', claimed_by=%s,
                 claimed_at=now(), attempts=attempts+1
               WHERE id=(SELECT id FROM worker_jobs WHERE status='pending'
                 AND run_after<=now() AND payload->>'source_runtime'=%s
                 AND payload->>'scope_profile_id'=%s AND job_type=ANY(%s)
                 ORDER BY priority, run_after, id FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING id, job_type, queue_name, payload, attempts""",
            (worker_id, NAMESPACE, self.profile_id, list(job_types)),
        ).fetchone()
        if not row:
            return None
        return Job(int(row["id"]), row["job_type"], row["queue_name"],
                   dict(row["payload"]), int(row["attempts"]))


def profile_enabled(repo: ScopedRepository) -> bool:
    row = repo.conn.execute(
        """SELECT is_active, identity_json->>'profile_ingestion_enabled' AS enabled
           FROM company_profiles WHERE id=%s""", (repo.profile_id,),
    ).fetchone()
    return bool(row and row["is_active"] and row["enabled"] != "false")


def current_extraction(repo: ScopedRepository, contract_id: int, archive_id: int):
    return repo.conn.execute(
        """SELECT e.id FROM tdr_extractions e JOIN contract_documents d
           ON d.id=e.contract_document_id WHERE d.id_contrato=%s
           AND d.id_contrato_archivo=%s AND e.is_current AND e.quality<>'failed'
           LIMIT 1""", (contract_id, archive_id),
    ).fetchone()


def eligible_contract(repo: ScopedRepository, contract_id: int) -> bool:
    return bool(repo.conn.execute(
        """SELECT 1 FROM seace_contracts WHERE id_contrato=%s
           AND estado_codigo=2 AND objeto_codigo IN (1,2)
           AND (fec_fin_cotizacion IS NULL OR fec_fin_cotizacion>now())""",
        (contract_id,),
    ).fetchone())


def enqueue_unread_documents(repo: ScopedRepository, contract_id: int) -> int:
    # process_contract historically skipped these when the file list was unchanged.
    rows = repo.conn.execute(
        """SELECT d.id_contrato_archivo FROM contract_documents d
           WHERE d.id_contrato=%s AND d.categoria=1
           AND NOT EXISTS(SELECT 1 FROM tdr_extractions e
             WHERE e.contract_document_id=d.id AND e.is_current AND e.quality<>'failed')""",
        (contract_id,),
    ).fetchall()
    count = 0
    for row in rows:
        archive_id = int(row["id_contrato_archivo"])
        count += int(repo.enqueue("download_file", {
            "id_contrato": contract_id, "id_contrato_archivo": archive_id,
        }, queue_name="io", priority=2,
            dedup_key=f"download_file:{repo.batch_id}:{archive_id}") is not None)
    return count


class HashBoundExtractor:
    def __init__(self, extractor, expected_sha256: str):
        self.extractor, self.expected = extractor, expected_sha256

    def extract(self, content: bytes, **kwargs):
        if hashlib.sha256(content).hexdigest() != self.expected:
            raise ValueError("Document changed between identification and reading")
        return self.extractor.extract(content, **kwargs)

    def reuse_prompt_versions(self, doc_class: str) -> tuple[str, ...]:
        return self.extractor.reuse_prompt_versions(doc_class)

    def validate_reused_extraction(self, source: dict, doc_class: str) -> None:
        self.extractor.validate_reused_extraction(source, doc_class)

    def recover_before_download(self, document_sha256: str, **kwargs):
        if document_sha256 != self.expected:
            raise ValueError("Recovery identity differs from the original document SHA")
        return self.extractor.recover_before_download(document_sha256, **kwargs)


def dispatch(settings: Settings, repo: ScopedRepository, job: Job, receipt_dir: Path):
    payload = job.payload
    contract_id = int(payload["id_contrato"])
    common = {"batch_id": repo.batch_id, "segment": payload.get("segment")}
    if (job.job_type in IO_TYPES
            and get_commercial_policy(payload) != get_commercial_policy(repo.reading_policy)):
        raise ReadingPolicyError("Queued PROD6 IO differs from the runtime policy")
    if not eligible_contract(repo, contract_id):
        return {"outcome": "closed_preserved"}
    if job.job_type == "process_contract":
        before = repo.conn.execute("SELECT pipeline_state FROM seace_contracts WHERE id_contrato=%s",
                                   (contract_id,)).fetchone()
        result = process_contract(settings, repo, contract_id, force_detail=True, **common)
        if eligible_contract(repo, contract_id):
            result["unread_enqueued"] = enqueue_unread_documents(repo, contract_id)
            # Metadata refresh must not leave an already-read contract looking
            # unfinished merely because upsert_files recorded a fresh inventory.
            previous = before["pipeline_state"] if before else None
            restored = previous if previous in {"validated", "normalized", "matched"} else "validated"
            repo.conn.execute("""UPDATE seace_contracts c SET pipeline_state=%s
               WHERE c.id_contrato=%s AND c.pipeline_state='files_listed'
               AND EXISTS(SELECT 1 FROM contract_documents d JOIN tdr_extractions e
                   ON e.contract_document_id=d.id WHERE d.id_contrato=c.id_contrato
                   AND e.is_current AND e.quality<>'failed')
               AND NOT EXISTS(SELECT 1 FROM contract_documents d
                   WHERE d.id_contrato=c.id_contrato AND d.categoria=1 AND NOT EXISTS(
                     SELECT 1 FROM tdr_extractions e WHERE e.contract_document_id=d.id
                     AND e.is_current AND e.quality<>'failed'))""", (restored, contract_id))
        return result
    if job.job_type == "download_file":
        archive_id = int(payload["id_contrato_archivo"])
        before = repo.conn.execute(
            "SELECT id, sha256_original FROM contract_documents WHERE id_contrato=%s AND id_contrato_archivo=%s",
            (contract_id, archive_id),
        ).fetchone()
        result = download_file_job(settings, repo, id_contrato=contract_id,
                                   id_contrato_archivo=archive_id, **common)
        if (before and not str(before["sha256_original"]).startswith("pending:")
                and before["sha256_original"] != result["sha256_original"]):
            # A changed PDF cannot continue to advertise the old extraction as current.
            repo.conn.execute("UPDATE tdr_extractions SET is_current=false WHERE contract_document_id=%s AND is_current",
                              (before["id"],))
            result["previous_reading_invalidated"] = True
        return result
    if job.job_type == "extract_tdr":
        archive_id = int(payload["id_contrato_archivo"])
        existing = current_extraction(repo, contract_id, archive_id)
        if existing:
            enqueue_derive_summary(repo, id_contrato=contract_id,
                                   extraction_id=int(existing["id"]), **common)
            return {"outcome": "current_reading_preserved", "extraction_id": int(existing["id"])}
        queued_policy = get_commercial_policy(payload)
        expected_policy = get_commercial_policy(repo.reading_policy)
        if queued_policy != expected_policy:
            # A fresh receipt folder/config cannot adopt an older queued paid
            # job whose semantic binding belongs to a different reader policy.
            raise ReadingPolicyError("Queued PROD6 reading differs from the runtime policy")
        historical_error = repo.conn.execute(
            """SELECT id FROM worker_jobs WHERE job_type='extract_tdr' AND attempts>0
               AND status IN ('dead','failed','claimed')
               AND payload->>'id_contrato'=%s AND payload->>'id_contrato_archivo'=%s
               AND payload->>'source_runtime' IS DISTINCT FROM %s LIMIT 1""",
            (str(contract_id), str(archive_id), NAMESPACE),
        ).fetchone()
        if historical_error:
            return {"outcome": "historical_attempt_preserved", "review_required": True,
                    "historical_job_id": int(historical_error["id"])}
        same_runtime_attempt = repo.conn.execute(
            """SELECT id FROM worker_jobs WHERE job_type='extract_tdr' AND attempts>0
               AND id<>%s AND payload->>'id_contrato'=%s AND payload->>'id_contrato_archivo'=%s
               AND payload->>'source_runtime'=%s LIMIT 1""",
            (job.id, str(contract_id), str(archive_id), NAMESPACE),
        ).fetchone()
        from buenapro_worker.prod6.reading import GuardedGeminiExtractor
        document = repo.conn.execute(
            "SELECT sha256_original FROM contract_documents WHERE id_contrato=%s AND id_contrato_archivo=%s",
            (contract_id, archive_id),
        ).fetchone()
        options = {"policy_context": repo.reading_policy} if repo.reading_policy else {}
        if job.attempts > 1 or same_runtime_attempt:
            options["allow_new_requests"] = False
        extractor = HashBoundExtractor(GuardedGeminiExtractor(settings, receipt_dir / "readings", **options),
                                      document["sha256_original"])
        extraction_id = extract_tdr_job(settings, repo, id_contrato=contract_id,
                                       id_contrato_archivo=archive_id, extractor=extractor, **common)
        return {"outcome": "read_or_reused", "extraction_id": extraction_id}
    if job.job_type == "derive_summary":
        return derive_summary_job(repo, id_contrato=contract_id,
                                  extraction_id=int(payload["extraction_id"]), **common)
    raise ValueError("Job outside PROD6 runtime scope")


def execute_io_job(settings: Settings, profile_id: str, job: Job, receipt_dir: Path, *, reading_policy=None):
    """Each free HTTP job owns a DB connection; paid reading stays on the coordinator."""
    if job.job_type not in IO_TYPES:
        raise ValueError("Parallel executor accepts only free HTTP jobs")
    expected_policy = reading_policy_context(reading_policy)
    if get_commercial_policy(job.payload) != get_commercial_policy(expected_policy):
        raise ReadingPolicyError("Queued PROD6 IO differs from the runtime policy")
    with connect(settings) as conn:
        conn.commit()
        conn.autocommit = True
        repo = ScopedRepository(conn, profile_id=profile_id, batch_id=job.payload["batch_id"],
                                reading_policy=expected_policy)
        with conn.transaction():
            if not profile_enabled(repo):
                raise RuntimeError("Profile was paused")
            outcome = dispatch(settings, repo, job, receipt_dir)
            conn.execute("UPDATE worker_jobs SET payload=payload || %s::jsonb WHERE id=%s",
                         (json.dumps({"runtime_result": outcome}), job.id))
            repo.complete(job.id)
        return outcome


def run(settings: Settings, *, profile_id: str, receipt_dir: Path, once: bool = False,
        interval_seconds: int = 1800, max_pages: int = 100, max_candidates: int = 200,
        http_workers: int = 3, io_workers: int = 2,
        llm1_commercial_taxonomy: str | None = None) -> int:
    requested_policy = reading_policy_context(
        {POLICY_KEY: llm1_commercial_taxonomy} if llm1_commercial_taxonomy is not None else None)
    if interval_seconds < 60 or max_pages < 1 or max_candidates < 1:
        raise ValueError("Positive bounded limits required; minimum poll interval is 60 seconds")
    if not 1 <= http_workers <= 4 or not 1 <= io_workers <= 4:
        raise ValueError("HTTP/IO parallelism must stay between one and four")
    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker_id = f"{NAMESPACE}:{profile_id}"
    with ExitStack() as connections:
        startup_phase = "database_connect"
        try:
            conn = connections.enter_context(connect(settings))
            conn.commit()
            conn.autocommit = True
            startup_phase = "coordinator_lock"
            locked = conn.execute("SELECT pg_try_advisory_lock(%s,%s) AS locked", LOCK).fetchone()
            if not locked["locked"]:
                raise RuntimeError("Another PROD6 coordinator holds the lock")
            startup_phase = "reading_policy"
            effective_policy = bind_reading_policy(receipt_dir / "readings", requested_policy)
            startup_phase = "claim_recovery"
            # Recovery owns only this namespace, after exclusive ownership is established.
            with conn.transaction():
                conn.execute("""UPDATE worker_jobs SET status='pending',claimed_by=NULL,claimed_at=NULL
                  WHERE status='claimed' AND claimed_by=%s AND payload->>'source_runtime'=%s
                  AND payload->>'scope_profile_id'=%s""", (worker_id, NAMESPACE, profile_id))
        except (psycopg.Error, ReadingPolicyError) as error:
            failed_at = datetime.now(timezone.utc)
            startup = {"runtime": NAMESPACE, "profile_id": profile_id,
                "status": "attention_required", "phase": startup_phase,
                "failed_at": failed_at.isoformat(), **failure_diagnostic(error)}
            # Without a live exclusive lock, state.json may belong to another coordinator.
            write_state(receipt_dir / f"startup-{failed_at.strftime('%Y%m%dT%H%M%S%f')}-{os.getpid()}.json",
                        startup)
            print(json.dumps(startup), flush=True)
            return 2
        egress_down = False

        def egress_alert(reason: str) -> None:
            nonlocal egress_down
            if not egress_down:
                notify_ops(f"BuenaPro PROD6: SEACE egress unavailable ({reason}); retrying every {interval_seconds}s.")
                egress_down = True

        def egress_backoff(state: dict[str, Any], reason: str) -> None:
            """Egress outages are neither source failures nor manual review; alert once."""
            egress_alert(reason)
            state.update(status="egress_backoff", egress_error=reason, retry_after_seconds=interval_seconds)
            write_state(receipt_dir / "state.json", state)
            write_state(receipt_dir / f"{state['batch_id']}.json", state)
            print(json.dumps(state), flush=True)

        while not stopping:
            batch_id = f"prod6-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
            repo = ScopedRepository(conn, profile_id=profile_id, batch_id=batch_id,
                                    reading_policy=effective_policy)
            state: dict[str, Any] = {"runtime": NAMESPACE, "profile_id": profile_id,
                "batch_id": batch_id, "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "sweeping", "phase": "profile_check", "done": {}, "suppressed": {},
                **effective_policy,
                "parallelism": {"http": http_workers, "io": io_workers, "paid_reading": 1}}
            write_state(receipt_dir / "state.json", state)
            try:
                if not profile_enabled(repo):
                    state["status"] = "profile_paused"
                    write_state(receipt_dir / "state.json", state)
                    return 2
                state["phase"] = "egress_check"
                try:
                    check_egress()
                except EgressUnavailable as error:
                    egress_backoff(state, type(error).__name__)
                    if once:
                        return 2
                    _wait_for_poll(lambda: stopping, interval_seconds)
                    continue
                state["phase"] = "source_discovery"
                source_started = time.monotonic()
                try:
                    with conn.transaction():
                        state["source"] = poll_prod6_profile(settings, repo, profile_id=profile_id,
                            max_pages_per_segment=max_pages, max_candidates=max_candidates,
                            batch_id=batch_id, search_parallelism=http_workers)
                except Exception as error:
                    if is_egress_failure(error):
                        egress_backoff(state, type(error).__name__)
                        if once:
                            return 2
                        _wait_for_poll(lambda: stopping, interval_seconds)
                        continue
                    source_error = free_http_failure(error)
                    if source_error is None:
                        if isinstance(error, httpx.HTTPStatusError):
                            state["source_error"] = {"error_type": type(error).__name__,
                                                     "http_status": error.response.status_code}
                        raise
                    state.update(status="source_backoff", source_error=source_error,
                                 retry_after_seconds=interval_seconds)
                    write_state(receipt_dir / "state.json", state)
                    write_state(receipt_dir / f"{batch_id}.json", state)
                    print(json.dumps(state), flush=True)
                    if once:
                        return 2
                    _wait_for_poll(lambda: stopping, interval_seconds)
                    continue
                if egress_down:
                    notify_ops("BuenaPro PROD6: SEACE egress recovered.")
                    egress_down = False
                state["source_seconds"] = round(time.monotonic() - source_started, 3)
                state["status"] = "processing"
                state["phase"] = "processing"
                write_state(receipt_dir / "state.json", state)
                while not stopping:
                    if not profile_enabled(repo):
                        state["status"] = "profile_paused"
                        write_state(receipt_dir / "state.json", state)
                        return 2
                    io_batch = []
                    with conn.transaction():
                        for _ in range(io_workers):
                            io_job = repo.claim_scoped(worker_id, IO_TYPES)
                            if not io_job:
                                break
                            io_batch.append(io_job)
                    if io_batch:
                        errors = []
                        # Wait for every reserved job to settle before stopping on an error.
                        with ThreadPoolExecutor(max_workers=io_workers) as pool:
                            futures = {pool.submit(execute_io_job, settings, profile_id, io_job,
                                                   receipt_dir, **({"reading_policy": effective_policy}
                                                   if effective_policy else {})): io_job for io_job in io_batch}
                            for future in as_completed(futures):
                                io_job = futures[future]
                                try:
                                    outcome = future.result()
                                except Exception as error:
                                    if (io_job.job_type == "download_file" and isinstance(error, httpx.HTTPStatusError)
                                            and error.response.status_code in {401, 403, 404, 410}):
                                        block = {"http_status": error.response.status_code,
                                                 "reason": "manual_review_required"}
                                        with conn.transaction():
                                            changed = conn.execute("""UPDATE worker_jobs SET status='pending',
                                               claimed_by=NULL,claimed_at=NULL,run_after='infinity'::timestamptz,
                                               payload=payload || %s::jsonb,last_error=%s
                                               WHERE id=%s AND status='claimed' AND claimed_by=%s
                                               AND payload->>'source_runtime'=%s
                                               AND payload->>'scope_profile_id'=%s""",
                                               (json.dumps({"source_document_block": block}),
                                                f"PROD6 source document HTTP {block['http_status']}; manual review; no automatic retry",
                                                io_job.id, worker_id, NAMESPACE, profile_id))
                                            if changed.rowcount != 1:
                                                raise RuntimeError("Blocked document ownership changed")
                                        state.setdefault("blocked_documents", []).append({"job_id": io_job.id,
                                            "contract_id": io_job.payload["id_contrato"],
                                            "file_id": io_job.payload["id_contrato_archivo"], **block})
                                        write_state(receipt_dir / "state.json", state)
                                        continue
                                    if is_egress_failure(error):
                                        # Not a source failure or manual review: defer for the next
                                        # cycle and let that cycle's egress check gate the retry.
                                        with conn.transaction():
                                            changed = conn.execute("""UPDATE worker_jobs SET status='pending',
                                               claimed_by=NULL,claimed_at=NULL,
                                               run_after=now()+%s * interval '1 second',last_error=%s
                                               WHERE id=%s AND status='claimed' AND claimed_by=%s
                                               AND payload->>'source_runtime'=%s
                                               AND payload->>'scope_profile_id'=%s""",
                                               (interval_seconds, "PROD6 free IO deferred: egress unavailable; next polling cycle",
                                                io_job.id, worker_id, NAMESPACE, profile_id))
                                            if changed.rowcount != 1:
                                                raise RuntimeError("Deferred IO job ownership changed")
                                        state.setdefault("egress_deferred", []).append({"job_id": io_job.id,
                                            "job_type": io_job.job_type, "error_type": type(error).__name__,
                                            "retry_after_seconds": interval_seconds})
                                        write_state(receipt_dir / "state.json", state)
                                        continue
                                    source_error = free_http_failure(error)
                                    if source_error is not None:
                                        with conn.transaction():
                                            changed = conn.execute("""UPDATE worker_jobs SET status='pending',
                                               claimed_by=NULL,claimed_at=NULL,
                                               run_after=now()+%s * interval '1 second',last_error=%s
                                               WHERE id=%s AND status='claimed' AND claimed_by=%s
                                               AND payload->>'source_runtime'=%s
                                               AND payload->>'scope_profile_id'=%s""",
                                               (interval_seconds,
                                                f"PROD6 free IO deferred: {source_error['error_type']}"
                                                f" status={source_error.get('http_status', 'transport')}; next polling cycle",
                                                io_job.id, worker_id, NAMESPACE, profile_id))
                                            if changed.rowcount != 1:
                                                raise RuntimeError("Deferred IO job ownership changed")
                                        state.setdefault("deferred_io", []).append({"job_id": io_job.id,
                                            "job_type": io_job.job_type, **source_error,
                                            "retry_after_seconds": interval_seconds})
                                        write_state(receipt_dir / "state.json", state)
                                        continue
                                    with conn.transaction():
                                        conn.execute("""UPDATE worker_jobs SET status='pending',claimed_by=NULL,
                                           claimed_at=NULL,last_error=%s WHERE id=%s AND status='claimed'""",
                                           (f"PROD6 attention required: {type(error).__name__}; no automatic retry",
                                            io_job.id))
                                    errors.append({"job_id": io_job.id, "error_type": type(error).__name__,
                                        **({"http_status": error.response.status_code}
                                           if isinstance(error, httpx.HTTPStatusError) else {})})
                                    continue
                                state["done"][io_job.job_type] = state["done"].get(io_job.job_type, 0) + 1
                                state["last_outcome"] = {"job_id": io_job.id,
                                    "contract_id": io_job.payload["id_contrato"],
                                    "type": io_job.job_type, "result": outcome}
                                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                                write_state(receipt_dir / "state.json", state)
                        if errors:
                            state.update(status="attention_required", io_errors=errors)
                            write_state(receipt_dir / "state.json", state)
                            print(json.dumps(state), flush=True)
                            return 2
                        if state.get("egress_deferred"):
                            break  # Remaining IO waits for the next cycle's egress check.
                        continue
                    with conn.transaction():
                        job = repo.claim_scoped(worker_id)
                    if not job:
                        break
                    # Preserve the actual batch provenance of any resumed pending job.
                    repo.batch_id = job.payload["batch_id"]
                    try:
                        with conn.transaction():
                            outcome = dispatch(settings, repo, job, receipt_dir)
                            conn.execute("UPDATE worker_jobs SET payload=payload || %s::jsonb WHERE id=%s",
                                (json.dumps({"runtime_result": outcome}), job.id))
                            repo.complete(job.id)
                    except Exception as error:
                        # No automatic retry, and no raw provider/PII exception in logs.
                        with conn.transaction():
                            conn.execute("""UPDATE worker_jobs SET status='pending',claimed_by=NULL,
                               claimed_at=NULL,last_error=%s WHERE id=%s""",
                               (f"PROD6 attention required: {type(error).__name__}; see private receipt", job.id))
                        state.update(status="attention_required", job_id=job.id,
                                     job_type=job.job_type, error_type=type(error).__name__)
                        write_state(receipt_dir / "state.json", state)
                        print(json.dumps(state), flush=True)
                        return 2
                    state["done"][job.job_type] = state["done"].get(job.job_type, 0) + 1
                    state["last_outcome"] = {"job_id": job.id, "contract_id": job.payload["id_contrato"],
                                              "type": job.job_type, "result": outcome}
                    state["suppressed"] = repo.suppressed
                    state["updated_at"] = datetime.now(timezone.utc).isoformat()
                    write_state(receipt_dir / "state.json", state)
                state["suppressed"] = repo.suppressed
                incomplete = bool(state.get("blocked_documents") or repo.suppressed.get("source_document_blocked"))
                if state.get("egress_deferred"):
                    egress_alert("io_jobs")
                state["status"] = ("stopped" if stopping else "sweep_blocked" if incomplete
                                   else "egress_backoff" if state.get("egress_deferred")
                                   else "sweep_deferred" if state.get("deferred_io") else "sweep_drained")
                if state["source"]["capped_segments"] or state["source"]["limit_reached"]:
                    state["source_coverage"] = "bounded_partial"
                else:
                    state["source_coverage"] = "configured_scope_complete"
                state["finished_at"] = datetime.now(timezone.utc).isoformat()
                write_state(receipt_dir / f"{batch_id}.json", state)
                write_state(receipt_dir / "state.json", state)
                print(json.dumps(state), flush=True)
            except Exception as error:
                state.update(status="attention_required", failed_at=datetime.now(timezone.utc).isoformat(),
                             **failure_diagnostic(error))
                write_state(receipt_dir / f"{batch_id}.json", state)
                write_state(receipt_dir / "state.json", state)
                print(json.dumps(state), flush=True)
                return 2
            if once or stopping:
                return 2 if state.get("deferred_io") or state.get("egress_deferred") or incomplete else 0
            _wait_for_poll(lambda: stopping, interval_seconds)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=1800)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-candidates", type=int, default=200)
    parser.add_argument("--http-workers", type=int, default=3)
    parser.add_argument("--io-workers", type=int, default=2)
    parser.add_argument("--llm1-commercial-taxonomy", choices=[TAXONOMY_VERSION],
                        help="Explicit fresh receipt namespace only: commercial classification in new readings")
    args = parser.parse_args()
    raise SystemExit(run(Settings(), **vars(args)))


if __name__ == "__main__":
    main()
