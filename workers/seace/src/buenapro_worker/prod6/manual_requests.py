"""Manual grading («Calificar») of PROD6 contracts, driven by ``manual_grading_requests``.

The web records the request. A ``direct`` row was evaluated by the web itself and is ignored here; a
``queued`` row belongs to this runtime and moves
``pending -> reading (process_contract queued) -> evaluating (analyze_match queued) -> done | failed``.
Failure reasons: reading_requires_review, reading_failed, no_documents, contract_not_found,
profile_inactive, invalid_model_output, reading_timeout.

Every pass is a function of the row and of the database, and each transition is a compare-and-set on
the status inside the transaction that holds the row lock, so a request is never processed twice and a
crash leaves it where it was. A manual pair skips the automatic checks (JEV decision, automation rules,
daily limit, open contract: a closed contract can be graded) but still needs an active company and a
valid reading. Its analyze_match shares the dedup key of the automatic one, so a pair is paid once.
"""
from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from typing import Any

import psycopg

from buenapro_worker.jobs.extract_tdr import enqueue_derive_summary
from buenapro_worker.jobs.poll_prod6_profile import enqueue_process_contract
from buenapro_worker.prod6.document_selection import listing_items, select_reading_files
from buenapro_worker.prod6.evaluation import AUTO_ORIGIN, BUSINESS_LINE_SQL, INVALID_OUTCOME

MANUAL_ORIGIN = "prod6_manual_request_v1"
# analyze_match payload source: _automatic_jobs_today counts only 'automatic', so these never count.
MANUAL_SOURCE = "manual_request"
SOURCE_KEY = "seace_prod6"
CLAIM_BATCH = 20
SWEEP_BATCH = 100
STUCK_HOURS = 24
READING_JOBS = ("process_contract", "download_file", "extract_tdr", "derive_summary")

# Same notion of a valid reading as the web: a current extraction, not failed, not under review (plus
# derived requirements). ``match_extraction_id`` is the reading the existing match was made from.
PAIR_SQL = """
SELECT c.id_contrato IS NOT NULL AS contract_found, c.cubso_segmento, c.objeto_codigo,
       cp.id IS NOT NULL AS profile_active,
       te.extraction_id, te.quality, te.requires_human_review,
       (SELECT count(*) FROM requirement_facets f WHERE f.id_contrato = %(contract)s::bigint
          AND f.extraction_id = te.extraction_id AND f.is_current) AS facets,
       m.id AS match_id, m.breakdown_json->'meta'->>'extraction_id' AS match_extraction_id,
       EXISTS (SELECT 1 FROM worker_jobs j
               WHERE j.job_type = 'analyze_match' AND j.status = 'done'
                 AND j.payload->>'origin' = ANY(%(origins)s)
                 AND j.payload->>'profile_id' = %(profile)s::text
                 AND j.payload->>'id_contrato' = %(contract)s::text
                 AND j.payload->'runtime_result'->>'outcome' = %(invalid)s
                 AND j.payload->'runtime_result'->>'extraction_id' = te.extraction_id::text) AS invalid_answer
FROM (SELECT 1) one
LEFT JOIN seace_contracts c ON c.id_contrato = %(contract)s::bigint
LEFT JOIN company_profiles cp ON cp.id = %(profile)s::uuid AND cp.is_active
LEFT JOIN LATERAL (
  SELECT tx.id AS extraction_id, tx.quality, tx.requires_human_review
  FROM contract_documents d
  JOIN tdr_extractions tx ON tx.contract_document_id = d.id AND tx.is_current
  WHERE d.id_contrato = c.id_contrato
  ORDER BY tx.created_at DESC LIMIT 1
) te ON true
LEFT JOIN matches m ON m.profile_id = %(profile)s::uuid AND m.id_contrato = %(contract)s::bigint
"""

LOCK_SQL = """
SELECT r.id, r.profile_id, r.source_id, r.status FROM manual_grading_requests r
WHERE r.source_key = 'seace_prod6' AND r.mode = 'queued' AND r.status = ANY(%(statuses)s)
  AND r.profile_id = ANY(%(profiles)s::uuid[])
  AND (%(contract)s::bigint IS NULL OR r.source_id = %(contract)s::bigint)
ORDER BY r.updated_at, r.id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
"""

# A request stuck in reading/evaluating never blocks a new one (the active-pair index) for more than a
# day; a pending one of a company this runtime does not serve cannot progress here either.
EXPIRE_SQL = """
UPDATE manual_grading_requests SET status = 'failed', reason = 'reading_timeout', updated_at = now()
WHERE source_key = 'seace_prod6' AND mode = 'queued'
  AND ((status IN ('reading', 'evaluating') AND updated_at < now() - make_interval(hours => %(hours)s))
    OR (status = 'pending' AND created_at < now() - make_interval(hours => %(hours)s)
        AND NOT profile_id = ANY(%(profiles)s::uuid[])))
RETURNING id
"""

# Whether the reading pipeline of a contract still has work: a pending (not held) or claimed job. A job
# held for manual review waits at run_after = infinity and will never run.
PIPELINE_SQL = """
SELECT bool_or(run_after < 'infinity'::timestamptz) AS live,
       bool_or(run_after = 'infinity'::timestamptz AND status = 'pending') AS held
FROM worker_jobs
WHERE job_type = ANY(%(types)s) AND status IN ('pending', 'claimed')
  AND payload->>'source_runtime' = %(namespace)s AND payload->>'id_contrato' = %(contract)s::text
"""

EVALUATION_JOB_SQL = """
SELECT status, (run_after = 'infinity'::timestamptz) AS held FROM worker_jobs
WHERE job_type = 'analyze_match' AND payload->>'origin' = %(origin)s
  AND payload->>'manual_request_id' = %(request)s::text
ORDER BY id DESC LIMIT 1
"""


@contextmanager
def tolerate_missing_table():
    """A worker deployed before migration 0050 simply has no manual requests to serve."""
    try:
        yield
    except psycopg.errors.UndefinedTable:
        pass


def pair_row(conn, contract_id: int, profile_id: str) -> dict[str, Any]:
    return conn.execute(PAIR_SQL, {"contract": contract_id, "profile": str(profile_id), "invalid": INVALID_OUTCOME,
                                   "origins": [AUTO_ORIGIN, MANUAL_ORIGIN]}).fetchone()


def pair_verdict(row: dict[str, Any]) -> tuple[str, str | None]:
    """(verdict, reason) of a pair: failed, done, absent, deriving or ready. Free; nothing is queued here.

    ``absent``: no current reading. ``deriving``: a reading whose requirements are not derived yet.
    ``done``: already graded from this reading (a match of unknown origin is never overwritten).
    """
    if not row["contract_found"]:
        return "failed", "contract_not_found"
    if not row["profile_active"]:
        return "failed", "profile_inactive"
    known = row["match_extraction_id"]
    if row["match_id"] is not None and not known:
        return "done", None
    if row["extraction_id"] is None:
        return "absent", None
    if row["quality"] == "failed":
        return "failed", "reading_failed"
    if row["requires_human_review"]:
        return "failed", "reading_requires_review"
    if row["match_id"] is not None and known == str(row["extraction_id"]):
        return "done", None
    if not row["facets"]:
        return "deriving", None
    if row["invalid_answer"]:
        return "failed", INVALID_OUTCOME
    return "ready", None


def reading_requested(conn, contract_id: int) -> bool:
    """Whether a queued request is reading this contract: its jobs may run even if the contract is closed."""
    with tolerate_missing_table():
        with conn.transaction():
            row = conn.execute(
                """SELECT EXISTS (SELECT 1 FROM manual_grading_requests WHERE source_key = 'seace_prod6'
                     AND mode = 'queued' AND status = 'reading' AND source_id = %s) AS active""",
                (contract_id,)).fetchone()
        return bool(row) and row["active"] is True
    return False


def pending_ids(conn, profile_ids: list[str]) -> set[int]:
    """Queued requests waiting for this runtime (a cheap query; the wake-up of an idle runtime)."""
    with tolerate_missing_table():
        with conn.transaction():
            rows = conn.execute(
                """SELECT id FROM manual_grading_requests WHERE source_key = 'seace_prod6' AND mode = 'queued'
                   AND status = 'pending' AND profile_id = ANY(%s::uuid[])""",
                ([str(profile) for profile in profile_ids],)).fetchall()
        return {int(row["id"]) for row in rows}
    return set()


def move(conn, request: dict[str, Any], status: str, reason: str | None = None) -> str:
    """Compare-and-set on the status the caller holds; returns the status the row is in afterwards."""
    changed = conn.execute(
        """UPDATE manual_grading_requests SET status = %s, reason = %s, updated_at = now()
           WHERE id = %s AND status = %s""", (status, reason, request["id"], request["status"])).rowcount
    return status if changed == 1 else request["status"]


def settle(conn, request_id: int, status: str, reason: str | None = None) -> bool:
    """The evaluation job ends its request: only an 'evaluating' one (timed-out ones stay failed)."""
    return conn.execute(
        """UPDATE manual_grading_requests SET status = %s, reason = %s, updated_at = now()
           WHERE id = %s AND status = 'evaluating'""", (status, reason, request_id)).rowcount == 1


def is_evaluating(conn, request_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM manual_grading_requests WHERE id = %s AND source_key = 'seace_prod6'
           AND mode = 'queued' AND status = 'evaluating'""", (request_id,)).fetchone()
    return row is not None


def business_line(conn, contract_id: int, profile_id: str) -> str | None:
    """The line the contract belongs to, by segment or by the keyword lines of the company's decision."""
    decision = conn.execute(
        """SELECT keyword_lines FROM prod6_profile_decisions WHERE id_contrato = %s AND profile_id = %s
           AND decision <> 'superseded' ORDER BY id DESC LIMIT 1""", (contract_id, profile_id)).fetchone()
    line = conn.execute(BUSINESS_LINE_SQL, {
        "contract": contract_id, "profile": profile_id,
        "lines": list(decision["keyword_lines"] or []) if decision else []}).fetchone()
    return str(line["id"]) if line else None


def enqueue_evaluation(repo, row: dict[str, Any], request: dict[str, Any]) -> int | None:
    """The one paid call of the pair. The dedup key is the automatic one: whichever job is queued first
    wins and the other finds the pair taken (None)."""
    profile, contract = str(request["profile_id"]), int(request["source_id"])
    return repo.enqueue("analyze_match", {
        "id_contrato": contract, "profile_id": profile, "business_line_id": business_line(repo.conn, contract, profile),
        "source": MANUAL_SOURCE, "origin": MANUAL_ORIGIN, "manual_request_id": int(request["id"]),
        "extraction_id": int(row["extraction_id"])},
        queue_name="llm", dedup_key=f"analyze_match:{profile}:{contract}", priority=3)


def dead_end_reason(conn, contract_id: int, objeto_codigo: Any, held: bool) -> str:
    """Why a reading that nothing is producing anymore will not come: no readable document, or a failure."""
    if held:
        return "reading_failed"
    documents = conn.execute(
        """SELECT id_contrato_archivo, categoria, filename, mime, raw_file_json FROM contract_documents
           WHERE id_contrato = %s ORDER BY id""", (contract_id,)).fetchall()
    primary, attachments = listing_items(documents)
    selection = select_reading_files(primary, attachments, objeto_codigo)
    return "no_documents" if not documents or selection.manual_review or not selection.files else "reading_failed"


def advance(repo, request: dict[str, Any]) -> str:
    """Take one locked request as far as the database allows; returns its status afterwards."""
    conn, status = repo.conn, request["status"]
    contract_id = int(request["source_id"])
    row = pair_row(conn, contract_id, str(request["profile_id"]))
    verdict, reason = pair_verdict(row)
    if verdict == "failed":
        return move(conn, request, "failed", reason)
    if verdict == "done":
        return move(conn, request, "done")
    if status == "evaluating":  # Its job is queued; evaluate_match settles it. A lost or held job fails it.
        job = conn.execute(EVALUATION_JOB_SQL, {"origin": MANUAL_ORIGIN, "request": int(request["id"])}).fetchone()
        live = job is not None and job["status"] in ("pending", "claimed") and not job["held"]
        return status if job is None or live else move(conn, request, "failed", INVALID_OUTCOME)
    if verdict == "ready":
        if enqueue_evaluation(repo, row, request) is None:
            return status  # An automatic job holds the pair: wait for it (done) or for its end (queue ours).
        return move(conn, request, "evaluating")
    pipeline = conn.execute(PIPELINE_SQL, {"types": list(READING_JOBS), "namespace": repo.namespace,
                                           "contract": contract_id}).fetchone()
    if pipeline["live"] is True:
        return move(conn, request, "reading") if status == "pending" else status
    if status != "pending":  # It was started and nothing runs anymore: the reading will not come.
        return move(conn, request, "failed",
                    dead_end_reason(conn, contract_id, row["objeto_codigo"], pipeline["held"] is True))
    if verdict == "deriving":  # Read but never summarised: only the summary is missing.
        enqueue_derive_summary(repo, id_contrato=contract_id, extraction_id=int(row["extraction_id"]),
                               batch_id=repo.batch_id)
    else:
        segment = str(row["cubso_segmento"] or "")
        enqueue_process_contract(repo, contract_id, segment=int(segment) if segment.isdigit() else None,
                                 batch_id=repo.batch_id, dedup_key=f"process_contract:manual:{contract_id}")
    return move(conn, request, "reading")


def safe_advance(repo, request: dict[str, Any]) -> str:
    """One broken request never stops the automatic pipeline or the service; it fails on its own."""
    try:
        with repo.conn.transaction():
            return advance(repo, request)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise
    except Exception as error:
        print(json.dumps({"manual_request_failed": int(request["id"]), "error_type": type(error).__name__}), flush=True)
        with repo.conn.transaction():
            return move(repo.conn, request, "failed", "reading_failed")


def lock_requests(repo, statuses: tuple[str, ...], limit: int, contract_id: int | None = None) -> list[dict[str, Any]]:
    return list(repo.conn.execute(LOCK_SQL, {
        "statuses": list(statuses), "profiles": [str(profile) for profile in repo.profile_ids],
        "contract": contract_id, "limit": limit}).fetchall())


def run_batch(repo, statuses: tuple[str, ...], limit: int, stats: Counter[str], contract_id: int | None = None) -> None:
    for request in lock_requests(repo, statuses, limit, contract_id):
        after = safe_advance(repo, request)
        stats[after if after != request["status"] else "waiting"] += 1


def advance_reading(repo, contract_id: int) -> dict[str, int]:
    """After derive_summary: the requests reading this contract are graded, or failed if the reading is unusable."""
    stats: Counter[str] = Counter()
    try:
        with tolerate_missing_table():
            with repo.conn.transaction():
                run_batch(repo, ("reading",), SWEEP_BATCH, stats, contract_id)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise
    except psycopg.Error:  # Never fail the summary that was just derived because of the manual requests.
        return {"error": 1}
    return {key: count for key, count in stats.items() if count}


def step(repo) -> dict[str, int]:
    """One pass of the runtime: expire the stuck, settle the started, claim the pending (at most 20).

    The counts are the statuses requests reached (``waiting`` for those that did not move).
    """
    stats: Counter[str] = Counter()
    try:
        with tolerate_missing_table():
            with repo.conn.transaction():
                stats["expired"] += len(list(repo.conn.execute(EXPIRE_SQL, {
                    "hours": STUCK_HOURS, "profiles": [str(profile) for profile in repo.profile_ids]}).fetchall()))
                run_batch(repo, ("reading", "evaluating"), SWEEP_BATCH, stats)
                run_batch(repo, ("pending",), CLAIM_BATCH, stats)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise
    except psycopg.Error:  # Schema drift or a bad row: the automatic work goes on; state.json counts the error.
        return {"error": 1}
    return {key: count for key, count in stats.items() if count}
