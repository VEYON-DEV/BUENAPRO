"""PROD6 discovery, document ingestion and automatic evaluation without legacy schedulers.

The existing queues are retained, but claims require this runtime's namespace,
explicit profile and five allowed job types. A session advisory lock prevents
two instances. Provider responses are durably cached before database writes.

LLM2 (``analyze_match``) runs only for a contract whose current JEV decision for the company
is ``accepted`` (``prod6/evaluation.py``); notifications and the legacy gate stay off.
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
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import psycopg
from dotenv import dotenv_values

from buenapro_worker.db.connection import connect
from buenapro_worker.jobs.analyze_match import analyze_match_job
from buenapro_worker.matching.substantive_policy import ModelOutputMismatch
from buenapro_worker.jobs.derive_summary import derive_summary_job
from buenapro_worker.jobs.download_file import download_file_job
from buenapro_worker.jobs.extract_tdr import enqueue_derive_summary, extract_tdr_job
from buenapro_worker.jobs.poll_prod6_profile import poll_prod6_profiles
from buenapro_worker.jobs.process_contract import process_contract
from buenapro_worker.queue.repository import Job, JobRepository
from buenapro_worker.seace import pacing
from buenapro_worker.seace.egress import EgressUnavailable, check_egress, is_egress_failure, notify_ops
from buenapro_worker.seace.pacing import (
    block_backoff_seconds, blocking_status, default_discovery_interval_seconds,
    default_max_source_requests, min_request_interval, reading_backoff_seconds,
    reading_transient_backoff_seconds, source_stop,
)
from buenapro_worker.settings import Settings
from buenapro_worker.extraction.commercial_classification import POLICY_KEY, TAXONOMY_VERSION, get_commercial_policy
from buenapro_worker.prod6.jev_screening import (
    JevScreeningClient, admit_accepted, score_pending, unadmitted_accepted,
)
from buenapro_worker.prod6.evaluation import (
    AUTO_ORIGIN, INVALID_ANSWER_RESENDS, INVALID_OUTCOME, enqueue_evaluations, evaluation_rows, skip_reason,
)
from buenapro_worker.prod6.evaluation_guard import GuardedMatchAnalyzer
from buenapro_worker.prod6.reading import (
    ProviderRejectedError, ReadingPolicyError, bind_reading_policy, reading_policy_context,
)
from buenapro_worker.prod6.document_selection import listing_items, select_reading_files
from buenapro_worker.prod6.source_cache import SourceCache


NAMESPACE = "prod6-only-v1"
JOB_TYPES = ("process_contract", "download_file", "extract_tdr", "derive_summary", "analyze_match")
IO_TYPES = ("process_contract", "download_file")
# Paid Gemini jobs run one at a time on the coordinator and stop together during a reading pause.
PAID_TYPES = ("extract_tdr", "analyze_match")
LOCK = (2061013, 6)
# A document whose own request was denied this many times goes to manual review.
SOURCE_BLOCK_LIMIT = 3
# A document whose reading Gemini rejected this many times in a row goes to manual review.
PROVIDER_ERROR_LIMIT = 3
# Gemini answers about the account (key, credits, quota), not the document: they stop every
# paid reading for a while and never count against the document.
READING_PAUSE_REASONS = {
    401: "Gemini: llave inválida o sin permiso (401/403)", 402: "Gemini: créditos agotados (402)",
    403: "Gemini: llave inválida o sin permiso (401/403)", 429: "Gemini: límite de uso (429)",
}
# Gemini saturated (not the account, not the document): a short pause that never counts against
# the document. It alerts only when it keeps renewing for longer than TRANSIENT_ALERT_AFTER_SECONDS.
READING_TRANSIENT_CODES = frozenset({500, 502, 503, 504, 529})
TRANSIENT_ALERT_AFTER_SECONDS = 7200
# Requests one free IO job can make (process_contract: detail + two file listings).
IO_REQUEST_COST = 3


def free_http_failure(error: Exception) -> dict[str, Any] | None:
    """Only temporary source HTTP failures may wait for another free polling cycle."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        # 429 is a source block (circuit breaker), never a free retry.
        if status in {408, 425, 500, 502, 503, 504}:
            return {"error_type": type(error).__name__, "http_status": status}
    elif isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return {"error_type": type(error).__name__}
    return None


# Manual review: pending forever, so nothing claims it and no new job is enqueued for it.
HOLD_DOCUMENT_SQL = """UPDATE worker_jobs SET status='pending',
   claimed_by=NULL,claimed_at=NULL,run_after='infinity'::timestamptz,
   payload=payload || %s::jsonb,last_error=%s
   WHERE id=%s AND status='claimed' AND claimed_by=%s
   AND payload->>'source_runtime'=%s
   AND payload->>'scope_profile_id'=%s"""
# A source stop is neither a failure nor a paid attempt: the claim's attempt is given back.
DEFER_FOR_SOURCE_SQL = """UPDATE worker_jobs SET status='pending',
   claimed_by=NULL,claimed_at=NULL,attempts=GREATEST(attempts-1,0),
   run_after=%s,payload=payload || %s::jsonb,last_error=%s
   WHERE id=%s AND status='claimed' AND claimed_by=%s
   AND payload->>'source_runtime'=%s
   AND payload->>'scope_profile_id'=%s"""


def parse_instant(value: Any) -> datetime | None:
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def load_source_block(path: Path) -> dict[str, Any] | None:
    """The source block recorded by an earlier process in state.json, if any.

    Call only while holding the coordinator lock, before this process rewrites the file.
    """
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        block = previous.get("source_block") if isinstance(previous, dict) else None
        until = previous.get("source_blocked_until") if isinstance(previous, dict) else None
    except (OSError, ValueError):
        return None
    if isinstance(block, dict) and parse_instant(block.get("until")) is not None:
        return dict(block)
    if parse_instant(until) is not None:
        return {"until": until, "status": None, "alerted": True}
    return None


def load_reading_pause(path: Path) -> dict[str, Any] | None:
    """The Gemini reading pause recorded by an earlier process in state.json, if any.

    Call only while holding the coordinator lock, before this process rewrites the file.
    """
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        pause = previous.get("reading_pause") if isinstance(previous, dict) else None
    except (OSError, ValueError):
        return None
    if isinstance(pause, dict) and parse_instant(pause.get("until")) is not None:
        return dict(pause)
    return None


def load_attention_alert(path: Path) -> str | None:
    """Fingerprint of the stop already alerted by the previous process, if it ended in attention_required."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        alert = previous.get("attention_alert") if isinstance(previous, dict) else None
    except (OSError, ValueError):
        return None
    return alert if isinstance(alert, str) and alert else None


def load_discovery_cursor(path: Path) -> dict[str, Any] | None:
    """Where the previous search sweep stopped (a valid ``discovery`` record of state.json)."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        cursor = previous.get("discovery") if isinstance(previous, dict) else None
    except (OSError, ValueError):
        return None
    if (isinstance(cursor, dict) and type(cursor.get("next_stream_index")) is int
            and cursor["next_stream_index"] >= 0):
        return cursor
    return None


def stream_start(cursor: dict[str, Any] | None) -> int:
    """Index to start from: where the previous sweep stopped (a changed stream list only shifts the rotation)."""
    return cursor["next_stream_index"] if cursor else 0


def load_discovery_completed_at(path: Path) -> datetime | None:
    """When the last COMPLETE search lap ended, as recorded by an earlier process in state.json."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        completed = previous.get("last_discovery_completed_at") if isinstance(previous, dict) else None
    except (OSError, ValueError):
        return None
    return parse_instant(completed)


def discovery_schedule(cursor: dict[str, Any] | None, completed_at: datetime | None, now: datetime,
                       interval_seconds: int) -> dict[str, Any]:
    """Whether this cycle runs the search, and when the next lap is planned.

    A lap over every stream may take several cycles (``complete`` false in the cursor): it
    continues in the very next cycle, without waiting for the interval. Only a complete lap
    starts the wait; with none on record (first run, fresh receipt folder) the search runs.
    ``next_due_at`` is None while the next search is simply the next cycle.
    """
    if cursor is not None and cursor.get("complete") is False:
        return {"due": True, "reason": "partial_pass_pending", "next_due_at": None}
    if completed_at is None:
        return {"due": True, "reason": "never_completed", "next_due_at": None}
    next_due = min(completed_at, now) + timedelta(seconds=interval_seconds)  # A future stamp never stalls it.
    if now >= next_due:
        return {"due": True, "reason": "interval_elapsed", "next_due_at": next_due.isoformat()}
    return {"due": False, "reason": f"not_due_until {next_due.isoformat()}", "next_due_at": next_due.isoformat()}


def discovery_lap(previous: dict[str, Any] | None, start: int, searched: int, now: datetime) -> dict[str, Any]:
    """Progress of the lap over all streams, which a limit or a pause can split across cycles."""
    if (previous is not None and previous.get("complete") is False
            and type(previous.get("pass_origin_index")) is int
            and type(previous.get("pass_streams_searched")) is int
            and isinstance(previous.get("pass_started_at"), str)):
        origin, done, began = (previous["pass_origin_index"], previous["pass_streams_searched"],
                               previous["pass_started_at"])
    else:
        origin, done, began = start, 0, now.isoformat()
    return {"pass_origin_index": origin, "pass_streams_searched": done + searched, "pass_started_at": began}


def streams_left_in_lap(cursor: dict[str, Any] | None) -> int | None:
    """Streams a resumed lap still has to search, so it ends where it began instead of repeating them."""
    if (cursor is None or cursor.get("complete") is not False
            or type(cursor.get("pass_streams_searched")) is not int
            or type(cursor.get("stream_count")) is not int):
        return None
    left = cursor["stream_count"] - cursor["pass_streams_searched"]
    return left if left >= 1 else None


# Re-derived at each cycle; never carried over from the cursor of an earlier one.
DISCOVERY_SCHEDULE_KEYS = ("due", "skipped_reason", "next_due_at", "interval_seconds")


def discovery_record(cursor: dict[str, Any] | None, *, due: bool, skipped_reason: str | None,
                     next_due_at: str | None, interval_seconds: int) -> dict[str, Any]:
    kept = {key: value for key, value in (cursor or {}).items() if key not in DISCOVERY_SCHEDULE_KEYS}
    return {**kept, "due": due, "skipped_reason": skipped_reason, "next_due_at": next_due_at,
            "interval_seconds": interval_seconds}


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
    """Queue scope of the coordinator.

    With several profiles, ``profile_id`` is the first one: the document reading is
    shared per contract, so the scope only names which coordinator owns the jobs.
    """

    def __init__(self, conn, *, profile_id: str, batch_id: str, reading_policy=None,
                 profile_ids: list[str] | None = None):
        super().__init__(conn)
        self.profile_id, self.batch_id = profile_id, batch_id
        # Companies this runtime evaluates (the first one is also the queue scope).
        self.profile_ids = list(dict.fromkeys(profile_ids or [profile_id]))
        self.reading_policy = reading_policy_context(reading_policy)
        self.suppressed: dict[str, int] = {}
        # Cleanups (temporary source copies) that may run only once the work is committed.
        self.after_commit: list = []

    def run_after_commit(self) -> None:
        pending, self.after_commit = self.after_commit, []
        for action in pending:
            try:
                action()
            except Exception as error:  # A leftover temporary never fails a saved job.
                print(json.dumps({"cleanup_failed": type(error).__name__}), flush=True)

    def dedup_prefix(self) -> str:
        return f"{NAMESPACE}:{self.profile_id}:"

    def enqueue(self, job_type, payload=None, **kwargs):
        # Ingestion does not authorize historical LLM2 or notifications. The one LLM2 job allowed
        # is the automatic evaluation of a JEV-accepted pair queued by prod6/evaluation.py.
        if job_type not in JOB_TYPES or (job_type == "analyze_match" and not (
                (payload or {}).get("source") == "automatic" and (payload or {}).get("origin") == AUTO_ORIGIN
                and (payload or {}).get("profile_id") in self.profile_ids)):
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
        kwargs["dedup_key"] = f"{self.dedup_prefix()}{kwargs.get('dedup_key') or job_type}"
        kwargs["max_attempts"] = 1
        return super().enqueue(job_type, payload, **kwargs)

    def claim_scoped(self, worker_id: str, job_types: tuple[str, ...] = JOB_TYPES) -> Job | None:
        if not job_types or not set(job_types).issubset(JOB_TYPES):
            raise ValueError("Claims must stay inside the PROD6 whitelist")
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


def profile_enabled(repo: ScopedRepository, profile_id: str | None = None) -> bool:
    row = repo.conn.execute(
        """SELECT is_active, identity_json->>'profile_ingestion_enabled' AS enabled
           FROM company_profiles WHERE id=%s""", (profile_id or repo.profile_id,),
    ).fetchone()
    return bool(row and row["is_active"] and row["enabled"] != "false")


def jev_api_key() -> str | None:
    """TYPESAFE_API_KEY from the environment, else the ignored .env.jev.local at the repo root."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    parents = Path(__file__).resolve().parents
    if not key and len(parents) > 5:
        key = (dotenv_values(parents[5] / ".env.jev.local").get("TYPESAFE_API_KEY") or "").strip()
    return key or None


def jev_cycle(repo: ScopedRepository, profile_ids: list[str], *, max_requests: int,
              threshold: float, batch_id: str) -> dict[str, Any]:
    """Score pending decisions and admit the accepted contracts; without a key nothing changes."""
    api_key = jev_api_key()
    if api_key is None:
        return {"status": "jev_unavailable", "reason": "missing_api_key"}
    with JevScreeningClient(api_key) as client:
        result, accepted = score_pending(repo.conn, client, profile_ids=profile_ids,
                                         threshold=threshold, max_requests=max_requests)
    # Contracts accepted by an earlier cycle that crashed before admission are admitted too.
    recovered = unadmitted_accepted(repo.conn, profile_ids=profile_ids)
    result["admission"] = admit_accepted(repo, accepted + recovered, batch_id=batch_id)
    unavailable = result["aborted"] or (result["failed"] and not result["accepted"] + result["rejected"])
    return {"status": "jev_unavailable" if unavailable else "ok",
            **({"reason": "repeated_failures"} if unavailable else {}), **result}


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
        """SELECT d.id_contrato_archivo, d.categoria, d.filename, d.mime, d.sha256_original,
                  d.raw_file_json, c.objeto_codigo,
                  EXISTS(SELECT 1 FROM tdr_extractions e
                         WHERE e.contract_document_id=d.id AND e.is_current AND e.quality<>'failed') AS read
           FROM contract_documents d JOIN seace_contracts c ON c.id_contrato=d.id_contrato
           WHERE d.id_contrato=%s ORDER BY d.id""",
        (contract_id,),
    ).fetchall()
    by_archive = {int(row["id_contrato_archivo"]): row for row in rows}
    primary, attachments = listing_items(rows)
    try:
        objeto = int(rows[0]["objeto_codigo"]) if rows else None
    except (KeyError, TypeError, ValueError):
        objeto = None
    count = 0
    # The primary PDF, or the attachment PDF that is the requirement when there is none.
    for item in select_reading_files(primary, attachments, objeto).files:
        archive_id = int(item["idContratoArchivo"])
        row = by_archive[archive_id]
        if row.get("read"):
            continue
        sha = str(row.get("sha256_original") or "")
        if sha and not sha.startswith("pending:") and row.get("mime") not in (None, "application/pdf"):
            continue  # Downloaded already and not a PDF: asking SEACE again cannot make it readable.
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


def document_sha(conn, contract_id: int, archive_id: int) -> Any:
    row = conn.execute(
        "SELECT sha256_original FROM contract_documents WHERE id_contrato=%s AND id_contrato_archivo=%s",
        (contract_id, archive_id),
    ).fetchone()
    return row.get("sha256_original") if row else None


def release_source_copy(repo: ScopedRepository, cache: SourceCache, contract_id: int,
                        archive_id: int) -> None:
    """Drop the temporary download of a document whose job ends without reading it, once committed."""
    repo.after_commit.append(partial(cache.discard, document_sha(repo.conn, contract_id, archive_id)))


def evaluate_match(settings: Settings, repo: ScopedRepository, job: Job, receipt_dir: Path) -> dict[str, Any]:
    """LLM2 for one JEV-accepted pair; everything that can be checked for free is checked first.

    The decision, the company rules, the reading and the match are read again at run time: a job
    waiting in the queue never pays for a pair that stopped qualifying. Notifications stay off.
    """
    payload = job.payload
    if payload.get("source") != "automatic" or payload.get("origin") != AUTO_ORIGIN:
        raise ValueError("analyze_match outside the automatic PROD6 evaluation origin")
    contract_id, profile_id = int(payload["id_contrato"]), str(payload["profile_id"])
    if profile_id not in repo.profile_ids:
        return {"outcome": "skipped", "reason": "profile_out_of_scope"}
    if not profile_enabled(repo, profile_id):
        return {"outcome": "skipped", "reason": "profile_paused"}
    rows = evaluation_rows(repo.conn, profile_ids=[profile_id], contract_id=contract_id)
    reason = skip_reason(rows[0], include_stale=True) if rows else "decision_not_accepted"
    if reason:
        return {"outcome": "skipped", "reason": reason}
    row = rows[0]
    # A job that was already claimed once may only replay its own durable answer, unless the
    # provider proved with an HTTP error that nothing was billed.
    guarded = GuardedMatchAnalyzer(settings, receipt_dir / "evaluations",
                                   allow_new_requests=job.attempts <= 1)
    try:
        while True:
            try:
                result = analyze_match_job(
                    settings, repo, id_contrato=contract_id, profile_id=profile_id,
                    business_line_id=payload.get("business_line_id"), source="automatic",
                    analyzer=guarded, notifications=False,
                    extra_meta={"evaluation_origin": AUTO_ORIGIN, "jev_decision_id": int(row["decision_id"]),
                                "jev_score": float(row["score"]) if row["score"] is not None else None})
                break
            except ModelOutputMismatch as error:
                # The model skipped or reordered requirements: only this pair fails. Nothing was
                # written yet, so the same request may be sent once more; after that the pair
                # stays without automatic evaluation (skip_reason) and the web offers the manual one.
                invalid = guarded.retire_invalid_answer()
                if invalid > INVALID_ANSWER_RESENDS or not guarded.allow_new_requests:
                    return {"outcome": INVALID_OUTCOME, "reason": error.code, "invalid_answers": invalid,
                            "extraction_id": int(row["extraction_id"]),
                            **({"provider_answered": True} if guarded.provider_answered else {})}
    finally:
        guarded.close()
    return {"outcome": "unchanged" if result.get("skipped") else "evaluated", **result,
            **({"provider_answered": True} if guarded.provider_answered else {})}


def dispatch(settings: Settings, repo: ScopedRepository, job: Job, receipt_dir: Path):
    payload = job.payload
    contract_id = int(payload["id_contrato"])
    common = {"batch_id": repo.batch_id, "segment": payload.get("segment")}
    # Private temporary copy of what download_file fetched, so extract_tdr reads it
    # instead of downloading the same file again. Nothing is created until a store.
    cache = SourceCache(receipt_dir / "source-cache")
    if (job.job_type in IO_TYPES
            and get_commercial_policy(payload) != get_commercial_policy(repo.reading_policy)):
        raise ReadingPolicyError("Queued PROD6 IO differs from the runtime policy")
    if not eligible_contract(repo, contract_id):
        if job.job_type == "extract_tdr":
            release_source_copy(repo, cache, contract_id, int(payload["id_contrato_archivo"]))
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
                                   id_contrato_archivo=archive_id, source_cache=cache, **common)
        if (before and not str(before["sha256_original"]).startswith("pending:")
                and before["sha256_original"] != result["sha256_original"]):
            # The superseded document's temporary copy has no job left to serve.
            repo.after_commit.append(partial(cache.discard, before["sha256_original"]))
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
            release_source_copy(repo, cache, contract_id, archive_id)
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
            release_source_copy(repo, cache, contract_id, archive_id)
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
        guarded = GuardedGeminiExtractor(settings, receipt_dir / "readings", **options)
        extractor = HashBoundExtractor(guarded, document["sha256_original"])
        extraction_id = extract_tdr_job(settings, repo, id_contrato=contract_id,
                                       id_contrato_archivo=archive_id, extractor=extractor,
                                       source_cache=cache, **common)
        # The reading is saved once this transaction commits; only then is the copy dropped.
        repo.after_commit.append(partial(cache.discard, document["sha256_original"]))
        return {"outcome": "read_or_reused", "extraction_id": extraction_id,
                **({"provider_answered": True} if guarded.provider_answered else {})}
    if job.job_type == "derive_summary":
        result = derive_summary_job(repo, id_contrato=contract_id,
                                    extraction_id=int(payload["extraction_id"]), **common)
        if not result["requires_human_review"]:
            # The reading is usable: queue the evaluation of every company JEV accepted it for.
            queued = enqueue_evaluations(repo, profile_ids=repo.profile_ids, contract_id=contract_id,
                                         include_stale=True)
            result["evaluations"] = {key: queued[key] for key in
                                     ("candidates", "enqueued", "already_queued", "daily_limited", "skipped")}
        return result
    if job.job_type == "analyze_match":
        return evaluate_match(settings, repo, job, receipt_dir)
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
        repo.run_after_commit()
        return outcome


def run(settings: Settings, *, profile_ids: list[str], receipt_dir: Path, once: bool = False,
        interval_seconds: int = 1800, max_pages: int = 5, max_candidates: int = 200,
        http_workers: int = 3, io_workers: int = 2, max_jev_requests: int = 200,
        jev_threshold: float = 60, llm1_commercial_taxonomy: str | None = None,
        max_source_requests: int | None = None, max_discovery_requests: int = 24,
        discovery_interval_seconds: int | None = None) -> int:
    profile_ids = list(dict.fromkeys(profile_ids))
    if not profile_ids:
        raise ValueError("At least one profile is required")
    profile_id = profile_ids[0]  # Queue scope; see ScopedRepository.
    if max_jev_requests < 0 or not 0 <= jev_threshold <= 100:
        raise ValueError("JEV requests must not be negative and the threshold must be 0-100")
    requested_policy = reading_policy_context(
        {POLICY_KEY: llm1_commercial_taxonomy} if llm1_commercial_taxonomy is not None else None)
    if interval_seconds < 60 or max_pages < 1 or max_candidates < 1:
        raise ValueError("Positive bounded limits required; minimum poll interval is 60 seconds")
    if not 1 <= http_workers <= 4 or not 1 <= io_workers <= 4:
        raise ValueError("HTTP/IO parallelism must stay between one and four")
    if max_source_requests is None:
        max_source_requests = default_max_source_requests()
    if max_source_requests < 1:
        raise ValueError("The PROD6 request budget per cycle must be at least one")
    if max_discovery_requests < 1 or max_discovery_requests < max_pages:
        raise ValueError("The discovery request limit must be positive and cover max-pages, "
                         "or a stream could never finish")
    # Discovery never takes more than this share of a cycle that searches: the rest is left to IO.
    # A cycle without search leaves the whole budget to IO.
    discovery_limit = min(max_discovery_requests, max_source_requests)
    if discovery_interval_seconds is None:
        discovery_interval_seconds = default_discovery_interval_seconds()
    if discovery_interval_seconds < 0:
        raise ValueError("The discovery interval must not be negative (0 searches in every cycle)")
    min_request_interval()  # An invalid pace or backoff in the environment stops the start.
    backoff_seconds = block_backoff_seconds()
    reading_backoff = reading_backoff_seconds()
    transient_backoff = reading_transient_backoff_seconds()
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
            notify_ops(f"BuenaPro PROD6: servicio detenido (attention_required): "
                       f"{startup['error_type']} en el arranque ({startup_phase}); requiere revisión.")
            print(json.dumps(startup), flush=True)
            return 2
        egress_down = False
        jev_down = False
        # A 403/429 pause survives restarts: the previous process left it in state.json.
        source_block = load_source_block(receipt_dir / "state.json")
        # The rotation of the search sweep survives restarts the same way.
        discovery_cursor = load_discovery_cursor(receipt_dir / "state.json")
        # So does the end of the last complete lap, which sets when the search is due again.
        discovery_completed_at = load_discovery_completed_at(receipt_dir / "state.json")
        # So do a Gemini reading pause and the stop already reported to operations.
        reading_pause = load_reading_pause(receipt_dir / "state.json")
        attention_alerted = load_attention_alert(receipt_dir / "state.json")

        def publish_discovery(state: dict[str, Any], *, due: bool, skipped_reason: str | None = None) -> None:
            """Search schedule in state.json: this cycle's decision and the next planned lap."""
            planned = discovery_schedule(discovery_cursor, discovery_completed_at,
                                         datetime.now(timezone.utc), discovery_interval_seconds)
            state["discovery"] = discovery_record(discovery_cursor, due=due, skipped_reason=skipped_reason,
                next_due_at=planned["next_due_at"], interval_seconds=discovery_interval_seconds)
            if discovery_completed_at is not None:
                state["last_discovery_completed_at"] = discovery_completed_at.isoformat()

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

        def open_block(state: dict[str, Any], status: int | None) -> datetime:
            """PROD6 answered 403/429: no PROD6 request until the backoff passes.

            One alert per block episode; a block that returns right after a probe keeps
            its original alert, and the recovery alert closes the episode.
            """
            nonlocal source_block
            pacing.PACER.trip(status)
            now = datetime.now(timezone.utc)
            current = parse_instant(source_block.get("until")) if source_block else None
            if current is not None and current > now:
                return current
            until = now + timedelta(seconds=backoff_seconds)
            if not (source_block and source_block.get("alerted")):
                notify_ops(f"BuenaPro PROD6: SEACE answered HTTP {status or 'unknown'}; source blocked. "
                           f"No PROD6 requests until {until.isoformat()} ({backoff_seconds}s); "
                           "local work continues.")
            source_block = {"status": status, "reason": f"HTTP {status or 'unknown'} from PROD6",
                            "since": (source_block or {}).get("since") or now.isoformat(),
                            "until": until.isoformat(), "alerted": True}
            state.update(source_block=source_block, source_blocked_until=source_block["until"])
            write_state(receipt_dir / "state.json", state)
            return until

        def block_recovered(state: dict[str, Any]) -> None:
            nonlocal source_block
            if source_block is not None:
                notify_ops("BuenaPro PROD6: SEACE source recovered after the block; PROD6 requests resumed.")
                source_block = None
                state.pop("source_block", None)
                state.pop("source_blocked_until", None)

        def hold_document(state: dict[str, Any], held: Job, http_status: int, *,
                          count: int | None = None, provider: ProviderRejectedError | None = None) -> None:
            """Manual review: no automatic retry and no new job for this document.

            ``provider`` marks a document whose reading Gemini rejected repeatedly (``count`` is then
            ``provider_error_count``); otherwise the source denied it (``source_block_count``).
            """
            block = {"http_status": http_status, "reason": "manual_review_required"}
            count_key = "source_block_count"
            origin = "source document"
            if provider is not None:
                block = {"provider_http_status": http_status,
                         **({"provider_status": provider.status} if provider.status else {}),
                         "reason": "manual_review_required"}
                count_key = "provider_error_count"
                origin = "Gemini evaluation" if held.job_type == "analyze_match" else "Gemini reading"
            with conn.transaction():
                changed = conn.execute(HOLD_DOCUMENT_SQL, (
                    json.dumps({"source_document_block": block,
                                **({count_key: count} if count is not None else {})}),
                    f"PROD6 {origin} HTTP {http_status}; manual review; no automatic retry",
                    held.id, worker_id, NAMESPACE, profile_id))
                if changed.rowcount != 1:
                    raise RuntimeError("Blocked document ownership changed")
            archive_id = held.payload.get("id_contrato_archivo")
            if archive_id is not None:
                # No reading will use the temporary copy of a document under manual review.
                SourceCache(receipt_dir / "source-cache").discard(
                    document_sha(conn, int(held.payload["id_contrato"]), int(archive_id)))
            state.setdefault("blocked_documents", []).append({"job_id": held.id,
                "contract_id": held.payload["id_contrato"],
                "file_id": archive_id, **block})
            write_state(receipt_dir / "state.json", state)

        def defer_for_source(state: dict[str, Any], deferred: Job, error: Exception, stop_kind: str) -> None:
            """A PROD6 stop is neither a document failure nor a paid attempt.

            Blocked: back to pending until the pause ends. Only a response the document's
            own request received counts toward ``source_block_count``; a request the open
            breaker refused never left the machine. Budget spent: next polling cycle.
            """
            if stop_kind == "blocked":
                status = blocking_status(error) or pacing.PACER.blocked_status
                run_after = open_block(state, status)
                count = int(deferred.payload.get("source_block_count") or 0)
                patch: dict[str, Any] = {}
                if isinstance(error, httpx.HTTPStatusError):
                    count += 1
                    patch["source_block_count"] = count
                    if count >= SOURCE_BLOCK_LIMIT:
                        hold_document(state, deferred, status or 0, count=count)
                        return
                message = f"PROD6 source blocked (HTTP {status or 'unknown'}); paused until {run_after.isoformat()}"
            else:
                status = None
                run_after = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
                patch = {}
                message = "PROD6 request budget of the cycle spent; next polling cycle"
            with conn.transaction():
                changed = conn.execute(DEFER_FOR_SOURCE_SQL, (
                    run_after, json.dumps(patch), message, deferred.id, worker_id, NAMESPACE, profile_id))
                if changed.rowcount != 1:
                    raise RuntimeError("Deferred job ownership changed")
            state.setdefault("source_deferred", []).append({"job_id": deferred.id,
                "job_type": deferred.job_type, "reason": stop_kind,
                **({"http_status": status} if status else {}), "run_after": run_after.isoformat()})
            write_state(receipt_dir / "state.json", state)

        def reading_paused() -> bool:
            until = parse_instant(reading_pause.get("until")) if reading_pause else None
            return until is not None and until > datetime.now(timezone.utc)

        def open_reading_pause(state: dict[str, Any], error: ProviderRejectedError) -> datetime:
            """Gemini answered an account error or is saturated: no paid reading until the backoff passes.

            Account errors (``PROD6_READING_BACKOFF_SECONDS``) alert once per episode. Saturation
            (``PROD6_READING_TRANSIENT_BACKOFF_SECONDS``) is silent until it has renewed without a
            successful reading for more than two hours, then alerts once; the longer account pause
            is never shortened by it. A failed probe after the pause renews it silently and the
            first successful reading closes the episode.
            """
            nonlocal reading_pause
            now = datetime.now(timezone.utc)
            current = reading_pause
            current_until = parse_instant(current.get("until")) if current else None
            current_kind = (current or {}).get("kind", "account")
            if error.code in READING_PAUSE_REASONS:
                until = now + timedelta(seconds=reading_backoff)
                reason = READING_PAUSE_REASONS[error.code]
                continuing = current is not None and current_kind == "account"
                if not (continuing and current.get("alerted")):
                    notify_ops(f"BuenaPro PROD6: lecturas Gemini en pausa ({reason}); sin lecturas hasta "
                               f"{until.isoformat()} ({reading_backoff}s); búsqueda, JEV y descargas continúan.")
                reading_pause = {"kind": "account", "code": error.code, "status": error.status, "reason": reason,
                                 "since": (current["since"] if continuing and current.get("since") else now.isoformat()),
                                 "until": until.isoformat(), "alerted": True}
            else:
                if current_kind == "account" and current_until is not None and current_until > now:
                    return current_until  # The account pause is longer and rules.
                until = now + timedelta(seconds=transient_backoff)
                reason = f"Gemini saturado ({error.code})"
                # A pause that lapsed unused for long is a new episode; an expired account pause
                # (this was its probe) continues its episode, which already alerted.
                grace = timedelta(seconds=2 * max(transient_backoff, interval_seconds))
                continuing = current is not None and (current_kind == "account"
                                                      or (current_until is not None and now <= current_until + grace))
                since = parse_instant(current.get("since")) if continuing else None
                since = since or now
                alerted = bool(continuing and current.get("alerted"))
                if not alerted and (now - since).total_seconds() > TRANSIENT_ALERT_AFTER_SECONDS:
                    notify_ops(f"BuenaPro PROD6: Gemini saturado desde hace >2 h ({reason}); lecturas pagadas en "
                               f"pausas de {transient_backoff}s, siguiente intento tras {until.isoformat()}; "
                               "búsqueda, JEV y descargas continúan.")
                    alerted = True
                reading_pause = {"kind": "transient", "code": error.code, "status": error.status, "reason": reason,
                                 "since": since.isoformat(), "until": until.isoformat(), "alerted": alerted}
            state.update(reading_pause=reading_pause, reading_paused_until=reading_pause["until"])
            write_state(receipt_dir / "state.json", state)
            return until

        def reading_recovered(state: dict[str, Any]) -> None:
            """A saturation episode that never alerted ends silently."""
            nonlocal reading_pause
            if reading_pause is not None:
                if reading_pause.get("alerted"):
                    notify_ops("BuenaPro PROD6: lecturas Gemini reanudadas; primera lectura exitosa tras la pausa.")
                reading_pause = None
                state.pop("reading_pause", None)
                state.pop("reading_paused_until", None)

        def reject_reading(state: dict[str, Any], rejected: Job, error: ProviderRejectedError) -> None:
            """Gemini answered an HTTP error: nothing was billed and the reading may be retried.

            Like a source stop, the claim's attempt is given back. Account errors (401/402/403/429)
            and saturation (500/502/503/504/529) pause every paid reading and do not penalize the
            document; other codes reschedule this job and count, and the third rejection of the
            same document goes to manual review.
            """
            paused = error.code in READING_PAUSE_REASONS or error.code in READING_TRANSIENT_CODES
            run_after = (open_reading_pause(state, error) if paused
                         else datetime.now(timezone.utc) + timedelta(seconds=interval_seconds))
            count = int(rejected.payload.get("provider_error_count") or 0) + (0 if paused else 1)
            if count >= PROVIDER_ERROR_LIMIT:
                hold_document(state, rejected, error.code, count=count, provider=error)
                return
            with conn.transaction():
                changed = conn.execute(DEFER_FOR_SOURCE_SQL, (
                    run_after, json.dumps({"provider_error_count": count} if count else {}),
                    f"Gemini HTTP {error.code}; reading retried at {run_after.isoformat()}",
                    rejected.id, worker_id, NAMESPACE, profile_id))
                if changed.rowcount != 1:
                    raise RuntimeError("Rejected reading ownership changed")
            state.setdefault("reading_deferred", []).append({"job_id": rejected.id,
                "job_type": rejected.job_type, "http_status": error.code,
                **({"provider_status": error.status} if error.status else {}),
                "provider_error_count": count, "run_after": run_after.isoformat()})
            write_state(receipt_dir / "state.json", state)

        def attention_alert(state: dict[str, Any], error_types: list[str], where: str) -> None:
            """One alert per stop: a restart that fails the same way does not repeat it."""
            nonlocal attention_alerted
            kinds = ", ".join(sorted(set(error_types))) or "unknown"
            fingerprint = f"{kinds}|{where}"
            state["attention_alert"] = fingerprint
            if fingerprint != attention_alerted:
                notify_ops(f"BuenaPro PROD6: servicio detenido (attention_required): {kinds} en {where}; "
                           "requiere revisión.")
                attention_alerted = fingerprint

        def io_allowed() -> bool:
            """IO needs PROD6: closed while the breaker is open or the budget cannot cover a job."""
            if pacing.PACER.blocked:
                return False
            remaining = pacing.PACER.remaining
            return remaining is None or remaining >= IO_REQUEST_COST

        while not stopping:
            batch_id = f"prod6-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
            repo = ScopedRepository(conn, profile_id=profile_id, batch_id=batch_id,
                                    reading_policy=effective_policy, profile_ids=profile_ids)
            started = datetime.now(timezone.utc)
            block_until = parse_instant(source_block.get("until")) if source_block else None
            block_active = block_until is not None and block_until > started
            # The cycle's budget and breaker: an active pause refuses every request from the start.
            pacing.PACER.begin_cycle(max_source_requests, blocked=block_active,
                                     blocked_status=(source_block or {}).get("status"))
            state: dict[str, Any] = {"runtime": NAMESPACE, "profile_id": profile_id,
                "profile_ids": profile_ids, "batch_id": batch_id, "started_at": started.isoformat(),
                "status": "sweeping", "phase": "profile_check", "done": {}, "suppressed": {},
                **effective_policy,
                "parallelism": {"http": http_workers, "io": io_workers, "paid_reading": 1},
                "source_pacing": {"min_interval_seconds": min_request_interval(),
                                  "max_requests": max_source_requests}}
            # The search runs only when due: the first time, while a lap is unfinished, or once the
            # interval since the last complete lap has passed. Other cycles keep the whole budget for IO.
            schedule = discovery_schedule(discovery_cursor, discovery_completed_at, started,
                                          discovery_interval_seconds)
            discovery_due = schedule["due"]
            discovery_skipped = None  # Why a due or undue search did not run in this cycle.
            publish_discovery(state, due=discovery_due)
            if source_block:
                state["source_block"] = source_block
                if block_active:
                    state["source_blocked_until"] = source_block["until"]
            if reading_pause:
                state["reading_pause"] = reading_pause
                if reading_paused():
                    state["reading_paused_until"] = reading_pause["until"]
            write_state(receipt_dir / "state.json", state)
            try:
                if not profile_enabled(repo):
                    state["status"] = "profile_paused"
                    write_state(receipt_dir / "state.json", state)
                    return 2
                # The scope profile was checked above; a paused extra company is skipped.
                active_ids = [profile_id] + [other for other in profile_ids[1:]
                                             if profile_enabled(repo, other)]
                repo.profile_ids = active_ids  # A paused extra company is not evaluated either.
                state["source"] = {}
                if block_active:
                    # The pause from an earlier cycle/process still runs: nothing reaches PROD6.
                    state.update(status="source_blocked", phase="source_blocked")
                    discovery_skipped = "source_blocked"
                    publish_discovery(state, due=discovery_due, skipped_reason=discovery_skipped)
                    write_state(receipt_dir / "state.json", state)
                else:
                    state["phase"] = "egress_check"
                    try:
                        check_egress()
                    except EgressUnavailable as error:
                        egress_backoff(state, type(error).__name__)
                        if once:
                            return 2
                        _wait_for_poll(lambda: stopping, interval_seconds)
                        continue
                    if not discovery_due:
                        discovery_skipped = schedule["reason"]
                        publish_discovery(state, due=False, skipped_reason=discovery_skipped)
                    else:
                        state["phase"] = "source_discovery"
                        source_started = time.monotonic()
                        try:
                            # One sweep for every active company: each stream is read once and
                            # every page feeds each company whose segments include its segment.
                            with conn.transaction():
                                polled = poll_prod6_profiles(settings, repo, profile_ids=active_ids,
                                    max_pages_per_segment=max_pages, max_candidates=max_candidates,
                                    batch_id=batch_id, search_parallelism=http_workers,
                                    max_requests=discovery_limit, start_stream=stream_start(discovery_cursor),
                                    max_streams=streams_left_in_lap(discovery_cursor))
                            by_profile = polled.pop("by_profile", None)
                            cursor = polled.pop("cursor", None)
                            for key, value in polled.items():
                                state["source"][key] = state["source"].get(key, 0) + value
                            if by_profile is not None:
                                state["source_by_profile"] = by_profile
                            if cursor is not None:
                                discovery_cursor = {
                                    **cursor, "max_requests": discovery_limit,
                                    "requests": polled.get("http_requests", 0),
                                    **discovery_lap(discovery_cursor, stream_start(discovery_cursor),
                                                    polled.get("streams_searched", 0), datetime.now(timezone.utc))}
                            if polled.get("source_blocked") or pacing.PACER.blocked:
                                open_block(state, pacing.PACER.blocked_status)
                            elif polled.get("budget_reached") or pacing.PACER.budget_reached:
                                state["source"]["budget_reached"] = 1
                            elif cursor is not None and cursor.get("complete"):
                                # The lap over every stream ended: the wait for the next one starts now.
                                discovery_completed_at = datetime.now(timezone.utc)
                        except Exception as error:
                            if is_egress_failure(error):
                                egress_backoff(state, type(error).__name__)
                                if once:
                                    return 2
                                _wait_for_poll(lambda: stopping, interval_seconds)
                                continue
                            stop_kind = source_stop(error)
                            if stop_kind == "blocked":
                                open_block(state, blocking_status(error) or pacing.PACER.blocked_status)
                            elif stop_kind == "budget":
                                state["source"]["budget_reached"] = 1
                            else:
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
                    if not pacing.PACER.blocked and pacing.PACER.used > 0:
                        block_recovered(state)  # The probe after a pause got through.
                    if discovery_due:
                        state["source_seconds"] = round(time.monotonic() - source_started, 3)
                        publish_discovery(state, due=True)
                state["phase"] = "jev_screening"
                state["jev"] = jev_cycle(repo, active_ids, max_requests=max_jev_requests,
                                         threshold=jev_threshold, batch_id=batch_id)
                if state["jev"]["status"] == "jev_unavailable":
                    if not jev_down:
                        notify_ops(f"BuenaPro PROD6: JEV screening unavailable ({state['jev']['reason']}); "
                                   f"contracts stay pending, retrying every {interval_seconds}s.")
                        jev_down = True
                elif jev_down:
                    notify_ops("BuenaPro PROD6: JEV screening recovered.")
                    jev_down = False
                state["status"] = "processing"
                state["phase"] = "processing"
                write_state(receipt_dir / "state.json", state)
                budget_held = False
                while not stopping:
                    if not profile_enabled(repo):
                        state["status"] = "profile_paused"
                        write_state(receipt_dir / "state.json", state)
                        return 2
                    io_batch = []
                    io_open = io_allowed()
                    if io_open:
                        with conn.transaction():
                            for _ in range(io_workers):
                                io_job = repo.claim_scoped(worker_id, IO_TYPES)
                                if not io_job:
                                    break
                                io_batch.append(io_job)
                    elif not pacing.PACER.blocked and not budget_held:
                        # Not enough budget left for a free job: what is pending waits.
                        budget_held = bool(conn.execute(
                            """SELECT 1 FROM worker_jobs WHERE status='pending' AND run_after<=now()
                               AND payload->>'source_runtime'=%s AND payload->>'scope_profile_id'=%s
                               AND job_type=ANY(%s) LIMIT 1""",
                            (NAMESPACE, profile_id, list(IO_TYPES))).fetchone())
                    if io_batch:
                        errors = []
                        failed_types = []
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
                                    stop_kind = source_stop(error)
                                    if stop_kind is not None:
                                        defer_for_source(state, io_job, error, stop_kind)
                                        continue
                                    if (io_job.job_type == "download_file" and isinstance(error, httpx.HTTPStatusError)
                                            and error.response.status_code in {401, 404, 410}):
                                        hold_document(state, io_job, error.response.status_code)
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
                                    failed_types.append((io_job.job_type, type(error).__name__))
                                    continue
                                state["done"][io_job.job_type] = state["done"].get(io_job.job_type, 0) + 1
                                state["last_outcome"] = {"job_id": io_job.id,
                                    "contract_id": io_job.payload["id_contrato"],
                                    "type": io_job.job_type, "result": outcome}
                                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                                write_state(receipt_dir / "state.json", state)
                        if errors:
                            state.update(status="attention_required", io_errors=errors)
                            attention_alert(state, [kind for _, kind in failed_types],
                                            ", ".join(sorted({job_type for job_type, _ in failed_types})))
                            write_state(receipt_dir / "state.json", state)
                            print(json.dumps(state), flush=True)
                            return 2
                        if state.get("egress_deferred"):
                            break  # Remaining IO waits for the next cycle's egress check.
                        continue
                    # IO needs PROD6 too: with the source paused or the budget spent only
                    # reading and summaries (which never ask the source unless a copy is gone) run;
                    # during a Gemini pause the reading itself waits as well.
                    claimable = tuple(kind for kind in JOB_TYPES
                                      if (io_open or kind not in IO_TYPES)
                                      and not (kind in PAID_TYPES and reading_paused()))
                    with conn.transaction():
                        job = (repo.claim_scoped(worker_id) if claimable == JOB_TYPES
                               else repo.claim_scoped(worker_id, claimable))
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
                        repo.run_after_commit()
                    except Exception as error:
                        repo.after_commit.clear()
                        # Reading needs its document again only when the temporary copy is gone;
                        # a refused download happens before any paid request, so it is not an attempt.
                        stop_kind = source_stop(error) if job.job_type == "extract_tdr" else None
                        if stop_kind is not None:
                            defer_for_source(state, job, error, stop_kind)
                            continue
                        if isinstance(error, ProviderRejectedError):
                            reject_reading(state, job, error)
                            continue
                        # No automatic retry, and no raw provider/PII exception in logs.
                        with conn.transaction():
                            conn.execute("""UPDATE worker_jobs SET status='pending',claimed_by=NULL,
                               claimed_at=NULL,last_error=%s WHERE id=%s""",
                               (f"PROD6 attention required: {type(error).__name__}; see private receipt", job.id))
                        state.update(status="attention_required", job_id=job.id,
                                     job_type=job.job_type, error_type=type(error).__name__)
                        attention_alert(state, [type(error).__name__], job.job_type)
                        write_state(receipt_dir / "state.json", state)
                        print(json.dumps(state), flush=True)
                        return 2
                    if job.job_type in PAID_TYPES and outcome.get("provider_answered"):
                        reading_recovered(state)
                    state["done"][job.job_type] = state["done"].get(job.job_type, 0) + 1
                    state["last_outcome"] = {"job_id": job.id, "contract_id": job.payload["id_contrato"],
                                              "type": job.job_type, "result": outcome}
                    state["suppressed"] = repo.suppressed
                    state["updated_at"] = datetime.now(timezone.utc).isoformat()
                    write_state(receipt_dir / "state.json", state)
                if not block_active and not pacing.PACER.blocked and pacing.PACER.used > 0:
                    block_recovered(state)  # A cycle without search: the first IO request was the probe.
                state["suppressed"] = repo.suppressed
                incomplete = bool(state.get("blocked_documents") or repo.suppressed.get("source_document_blocked"))
                jev_unavailable = state["jev"]["status"] == "jev_unavailable"
                reading_stopped = reading_paused()
                blocked_cycle = pacing.PACER.blocked or "source_blocked_until" in state
                budget_reached = bool(pacing.PACER.budget_reached or state["source"].get("budget_reached")
                                      or budget_held or any(item["reason"] == "budget"
                                                            for item in state.get("source_deferred", [])))
                state["source_requests"] = {"used": pacing.PACER.used, "max": max_source_requests,
                                            "budget_reached": budget_reached}
                if state.get("egress_deferred"):
                    egress_alert("io_jobs")
                state["status"] = ("stopped" if stopping else "source_blocked" if blocked_cycle
                                   else "sweep_blocked" if incomplete
                                   else "egress_backoff" if state.get("egress_deferred")
                                   else "jev_unavailable" if jev_unavailable
                                   else "reading_paused" if reading_stopped
                                   else "sweep_deferred" if state.get("deferred_io")
                                   else "source_budget_reached" if budget_reached else "sweep_drained")
                if block_active or blocked_cycle:
                    state["source_coverage"] = "not_polled" if block_active else "bounded_partial"
                elif (state["source"].get("capped_segments") or state["source"].get("limit_reached")
                        or state["source"].get("discovery_limit_reached") or budget_reached):
                    state["source_coverage"] = "bounded_partial"
                elif not discovery_due:
                    state["source_coverage"] = "discovery_not_due"
                else:
                    state["source_coverage"] = "configured_scope_complete"
                publish_discovery(state, due=discovery_due, skipped_reason=discovery_skipped)
                state["finished_at"] = datetime.now(timezone.utc).isoformat()
                attention_alerted = None  # A cycle that ended without a stop closes the previous one.
                write_state(receipt_dir / f"{batch_id}.json", state)
                write_state(receipt_dir / "state.json", state)
                print(json.dumps(state), flush=True)
            except Exception as error:
                state.update(status="attention_required", failed_at=datetime.now(timezone.utc).isoformat(),
                             **failure_diagnostic(error))
                attention_alert(state, [type(error).__name__], f"fase {state.get('phase', 'desconocida')}")
                write_state(receipt_dir / f"{batch_id}.json", state)
                write_state(receipt_dir / "state.json", state)
                print(json.dumps(state), flush=True)
                return 2
            if once or stopping:
                return 2 if (state.get("deferred_io") or state.get("egress_deferred") or incomplete
                             or jev_unavailable or blocked_cycle or reading_stopped) else 0
            _wait_for_poll(lambda: stopping, interval_seconds)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", action="append", dest="profile_ids", required=True,
                        help="Repeat for each company; the first one scopes the job queue")
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=1800)
    parser.add_argument("--max-pages", type=int, default=5,
                        help="Search pages per (object, segment) stream and cycle")
    parser.add_argument("--max-candidates", type=int, default=200)
    parser.add_argument("--http-workers", type=int, default=3)
    parser.add_argument("--io-workers", type=int, default=2)
    parser.add_argument("--max-jev-requests", type=int, default=200,
                        help="JEV queries per cycle (one per contract, all companies together)")
    parser.add_argument("--jev-threshold", type=float, default=60,
                        help="Minimum 0-100 affinity for a company to accept a contract")
    parser.add_argument("--max-source-requests", type=int,
                        help="PROD6 requests per cycle, searches/details/lists/downloads together "
                             "(default PROD6_MAX_SOURCE_REQUESTS or 40); the rest waits for the next cycle")
    parser.add_argument("--max-discovery-requests", type=int, default=24,
                        help="Search requests per cycle (one sweep for all companies); the rest of "
                             "--max-source-requests stays for contract detail/file/download jobs. The sweep "
                             "resumes next cycle at the stream where it stopped; "
                             "it applies only in cycles that search (see --discovery-interval-seconds)")
    parser.add_argument("--discovery-interval-seconds", type=int,
                        help="Run the search only when the last COMPLETE lap over all streams is older than "
                             "this (default PROD6_DISCOVERY_INTERVAL_SECONDS or 7200; 0 searches in every "
                             "cycle). A lap cut short by a limit or a pause continues in the next cycle; "
                             "cycles without search leave all of --max-source-requests to IO")
    parser.add_argument("--llm1-commercial-taxonomy", choices=[TAXONOMY_VERSION],
                        help="Explicit fresh receipt namespace only: commercial classification in new readings")
    args = parser.parse_args()
    raise SystemExit(run(Settings(), **vars(args)))


if __name__ == "__main__":
    main()
