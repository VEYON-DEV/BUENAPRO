"""Re-read retained PROD2 embedding-preparation reviews with a new reading configuration.

A selected case that ended in EmbeddingPreparationReview
(retrieval_context_needs_expansion_or_review) keeps its prepared PDF, its OCR
caches and its paid vectors. This script reads exactly those cases again, with
a new embedding reading configuration (for example the v6 specific-section
policy), and records one immutable outcome per case in a new private folder.

  --plan     Offline. Validates the original evidence and freezes a manifest.
  --execute  Requires the manifest and unchanged evidence. One LLM1 call per case.

Never done here: discovery, Redis, document downloads, selector, JEV, full-PDF
generation, Luna fallback, or buying embeddings (max_embedding_calls=0). A corpus
whose vectors are not all stored in PostgreSQL fails without payment.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg
from psycopg.rows import dict_row

from buenapro_worker.settings import Settings
from buenapro_worker.prod2.analysis import extraction_request_hash
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.embedding_corpus import CorpusStopped
from buenapro_worker.prod2.embedding_retrieval import _config as retrieval_config
from buenapro_worker.prod2.embedding_runtime import (
    EmbeddingPreparationReview, config_hash, load_config,
    prepare_retrieved_input, validate_frozen_config)
from buenapro_worker.prod2.handoff_guards import HandoffGuards, document_preparation_review
from buenapro_worker.prod2.input_policy import prepare_input
from buenapro_worker.prod2.llm1_completion import persist_completed_llm1
from buenapro_worker.prod2.model_routing import resolve_llm1_model
from buenapro_worker.prod2.pdf_preparation import _result
from buenapro_worker.prod2.selected_control import LimitReached, RunLimits
from buenapro_worker.prod2.selected_payments import audit_summary
from buenapro_worker.prod2.selected_read import paid_selected_read
import run_prod2_selected as selected

VERSION = "prod2_review_reread_v1"
REVIEW_REASON = "retrieval_context_needs_expansion_or_review"
OPEN_STATES = {"registration_open", "submission_open"}
# Same window as the sector follow-up this script is modelled on: 6 hours.
DEFAULT_MAX_OBSERVATION_HOURS = 6
MODEL_ROUTINGS = ("original", "fixed")
MAX_OBSERVATION_HOURS = 168
CLOCK_SKEW_SECONDS = 60
MODEL_WORKERS = 2
NO_EMBEDDING_CALLS = 0
SHA = re.compile(r"[0-9a-f]{64}")
STOP = threading.Event()
OUTPUT_LOCK = threading.Lock()


class Refused(ValueError):
    """A case or its preflight does not meet the contract; `reason` is a controlled code."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class MissingVectors(ValueError):
    """A corpus or query vector is not stored in PostgreSQL; this script never buys embeddings."""


class OwnedReviewGuard(HandoffGuards):
    """Dispatch stop plus a check that this connection still owns the coordinator lock."""

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def before_request(self):
        super().before_request()
        if self.owner.closed or self.owner.broken:
            raise ValueError("Production coordinator connection was lost")
        held = self.owner.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks
            WHERE locktype='advisory' AND granted AND pid=pg_backend_pid()
              AND classid::bigint=(hashtext('prod2_accepted_executor')::bigint & 4294967295)
              AND objid=0 AND objsubid=2) AS held""").fetchone()["held"]
        if held is not True:
            raise ValueError("Production coordinator no longer owns its lock")


def literal(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Literal paths are required; symlinks are refused")
    return path


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha(path):
    path = literal(path)
    if not path.is_file():
        raise ValueError("Literal evidence file required")
    return file_digest(path)


def once(path, value):
    """Write a receipt once; an identical replay is accepted, a different one is refused."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or read_private(path) != value:
            raise ValueError("Frozen evidence changed")
        return
    private_write(path, value)


def parse_aware(value, name):
    if not isinstance(value, str):
        raise Refused("timestamp_invalid", name + " is missing")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise Refused("timestamp_invalid", name + " is not an ISO timestamp") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise Refused("timestamp_invalid", name + " has no timezone")
    return moment


def open_observation(record):
    """The part of the lifecycle rule that depends only on the original evidence."""
    if record.get("confirmed_open") is not True or record.get("temporal_state") not in OPEN_STATES:
        raise Refused("not_open", "the contract was not positively observed open "
                      "(confirmed_open and registration_open/submission_open required)")
    if record.get("requires_fresh_lifecycle") is True:
        raise Refused("fresh_lifecycle_required", "the original outcome asks for a fresh lifecycle")
    return parse_aware(record.get("temporal_observed_at"), "temporal_observed_at")


def validate_lifecycle(record, now, deadline, *, max_age_seconds):
    """Only an open contract with a recent open observation and a future deadline may be read.

    Same rule as the sector follow-up: closed contracts are excluded, the observation
    must be recent and the deadline must still be ahead. The deadline comes from
    PostgreSQL because the pilot outcomes do not store `closes_at`.
    """
    observed = open_observation(record)
    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        raise Refused("no_deadline", "PostgreSQL has no precise submission deadline for this contract")
    age = (now - observed).total_seconds()
    if not -CLOCK_SKEW_SECONDS <= age <= max_age_seconds:
        raise Refused("stale_observation", "the open observation is %.1f h old; the limit is %g h "
                      "(--max-observation-hours)" % (age / 3600, max_age_seconds / 3600))
    if deadline <= now:
        raise Refused("closed", "the submission deadline has passed")


def load_json(path):
    try:
        return read_private(path)
    except (OSError, ValueError):
        raise Refused("unreadable_evidence", Path(path).name + " is missing or unreadable") from None


def case_evidence(original, cid):
    """Validate one original case. Returns its frozen facts or raises Refused with the reason."""
    case = original / str(cid)
    if not case.is_dir() or case.is_symlink():
        raise Refused("case_missing", "no case folder in the original run")
    outcome_path = case / "outcome.private.json"
    receipt_path = case / "embedding-preparation-review.private.json"
    outcome = load_json(outcome_path)
    if not isinstance(outcome, dict) or outcome.get("convocation_id") != cid:
        raise Refused("outcome_identity", "outcome.private.json belongs to another convocation")
    if outcome.get("status") != "needs_review" or outcome.get("error_type") != "EmbeddingPreparationReview":
        raise Refused("not_expected_review", "outcome is %r/%r, expected 'needs_review'/'EmbeddingPreparationReview'"
                      % (outcome.get("status"), outcome.get("error_type")))
    receipt = load_json(receipt_path)
    if not isinstance(receipt, dict) or receipt.get("reason") != REVIEW_REASON:
        raise Refused("not_expected_review", "review receipt reason is %r, expected %r"
                      % (receipt.get("reason") if isinstance(receipt, dict) else None, REVIEW_REASON))
    if receipt.get("full_pdf_fallback") is not False or not SHA.fullmatch(str(receipt.get("corpus_hash"))):
        raise Refused("review_receipt_invalid", "the review receipt does not bind a corpus without full-PDF fallback")
    proof = outcome.get("document_preparation_review")
    if proof is not None and (not isinstance(proof, dict) or proof.get("reason") != REVIEW_REASON
                              or proof.get("receipt_sha256") != sha(receipt_path)):
        raise Refused("review_receipt_invalid", "the outcome's typed review proof differs from the receipt")
    if outcome.get("accepted") is not True:
        raise Refused("not_accepted", "the contract was not accepted by the affinity screening")
    for field in ("description", "object_type"):
        if not isinstance(outcome.get(field), str) or not outcome[field].strip():
            raise Refused("outcome_incomplete", "outcome.private.json has no " + field)
    if not SHA.fullmatch(str(outcome.get("detail_hash"))):
        raise Refused("outcome_incomplete", "outcome.private.json has no detail_hash")
    try:
        open_observation(outcome)
    except Refused as error:
        raise Refused(error.reason, "the original outcome fails the lifecycle rule: " + str(error)) from None
    preparations = sorted(case.glob("preparation-cache/selected-analysis-*/manifest.private.json"))
    if len(preparations) != 1:
        raise Refused("preparation_count", "%d retained preparations, exactly 1 required" % len(preparations))
    preparation = preparations[0]
    manifest = load_json(preparation)
    try:
        _result(preparation.parent, manifest, manifest["max_analysis_bytes"])
    except Exception:
        raise Refused("preparation_unverified", "the retained preparation does not match its exact PDF parts") from None
    try:
        costs = audit_summary(case)
    except Exception:
        raise Refused("payment_audit_failed", "the payment receipts of the case cannot be audited") from None
    if (costs["llm1_calls"] or costs["usage_unknown_attempts"] or costs["embedding_usage_unknown_attempts"]
            or costs["embedding_unknown_dispatch_attempts"] or costs["embedding_receipt_conflicts"]):
        raise Refused("payment_state", "the case has an LLM1 attempt or an uncertain payment receipt")
    files = [outcome_path, receipt_path]
    files += sorted(p for p in preparation.parent.rglob("*") if p.is_file() or p.is_symlink())
    persisted = case / "embedding-corpus-persistence.private.json"
    if persisted.exists() or persisted.is_symlink():
        files.append(persisted)
    for folder in sorted(case.glob("embedding-corpus-*")):
        if folder.is_dir():
            files += sorted(folder.glob("*.private.json"))
    return {"convocation_id": cid,
            "original_outcome_sha256": sha(outcome_path),
            "review_receipt_sha256": sha(receipt_path),
            "corpus_hash": receipt["corpus_hash"],
            "retrieval_hash": receipt.get("retrieval_hash"),
            "temporal_observed_at": outcome["temporal_observed_at"],
            "preparation": str(preparation.relative_to(original)),
            "evidence": [{"path": str(p.relative_to(original)), "sha256": sha(p)} for p in files]}


def check_config_compatible(old, new):
    """The new configuration may change only how the context is selected from stored vectors."""
    for key in sorted((set(old) | set(new)) - {"retrieval", "configuration_revision"}):
        if old.get(key) != new.get(key):
            raise ValueError("The new configuration changes '%s'; its corpus or prompt would differ "
                             "from the stored vectors" % key)
    if old["retrieval"]["queries"] != new["retrieval"]["queries"]:
        raise ValueError("The new configuration changes the retrieval queries; their vectors would have to be bought")
    retrieval_config(new["retrieval"])


def frozen_plan(original, folder, ids, config_path, max_llm1_calls, max_observation_hours, model_routing="original"):
    """Return (manifest, rejected). The manifest is a pure function of the evidence and arguments."""
    original, folder = literal(original), literal(folder)
    if not original.is_dir():
        raise ValueError("The original run folder does not exist")
    if folder == original or folder.is_relative_to(original) or original.is_relative_to(folder):
        raise ValueError("Use a new private folder outside the original run")
    if folder.exists() and (not folder.is_dir() or (
            any(folder.iterdir()) and not (folder / "manifest.private.json").is_file())):
        raise ValueError("The folder must be new or own a frozen manifest")
    if not 1 <= max_llm1_calls <= 2 * len(ids):
        raise ValueError("--max-llm1-calls must be between 1 and twice the number of ids "
                         "(one call per case plus its saturation fallback)")
    if not 0 < max_observation_hours <= MAX_OBSERVATION_HOURS:
        raise ValueError("--max-observation-hours must be above 0 and at most %d" % MAX_OBSERVATION_HOURS)
    if model_routing not in MODEL_ROUTINGS:
        raise ValueError("--llm1-model-routing must be one of %s" % ", ".join(MODEL_ROUTINGS))
    old = read_private(original / "manifest.private.json")
    old_config = validate_frozen_config(old)
    if old_config is None:
        raise ValueError("The original run did not use the embeddings reading contract")
    config = load_config(config_path)
    check_config_compatible(old_config, config)
    cases, rejected = [], []
    for cid in ids:
        try:
            cases.append(case_evidence(original, cid))
        except Refused as error:
            rejected.append({"convocation_id": cid, "reason": error.reason, "detail": str(error)})
    plan = {"version": VERSION,
            "original_folder": str(original),
            "folder": str(folder),
            "original_manifest_sha256": sha(original / "manifest.private.json"),
            "original_config_sha256": old["embedding_reading"]["config_sha256"],
            "embedding_config": config,
            "embedding_config_sha256": config_hash(config),
            "ids": list(ids),
            "cases": cases,
            "max_llm1_calls": max_llm1_calls,
            "max_embedding_calls": NO_EMBEDDING_CALLS,
            "max_observation_age_seconds": int(round(max_observation_hours * 3600)),
            **({"llm1_model_routing": model_routing} if model_routing != "original" else {}),
            "model_workers": MODEL_WORKERS,
            "no_discovery": True, "no_source_http": True, "no_redis": True, "no_jev": True,
            "no_embedding_purchase": True, "no_full_pdf_or_luna": True}
    return plan, rejected


def verify_plan(args):
    """The stored manifest must exist and equal what the same arguments produce from today's evidence."""
    folder = literal(args.folder)
    marker = folder / "manifest.private.json"
    if not marker.is_file():
        raise ValueError("No frozen manifest in the folder; run --plan first")
    stored = read_private(marker)
    current, rejected = frozen_plan(args.original, folder, args.ids, args.embedding_config,
                                    args.max_llm1_calls, args.max_observation_hours, getattr(args, "llm1_model_routing", "original"))
    if rejected or current != stored:
        changed = sorted(key for key in set(stored) | set(current) if stored.get(key) != current.get(key))
        raise ValueError("Frozen manifest or original evidence changed (%s)" % ", ".join(changed or ["cases"]))
    return stored


def plan_case(plan, cid):
    return next(c for c in plan["cases"] if c["convocation_id"] == cid)


def prepare_case(plan, cid):
    original = Path(plan["original_folder"])
    manifest_path = original / plan_case(plan, cid)["preparation"]
    manifest = read_private(manifest_path)
    prepared = _result(manifest_path.parent, manifest, manifest["max_analysis_bytes"])
    caches = (original / str(cid)).glob("embedding-corpus-*")
    prepared["embedding_ocr_cache_sources"] = sorted(str(p) for p in caches if p.is_dir())
    return prepared


SOURCE_SQL = """SELECT d.detail_hash, d.proposals_closes_at
    FROM prod2_details d JOIN prod2_discoveries p USING(id_convocatoria)
    WHERE id_convocatoria=%s"""
CURRENT_READING_SQL = "SELECT id FROM prod2_document_extractions WHERE id_convocatoria=%s AND is_current"
PRIOR_ATTEMPTS_SQL = """SELECT id,status,request_hash,claim_token::text FROM prod2_evaluations
    WHERE id_convocatoria=%s AND kind='gemini_extraction'"""


def pg_preflight(conn, plan, cid, record, request_hash=None, selection_context=None):
    """Read-only checks before any provider boundary; repeated right before the LLM1 request."""
    source = conn.execute(SOURCE_SQL, (cid,)).fetchone()
    if not source or source["detail_hash"] != record["detail_hash"]:
        raise Refused("detail_changed", "PostgreSQL holds another ficha than the one the review was read from")
    deadline = source["proposals_closes_at"]
    if record.get("closes_at") is not None and parse_aware(record["closes_at"], "closes_at") != deadline:
        raise Refused("deadline_changed", "PostgreSQL deadline differs from the one in the original outcome")
    validate_lifecycle(record, datetime.now(timezone.utc), deadline,
                       max_age_seconds=plan["max_observation_age_seconds"])
    if conn.execute(CURRENT_READING_SQL, (cid,)).fetchall():
        raise Refused("existing_reading", "An existing reading is preserved; no new payment")
    prior = conn.execute(PRIOR_ATTEMPTS_SQL, (cid,)).fetchall()
    case = Path(plan["folder"]) / str(cid)
    for entry in prior:
        if entry["status"] == "pending":
            continue  # Never claimed, or released after Gemini answered an HTTP error: nothing paid.
        claim_path = case / ("gemini_extraction-" + str(entry["request_hash"]) + ".claim.private.json")
        owned = read_private(claim_path) if request_hash and claim_path.exists() else {}
        # Ours: the primary request or its saturation fallback, both claimed from this folder.
        mine = (entry["id"] == owned.get("id") and entry["claim_token"] == str(owned.get("claim_token"))
                and entry["status"] in {"running", "done"})
        if not mine:
            raise Refused("historical_attempt", "Historical paid attempt preserved; no repayment")
    if selection_context is not None:
        from buenapro_worker.prod2.llm1_completion import _current_selection_receipt
        with conn.cursor() as cursor:
            _current_selection_receipt(cursor, selection_context, record["description"])


def embedding_free_limits(limits, guard):
    """Limits for the retrieval stage: no embedding and no LLM1 request can be reserved here."""
    stage = RunLimits(max_accepted=1, max_candidates=1, max_jev_calls=0, max_llm1_calls=0,
                      max_embedding_calls=NO_EMBEDDING_CALLS, model_workers=MODEL_WORKERS)
    stage.model_slots = limits.model_slots
    stage.handoff_guard = guard
    return stage


def read_case(settings, plan, cid, limits, guard):
    """Read one case. An existing outcome is returned untouched: terminal outcomes are never retried."""
    case = Path(plan["folder"]) / str(cid)
    out = case / "outcome.private.json"
    if out.exists():
        return read_private(out)
    guard.before_request()
    original = Path(plan["original_folder"])
    record = read_private(original / str(cid) / "outcome.private.json")
    old = read_private(original / "manifest.private.json")
    case.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared = prepare_case(plan, cid)
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        pg_preflight(conn, plan, cid, record, selection_context=prepared["selection_context"])
        guard.before_request()
        try:
            prepared = prepare_retrieved_input(conn, settings, prepared, case, embedding_free_limits(limits, guard),
                                               deepcopy(plan["embedding_config"]), convocation_id=cid,
                                               stop_requested=STOP.is_set, memoize_expansion=True)
        except LimitReached as error:
            if STOP.is_set():
                raise
            raise MissingVectors(
                "Corpus or query vectors are not all stored; embeddings are never bought here") from error
        stage = read_private(case / "embedding-stage.private.json")
        if stage["attempt_ids"] or stage["new_token_count"] or stage["known_cost_usd"]:
            raise RuntimeError("An embedding request was made; this script must never buy embeddings")
        prepared, local = prepare_input(prepared, case)
        # "fixed" reads with the base model: the page routing's large model (gemini-2.5-flash-lite)
        # answers 404 to keys created after Google retired it for new users (28/10).
        routed = old if plan.get("llm1_model_routing", "original") == "original" else {
            key: value for key, value in old.items() if key != "llm1_model_routing"}
        model, routing = resolve_llm1_model(routed, prepared["page_count"], settings.gemini_model)
        active = settings.model_copy(update={"gemini_model": model})
        pdf = prepared["pdf_path"].read_bytes()
        first = prepared["document_set"][0]
        anchor = first["document_uuid"]
        anchor_sha = first.get("archive_sha256", first["original_sha256"])
        keys = ("document_scope", "document_set", "document_set_hash", "document_set_identity",
                "selection_context", "llm1_input")
        context = {key: prepared[key] for key in keys}
        request = extraction_request_hash(active, pdf, object_type=record["object_type"],
                                          original_sha256=anchor_sha,
                                          manifest=prepared["source_document_manifest"],
                                          document_context=context)

        def before(issue):
            def checked():
                guard.before_request()
                pg_preflight(conn, plan, cid, record, request, selection_context=prepared["selection_context"])
                issue()
            return checked

        evaluation, bundle, hit, request, execution = paid_selected_read(
            conn, active, case, convocation_id=cid, profile_id=old["profile_id"],
            object_type=record["object_type"], anchor=anchor, anchor_sha=anchor_sha, pdf=pdf,
            prepared=prepared, document_context=context, limits=limits, before_request_factory=before,
            local_text_result=local,
            generation_options={**selected.llm1_generation_options(old), "after_attempt": guard.observe_attempt},
            fallback_config=None)
        saved = persist_completed_llm1(
            conn, convocation_id=cid, document_uuid=anchor,
            extraction={**bundle, "evaluation_id": evaluation, "allowed_models": [model]},
            document_sha256=anchor_sha, source_url=first["source_url"])
        result = {**record, **saved, "status": "llm1_complete",
                  "requires_human_review": bundle["requires_human_review"],
                  "llm1_prior_preserved": False, "llm1_model": model, "llm1_cached": hit,
                  "llm1_evaluation_id": evaluation, "llm1_request_hash": request,
                  "llm1_cost_usd": 0 if hit else bundle["cost_usd"],
                  "facet_count": len(bundle["facets"]),
                  "context_tokens": local["context_tokens"], "context_reasons": local.get("reasons", []),
                  "reread_policy": VERSION,
                  "original_outcome_sha256": plan_case(plan, cid)["original_outcome_sha256"],
                  "embedding_config_sha256": plan["embedding_config_sha256"],
                  "llm1_input": prepared["llm1_input"], "llm1_model_routing": routing,
                  "embedding_stage": stage}
        # The old review stays in the separate immutable original outcome.
        for key in ("error_type", "error_chain", "document_preparation_review"):
            result.pop(key, None)
        once(out, result)
    return result


def failure_outcome(plan, cid, error):
    case = Path(plan["folder"]) / str(cid)
    value = {"convocation_id": cid,
             "status": "needs_review" if isinstance(error, EmbeddingPreparationReview) else "failed",
             **selected.safe_error_facts(error),
             "no_automatic_retry": True,
             "reread_policy": VERSION,
             "original_outcome_sha256": plan_case(plan, cid)["original_outcome_sha256"]}
    if isinstance(error, Refused):
        value["reason"] = error.reason
    if isinstance(error, MissingVectors):
        value["reason"] = "missing_vectors"
    proof = document_preparation_review(error, case)
    if proof is not None:
        value["document_preparation_review"] = proof
    try:
        costs = audit_summary(case)
        value["llm1_cost_usd"] = costs["known_cost_usd"]
        value["usage_unknown_attempts"] = costs["usage_unknown_attempts"]
    except Exception:
        pass
    return value


def case_line(record):
    line = {"convocation_id": record["convocation_id"], "status": record["status"],
            "context_tokens": record.get("context_tokens"), "facet_count": record.get("facet_count"),
            "cost_usd": record.get("llm1_cost_usd"), "model": record.get("llm1_model")}
    if record["status"] != "llm1_complete":
        line["error_type"] = record.get("error_type")
        if record.get("reason"):
            line["reason"] = record["reason"]
    return line


def emit(value):
    with OUTPUT_LOCK:
        print(json.dumps(value, default=str), flush=True)


def run_case(settings, plan, cid, limits, guard):
    """One case never stops the others. Returns (record, already_recorded)."""
    out = Path(plan["folder"]) / str(cid) / "outcome.private.json"
    existed = out.exists()
    try:
        return read_case(settings, plan, cid, limits, guard), existed
    except (LimitReached, CorpusStopped) as error:
        # Stopped or over the LLM1 ceiling before the case was dispatched: no outcome, it stays pending.
        return {"convocation_id": cid, "status": "pending", "error_type": type(error).__name__}, False
    except Exception as error:
        value = failure_outcome(plan, cid, error)
        once(out, value)
        return value, False


def execute(plan, settings):
    folder = Path(plan["folder"])
    ids = plan["ids"]
    STOP.clear()
    costs = audit_summary(folder)
    if costs["usage_unknown_attempts"] or costs["embedding_attempts"]:
        raise ValueError("The folder has uncertain payments or embedding attempts; explicit diagnosis required")
    once(folder / "runtime-bindings.private.json", {
        "version": VERSION, "script_sha256": file_digest(Path(__file__).resolve()),
        "manifest_sha256": sha(folder / "manifest.private.json"),
        "embedding_config_sha256": plan["embedding_config_sha256"]})
    limits = RunLimits(max_accepted=len(ids), max_candidates=len(ids), max_jev_calls=0,
                       max_llm1_calls=plan["max_llm1_calls"], max_embedding_calls=NO_EMBEDDING_CALLS,
                       model_workers=MODEL_WORKERS, llm1_calls=costs["llm1_calls"])
    results = {}
    reused = []
    with psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=True) as owner:
        lock = owner.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked")
        locked = lock.fetchone()["locked"]
        if not locked:
            raise ValueError("Another coordinator owns PROD2")
        guard = OwnedReviewGuard(folder, owner=owner, stop=STOP, max_candidates=len(ids), max_new_documents=0,
                                 binding={"version": VERSION, "manifest_sha256": sha(folder / "manifest.private.json")})
        limits.handoff_guard = guard

        def work(cid):
            record, existed = run_case(settings, plan, cid, limits, guard)
            emit(case_line(record))
            return cid, record, existed

        previous = {}
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.getsignal(number)
                signal.signal(number, lambda *_: STOP.set())
        try:
            with ThreadPoolExecutor(max_workers=MODEL_WORKERS) as pool:
                for future in as_completed([pool.submit(work, cid) for cid in ids]):
                    cid, record, existed = future.result()
                    results[cid] = record
                    if existed:
                        reused.append(cid)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        guard_report = guard.report()
    records = [results[cid] for cid in ids]
    final_costs = audit_summary(folder)
    report = {"version": VERSION, "folder": str(folder), "ids": ids,
              "llm1_complete": sum(r["status"] == "llm1_complete" for r in records),
              "needs_review": sum(r["status"] == "needs_review" for r in records),
              "failed": sum(r["status"] == "failed" for r in records),
              "pending": [r["convocation_id"] for r in records if r["status"] == "pending"],
              "already_recorded": sorted(reused),
              "cost_usd": sum(r.get("llm1_cost_usd") or 0 for r in records),
              "costs": final_costs, "guard": guard_report,
              "embedding_calls": final_costs["embedding_calls"],
              "source_calls": 0, "redis_calls": 0, "jev_calls": 0,
              "at_utc": datetime.now(timezone.utc).isoformat()}
    private_write(folder / ("report-%d.private.json" % time.time_ns()), report)
    return report


def parse_ids(tokens):
    ids = []
    for token in tokens:
        ids += [int(part) for part in token.split(",") if part.strip()]
    if not ids or len(set(ids)) != len(ids) or any(cid <= 0 for cid in ids):
        raise ValueError("--ids needs distinct positive convocation ids")
    return ids


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="Offline: validate evidence and freeze the manifest")
    mode.add_argument("--execute", action="store_true", help="Read the planned cases (LLM1 only)")
    p.add_argument("--original", type=Path, required=True, help="Folder of the original run")
    p.add_argument("--ids", nargs="+", required=True, help="Explicit convocation ids (spaces or commas)")
    p.add_argument("--embedding-config", type=Path, required=True, help="New embedding reading configuration")
    p.add_argument("--folder", type=Path, required=True, help="New private folder for the manifest and outcomes")
    p.add_argument("--max-llm1-calls", type=int, required=True, help="Ceiling of LLM1 calls, at most one per id")
    p.add_argument("--llm1-model-routing", choices=MODEL_ROUTINGS, default="original",
                   help="original: the run's own page routing; fixed: the base model for every case")
    p.add_argument("--max-observation-hours", type=float, default=DEFAULT_MAX_OBSERVATION_HOURS,
                   help="Oldest accepted 'open' observation; default %d h as in the sector follow-up. "
                        "The deadline must still be in the future whatever this value." % DEFAULT_MAX_OBSERVATION_HOURS)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    args.ids = parse_ids(args.ids)
    args.folder = literal(args.folder)
    if args.plan:
        plan, rejected = frozen_plan(args.original, args.folder, args.ids, args.embedding_config,
                                     args.max_llm1_calls, args.max_observation_hours, getattr(args, "llm1_model_routing", "original"))
        now = datetime.now(timezone.utc)
        for case in plan["cases"]:
            # Advisory only (it depends on the clock, so it is not part of the manifest).
            observed = parse_aware(case["temporal_observed_at"], "temporal_observed_at")
            age = (now - observed).total_seconds()
            emit({"convocation_id": case["convocation_id"], "status": "planned",
                  "temporal_observed_at": case["temporal_observed_at"],
                  "observed_age_hours": round(age / 3600, 1),
                  "within_observation_limit": -CLOCK_SKEW_SECONDS <= age <= plan["max_observation_age_seconds"]})
        for item in rejected:
            emit({"convocation_id": item["convocation_id"], "status": "rejected",
                  "reason": item["reason"], "detail": item["detail"]})
        if rejected:
            emit({"status": "not_planned", "rejected": [item["convocation_id"] for item in rejected],
                  "manifest_written": False})
            return 2
        once(args.folder / "manifest.private.json", plan)
        emit({"status": "planned", "folder": str(args.folder), "ids": plan["ids"],
              "max_llm1_calls": plan["max_llm1_calls"], "max_embedding_calls": 0,
              "manifest_sha256": sha(args.folder / "manifest.private.json"), "provider_calls": 0})
        return 0
    plan = verify_plan(args)
    settings = Settings(_env_file=ROOT / ".env.local")
    report = execute(plan, settings)
    emit({key: report[key] for key in ("version", "folder", "llm1_complete", "needs_review", "failed",
                                       "pending", "already_recorded", "cost_usd", "embedding_calls",
                                       "source_calls", "redis_calls", "jev_calls")}
         | {"dispatch_stopped": report["guard"]["dispatch_stopped"]})
    return 0 if report["llm1_complete"] == len(plan["ids"]) else 1


if __name__ == "__main__":
    sys.exit(main())
