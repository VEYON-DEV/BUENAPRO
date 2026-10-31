"""HTTP-only PROD2 coordinator: shared keywords -> Redis -> Jev60 -> ficha -> selector -> LLM1.

Defaults to a ten-opportunity pilot. --all-keywords enables the configured union,
not a national census. --all-results removes count/call ceilings for the selected
keywords only, preserving frozen inputs when resuming. One coordinator owns the PG lock; its threads share limits
and atomic Redis reservations. No LLM2, matching, scheduler or notifications.
--llm1-min-affinity reads only accepted candidates at or above it; the rest keep their ficha
and end accepted_unread, with no document download, selector, embedding or LLM1 spend.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row
import redis

from run_prod2_accepted import commercial_snapshot, get_detail
from buenapro_worker.settings import Settings
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import accepted_repository as repo
from buenapro_worker.prod2.accepted_runner import accepted, paid_jev, paid_llm1
from buenapro_worker.prod2.analysis import (JevDescriptionAnalyzer, extract_document, extraction_request_hash,
    jev_request_hash, ficha_reading_description, LLM1_FICHA_CONTEXT_POLICY)
from buenapro_worker.prod2.archive_inventory import list_archive, extract_members, inventory_policy, POLICY_7Z
from buenapro_worker.prod2.batch_plan import MEMBER_IDS, configured_keywords, digest, publication_window
from buenapro_worker.prod2.business_batches import select_business_batches, select_keyword_preset, report_business_batches
from buenapro_worker.prod2.batch_repository import save_participation_assessment
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.client import Prod2EgressUnavailable, Prod2HttpClient, Prod2ProtocolError, SEARCH_URL
from buenapro_worker.prod2.document_selection import (JevDocumentSelector, document_selection_request_hash,
    DOCUMENT_SELECTION_POLICY, DOCUMENT_SELECTION_POLICY_V2, DOCUMENT_SELECTION_POLICY_V3)
from buenapro_worker.prod2.documents import METADATA_URL, download_document
from buenapro_worker.prod2.llm1_completion import persist_completed_llm1
from buenapro_worker.prod2.model_routing import default_model_routing, resolve_llm1_model
from buenapro_worker.prod2.participation import decide
from buenapro_worker.prod2.pdf_preparation import SELECTED_DOCUMENTS_POLICY, prepare_selected_document_set
from buenapro_worker.prod2.previews import upload_preview
from buenapro_worker.prod2.repository import persist_detail, record_document_download, hash_request
from buenapro_worker.prod2.runtime import production_staging_settings
from buenapro_worker.prod2.selected_control import RunLimits, SharedPacer, LimitReached
from buenapro_worker.prod2.selected_payments import AuditedJevClient, paid_document_selection, audit_summary, provider_permission
from buenapro_worker.prod2.staging import RedisStage
from buenapro_worker.prod2.storage_policy import OFFICIAL_LINKS_POLICY, official_links_only, verify_completed_storage
from buenapro_worker.prod2.completed_read_reuse import reuse_completed_read
from buenapro_worker.seace import shared_gate
from buenapro_worker.seace.egress import EGRESS_EXIT_CODE, EgressUnavailable, seace_http_client

STOP = threading.Event()
# The SEACE egress proxy failed (not a 403): stop without source_access_blocked, exit EGRESS_EXIT_CODE.
EGRESS_DOWN = threading.Event()
EMBEDDING_DISPATCH_STOP = threading.Event()
HANDOFF_GUARD = None
JEV_THRESHOLD = 60  # accepted_runner.accepted / accepted_repository.ACCEPTANCE_THRESHOLD, which stay fixed


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--cache-folder", type=Path, default=ROOT / "tmp/prod2-selected-cache",
                   help="Private verified PDF cache shared across runs; retain for Word deduplication")
    p.add_argument("--keyword", action="append")
    p.add_argument("--lane", action="append", help="Legacy configured priority lane; repeatable")
    p.add_argument("--business-batch", action="append", help="Versioned business batch ID; repeatable")
    p.add_argument("--business-scope", choices=["digitales", "industriales", "todas"],
                   help="Union of business batches; one query per distinct keyword")
    p.add_argument("--keyword-preset", help="Versioned exact keyword subset, e.g. digitales30")
    p.add_argument("--sweep-id", help="Frozen source-query epoch; reuse to resume, new ID to refresh")
    p.add_argument("--keyword-cache-folder", type=Path, default=ROOT / "tmp/prod2-keyword-cache",
                   help="Private native-query captures retained across batch/run folders")
    p.add_argument("--keyword-status", action="store_true", help="Read PostgreSQL keyword progress; no source/model calls")
    p.add_argument("--all-keywords", action="store_true")
    p.add_argument("--all-results", action="store_true",
                   help="Process every unique result of the selected keywords; override candidate/accepted/provider count caps without changing frozen paid inputs")
    p.add_argument("--preserve-prior-readings", action="store_true",
                   help="Preserve prior canonical reads and defer prior unresolved/failed paid reads across business batches")
    p.add_argument("--years", default="auto", help="auto = all exercise years observed in native selector")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--from-date")
    p.add_argument("--to-date")
    p.add_argument("--search-workers", type=int, default=1)
    p.add_argument("--processing-workers", type=int, default=2)
    p.add_argument("--model-workers", type=int, default=2)
    p.add_argument("--request-interval", type=float, default=2)
    p.add_argument("--skip-empty-scope-probe", action="store_true", help="Disable verified empty year/object pruning for keyword groups")
    p.add_argument("--max-accepted", type=int, default=10)
    p.add_argument("--max-candidates", type=int, default=100)
    p.add_argument("--max-jev-calls", type=int, default=120, help="Affinity and selector calls combined, including uncertain attempts")
    p.add_argument("--max-llm1-calls", type=int, default=10)
    p.add_argument("--llm1-min-affinity", type=float, default=JEV_THRESHOLD,
                   help="Accepted candidates below this Jev affinity are promoted with their ficha but not read "
                        "(accepted_unread); 60 reads every accepted one. A frozen run keeps its value")
    p.add_argument("--mapping-handoff", type=Path,
                   help="Explicit ID mode: complete metadata-only mapping handoff; never repeat its sweep")
    p.add_argument("--selection", type=Path, help="Frozen prod2_handoff_selection_v1 JSON")
    p.add_argument("--selection-sha256", help="Exact SHA256 of the approved selection file")
    p.add_argument("--runtime-recovery", type=Path,
                   help="ID mode only: externally reviewed runtime compatibility receipt; never rewrite the frozen manifest")
    p.add_argument("--runtime-recovery-sha256", help="Exact SHA256 of the runtime recovery receipt")
    p.add_argument("--max-new-documents", type=int,
                   help="ID mode only: cumulative unique official sources/archive members, separate from LLM1 calls")
    p.add_argument("--selector-high", type=float, default=80)
    p.add_argument("--selector-low", type=float, default=20)
    p.add_argument("--document-previews", action="store_true",
                   help="Latest document selector with bounded local text/OCR previews, including archive members")
    p.add_argument("--archive-7z", action="store_true",
                   help="New folders only: explicit bounded 7z inventory/extraction policy; preserves legacy ZIP/RAR runs")
    p.add_argument("--preview-anchor", action="append", default=[], metavar="SHA:PAGE[,PAGE]",
                   help="Explicit SHA-bound PDF page sample; repeat for up to ten sources, max three pages each")
    p.add_argument("--selector-policy", choices=["v2", "v3"], default="v3",
                   help="Versioned selector for new runs with --document-previews")
    p.add_argument("--llm1-ficha-context", action="store_true",
                   help="New runs: compare selected document scope with sanitized official ficha description")
    p.add_argument("--compact-unique-requirements", action="store_true",
                   help="New reading: concise unique requirements, full PDF and unchanged schema")
    p.add_argument("--llm1-model-routing", choices=["auto", "pages20", "fixed"], default="auto",
                   help="New runs: >20 prepared pages use 2.5 Flash-Lite, <=20 use 3.1; auto preserves frozen legacy runs")
    p.add_argument("--llm1-scope", choices=["auto", "qualification", "legacy"], default="auto",
                   help="New runs: qualification requirements, excluding payment/proposal lists; auto preserves existing runs")
    p.add_argument("--llm1-commercial-taxonomy", choices=["auto", "none", "commercial_taxonomy_v1"], default="auto",
                   help="Explicit new folders only: documentary commercial labels; auto preserves frozen inputs and leaves new folders disabled")
    p.add_argument("--llm1-input", choices=["auto", "native", "pdf", "embeddings"], default="auto",
                   help="New runs: complete native text when usable, otherwise PDF; auto preserves existing runs")
    p.add_argument("--embedding-config", type=Path,
                   help="New embeddings runs default to partial local OCR runtime.v5; existing folders retain their frozen configuration")
    p.add_argument("--embedding-resume-from", type=Path,
                   help="Fork a stopped, source-complete queue without repeating handled candidates or source queries")
    p.add_argument("--max-embedding-calls", type=int, default=200)
    p.add_argument("--llm1-fallback", choices=["auto", "luna", "none"], default="auto",
                   help="New folders: GPT-6 Luna low once after known Gemini MAX_TOKENS; auto preserves historical folders")
    p.add_argument("--document-storage", choices=["auto", "official", "r2"], default="auto",
                   help="New folders: official SEACE links and temporary files; auto preserves frozen historical R2 runs")
    p.add_argument("--max-source-mib", type=int, default=128)
    p.add_argument("--max-expanded-mib", type=int, default=256)
    p.add_argument("--max-archive-members", type=int, default=2000)
    p.add_argument("--local", action="store_true", help="Use local .env.local DATABASE_URL/REDIS_URL; no SSH")
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    p.add_argument("--dry-run", action="store_true", help="Offline: no DB, Redis, HTTP or provider calls")
    p.add_argument("--preflight", action="store_true", help="Native HTTP access/year test only; no DB or providers")
    p.add_argument("--prepare-selector-cache", action="store_true", help="Apply ONLY migration0039; no source or provider calls")
    p.add_argument("--prepare-keyword-tracking", action="store_true", help="Apply ONLY migration0040; no source or provider calls")
    p.add_argument("--prepare-document-events", action="store_true", help="Apply ONLY migration0041; no source or provider calls")
    p.add_argument("--prepare-7z-downloads", action="store_true", help="Apply ONLY migration0042 under the shared coordinator; no source/providers")
    p.add_argument("--prepare-embeddings", action="store_true", help="Apply ONLY migration0043 under the shared coordinator; no source/providers")
    return p


def execution_policy(args):
    """Operational limits are independent from frozen source/model inputs.

    An explicit all-results resume keeps the original manifest and payment
    hashes. File-size, concurrency, pacing and unknown-payment guards remain.
    """
    all_results = bool(getattr(args, "all_results", False))
    value = {"policy_version": "prod2_execution_limits_v2", "all_results": all_results,
        "effective_limits": {name: None if all_results else getattr(args, name)
            for name in ("max_accepted", "max_candidates", "max_jev_calls", "max_llm1_calls")}}
    if getattr(args, "mapping_handoff", None):
        value.update(max_new_documents=args.max_new_documents,
            candidate_limit_unit="cumulative_processed_ID_admissions", limit_expansion_requires_readback=True)
    return value


def runtime_recovery_report_context(args, reader_folder):
    """Audit approved running code separately from immutable paid/source inputs."""
    if getattr(args, "runtime_recovery", None) is None:
        return None
    from buenapro_worker.prod2.handoff_runtime_recovery import report_context
    return report_context(args.runtime_recovery, args.runtime_recovery_sha256,
        root=ROOT, reader_folder=reader_folder)


def completion_exit_code(report, policy):
    if "handoff_guards" in report:
        return 2 if report["handoff_guards"]["dispatch_stopped"] or report["costs"]["usage_unknown_attempts"] else 0
    if report.get("embedding_dispatch_stop_requested") is True:
        return 2
    # accepted_unread is a settled outcome: listed with its ficha, deliberately not read.
    settled = report["llm1_complete"] + report.get("accepted_unread", 0)
    if not policy["all_results"]:
        return 0 if (settled == policy["effective_limits"]["max_accepted"]
                     and report["costs"]["usage_unknown_attempts"] == 0) else 2
    # Finishing the queue is different from completing all source queries or
    # resolving an uncertain/failed paid read. Keep those gaps visible.
    complete = (report["source_coverage_complete"] and report["redis"]["queued"] == 0
        and report["redis"]["pending"] == 0 and settled == report["accepted"]
        and report["costs"]["usage_unknown_attempts"] == 0
        and all(row.get("status") in {"llm1_complete", "accepted_unread", "rejected_below_60"}
                for row in report["records"]))
    return 0 if complete else 2


def bounded_error_chain(error):
    visited = set()
    while error is not None and id(error) not in visited and len(visited) < 5:
        visited.add(id(error))
        yield error
        error = error.__cause__ if error.__cause__ is not None else error.__context__


def egress_failure(error):
    return any(isinstance(cause, EgressUnavailable) for cause in bounded_error_chain(error))


def safe_error_facts(error):
    """Record validated machine facts without messages, URLs or credentials."""
    from buenapro_worker.prod2.embedding_client import EmbeddingRequestError, safe_request_error_facts
    chain = []
    for error in bounded_error_chain(error):
        item = {"error_type": type(error).__name__}
        state = getattr(error, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
            item["sqlstate"] = state
        if isinstance(error, EmbeddingRequestError):
            item.update(safe_request_error_facts(error))
        chain.append(item)
    return {"error_type": chain[0]["error_type"], "error_chain": chain}


def stop_handoff_after_local_error(guard, error, *, phase, convocation_id=None):
    """Keep unexpected failures pending, with no claim about provider usage."""
    STOP.set()
    guard.stop.set()
    with guard.lock:
        if not guard.marker.exists():
            receipt = {"version": "prod2_handoff_guards_v1", "binding_hash": guard.binding_hash,
                "reason": "unexpected_local_exception", "phase": phase,
                "observed_at": datetime.now(timezone.utc).isoformat(), **safe_error_facts(error)}
            if convocation_id is not None:
                receipt["convocation_id"] = convocation_id
            private_write(guard.marker, receipt)


def wait_consumers(workers, handoff_guard=None):
    # Legacy keeps its existing result order. ID imports observe a failed
    # consumer immediately, so another consumer cannot silently drain the run.
    for worker in as_completed(workers) if handoff_guard is not None else workers:
        try:
            worker.result()
        except Exception as error:
            if handoff_guard is not None:
                stop_handoff_after_local_error(handoff_guard, error, phase="consumer_worker")
            raise


def stop_after_uncertain_embedding_error(error):
    """Stop new dispatch after a sent failure; preserve receipts and in-flight work.

    On the normal path EmbeddingStore saves the uncertain terminal first. If
    persistence fails, a claim/started receipt may be all that remains. Either
    way, stop conservatively when a sent failure is in the chain. Status/code
    cannot prove zero cost or recovery, resolve charges, or authorize a retry.
    """
    from buenapro_worker.prod2.embedding_client import EmbeddingRequestError, safe_request_error_facts
    for cause in bounded_error_chain(error):
        if isinstance(cause, EmbeddingRequestError) and cause.request_sent is True:
            EMBEDDING_DISPATCH_STOP.set()
            STOP.set()
            return {"policy_version": "prod2_stop_after_sent_embedding_failure_v1",
                    "request_sent": True, **safe_request_error_facts(cause)}
    return None


def requested_model_routing(args):
    """New folders use page routing; existing manifests keep exact paid inputs."""
    if args.llm1_model_routing == "fixed":
        return {}
    if args.llm1_model_routing == "pages20":
        return {"llm1_model_routing": default_model_routing()}
    marker = args.folder / "manifest.private.json"
    if args.folder.is_symlink() or marker.is_symlink():
        raise ValueError("Frozen model-routing paths must not be symlinks")
    if not marker.exists():
        return {"llm1_model_routing": default_model_routing()}
    frozen = read_private(marker)
    requested = frozen.get("requested")
    if not isinstance(requested, dict):
        raise ValueError("Frozen coordinator manifest has no requested inputs")
    if (("llm1_model_routing" in requested) != ("llm1_model_routing" in frozen)
            or requested.get("llm1_model_routing") != frozen.get("llm1_model_routing")):
        raise ValueError("Frozen model-routing inputs differ from the manifest")
    if "llm1_model_routing" not in requested:
        return {}
    resolve_llm1_model(requested, 1, "gemini-3.1-flash-lite")
    return {"llm1_model_routing": requested["llm1_model_routing"]}


def requested_luna_fallback(args):
    from buenapro_worker.prod2.luna_fallback import (LUNA_FALLBACK_KEY,
        default_luna_fallback_config, validate_luna_fallback_config)
    marker = args.folder / "manifest.private.json"
    if args.folder.is_symlink() or marker.is_symlink():
        raise ValueError("Frozen fallback paths must not be symlinks")
    frozen = read_private(marker) if marker.exists() else None
    previous = None
    if frozen is not None:
        requested = frozen.get("requested")
        if (not isinstance(requested, dict) or (LUNA_FALLBACK_KEY in requested) != (LUNA_FALLBACK_KEY in frozen)
                or requested.get(LUNA_FALLBACK_KEY) != frozen.get(LUNA_FALLBACK_KEY)):
            raise ValueError("Frozen fallback inputs differ from the manifest")
        if LUNA_FALLBACK_KEY in requested:
            previous = validate_luna_fallback_config(requested[LUNA_FALLBACK_KEY])
    flag = getattr(args, "llm1_fallback", "auto")
    chosen = (previous if frozen is not None else default_luna_fallback_config()) if flag == "auto" else (
        default_luna_fallback_config() if flag == "luna" else None)
    if frozen is not None and chosen != previous:
        raise ValueError("Use a new folder for a changed LLM1 fallback policy")
    return {LUNA_FALLBACK_KEY: chosen} if chosen is not None else {}


def requested_reading_policies(args):
    """Never silently change a folder that already owns model reservations."""
    from buenapro_worker.prod2.extraction_policy import QUALIFICATION_FOCUS_POLICY, SCOPE_POLICY_KEY, get_scope_policy
    from buenapro_worker.prod2.input_policy import NATIVE_TEXT_OR_PDF_POLICY, EMBEDDING_RETRIEVED_TEXT_POLICY, INPUT_POLICY_KEY, get_input_policy
    marker = args.folder / "manifest.private.json"
    if args.folder.is_symlink() or marker.is_symlink():
        raise ValueError("Frozen reading-policy paths must not be symlinks")
    frozen = read_private(marker) if marker.exists() else None
    compact = getattr(args, "compact_unique_requirements", False)
    policies = {}
    for flag, key, policy, enabled, disabled, validator in (
        (getattr(args, "llm1_scope", "auto"), SCOPE_POLICY_KEY, QUALIFICATION_FOCUS_POLICY,
         "qualification", "legacy", get_scope_policy),
        (getattr(args, "llm1_input", "auto"), INPUT_POLICY_KEY, NATIVE_TEXT_OR_PDF_POLICY,
         "native", "pdf", get_input_policy),
    ):
        previous = None
        if frozen is not None:
            requested = frozen.get("requested")
            if (not isinstance(requested, dict) or (key in requested) != (key in frozen)
                    or requested.get(key) != frozen.get(key)):
                raise ValueError("Frozen reading-policy inputs differ from the manifest")
            previous = validator(requested)
        chosen = (previous if frozen is not None else None if compact else policy) if flag == "auto" else (
            EMBEDDING_RETRIEVED_TEXT_POLICY if key == INPUT_POLICY_KEY and flag == "embeddings" else
            policy if flag == enabled else None)
        if frozen is not None and chosen != previous:
            raise ValueError("Use a new folder for a changed LLM1 reading policy")
        if chosen is not None:
            policies[key] = chosen
    if compact and policies:
        raise ValueError("Legacy compact reading cannot be combined with new scope/input policies")
    return policies


def requested_commercial_taxonomy(args):
    """Explicit opt-in only; never upgrade the semantics of a frozen folder."""
    from buenapro_worker.extraction.commercial_classification import (
        POLICY_KEY, TAXONOMY_VERSION, get_commercial_policy,
    )
    marker = args.folder / "manifest.private.json"
    if args.folder.is_symlink() or marker.is_symlink():
        raise ValueError("Frozen commercial-taxonomy paths must not be symlinks")
    frozen = read_private(marker) if marker.exists() else None
    previous = None
    if frozen is not None:
        requested = frozen.get("requested")
        if (not isinstance(requested, dict) or (POLICY_KEY in requested) != (POLICY_KEY in frozen)
                or requested.get(POLICY_KEY) != frozen.get(POLICY_KEY)):
            raise ValueError("Frozen commercial-taxonomy inputs differ from the manifest")
        previous = get_commercial_policy(requested)
    flag = getattr(args, "llm1_commercial_taxonomy", "auto")
    if flag not in {"auto", "none", TAXONOMY_VERSION}:
        raise ValueError("Unsupported LLM1 commercial taxonomy policy")
    chosen = previous if flag == "auto" else None if flag == "none" else TAXONOMY_VERSION
    if frozen is not None and chosen != previous:
        raise ValueError("Use a new folder for a changed LLM1 commercial taxonomy")
    if chosen and getattr(args, "compact_unique_requirements", False):
        raise ValueError("Legacy compact reading cannot use commercial taxonomy")
    return {POLICY_KEY: chosen} if chosen else {}


def requested_embedding_reading(args):
    from buenapro_worker.prod2.embedding_runtime import INPUT_POLICY, load_config, config_hash, validate_frozen_config
    policies = requested_reading_policies(args)
    marker = args.folder / "manifest.private.json"
    frozen = read_private(marker) if marker.exists() else None
    if policies.get("llm1_input_policy") != INPUT_POLICY:
        if getattr(args, "embedding_resume_from", None):
            raise ValueError("Queue migration requires explicit embeddings reading")
        return {}
    if policies.get("llm1_scope_policy") != "prod2_qualification_focus_v1":
        raise ValueError("Embeddings reading requires qualification scope")
    config = (validate_frozen_config(frozen) if frozen is not None and args.embedding_config is None else
              load_config(args.embedding_config or ROOT / "docs/sources/prod2-embedding-reading.runtime.v5.json"))
    binding = {"config": config, "config_sha256": config_hash(config)}
    if frozen is not None and validate_frozen_config(frozen) != config:
        raise ValueError("Use a new folder for changed embedding configuration")
    return {"embedding_reading": binding,
        **({"llm1_source_coverage_policy": config["reading"]["source_coverage_policy"]}
           if config.get("reading", {}).get("source_coverage_policy") else {}),
        **({"llm1_retrieved_transport_policy": config["reading"]["transport_policy"]}
           if config.get("reading", {}).get("transport_policy") else {}),
        **({"llm1_visual_review_policy": "prod2_ocr_text_layout_review_v2"}
           if config["corpus"]["ocr_textual_scan_policy"] in {
               "prod2_confident_text_layout_review_v2", "prod2_confident_text_layout_review_v3"} else {}),
        **({"embedding_resume_from": str(args.embedding_resume_from.absolute())}
           if args.embedding_resume_from else {})}


def requested_storage_policy(args):
    marker = args.folder / "manifest.private.json"
    if args.folder.is_symlink() or marker.is_symlink():
        raise ValueError("Frozen storage paths must not be symlinks")
    frozen = read_private(marker) if marker.exists() else None
    previous = None
    if frozen is not None:
        requested = frozen.get("requested")
        key = "document_storage_policy"
        if (not isinstance(requested, dict) or (key in requested) != (key in frozen)
                or requested.get(key) != frozen.get(key)):
            raise ValueError("Frozen storage inputs differ from the manifest")
        official_links_only(requested)
        previous = requested.get(key)
    chosen = (previous if frozen is not None else OFFICIAL_LINKS_POLICY) if args.document_storage == "auto" else (
        OFFICIAL_LINKS_POLICY if args.document_storage == "official" else None)
    if frozen is not None and chosen != previous:
        raise ValueError("Use a new folder for a changed document storage policy")
    return {"document_storage_policy": chosen} if chosen is not None else {}


def requested_archive_policy(args):
    """7z needs a new explicitly opted-in folder; resumes retain the frozen policy."""
    marker = args.folder / "manifest.private.json"
    if args.folder.is_symlink() or marker.is_symlink():
        raise ValueError("Frozen archive-policy paths must not be symlinks")
    frozen = read_private(marker) if marker.exists() else None
    previous = None
    key = "archive_inventory_policy"
    if frozen is not None:
        requested = frozen.get("requested")
        if (not isinstance(requested, dict) or (key in requested) != (key in frozen)
                or requested.get(key) != frozen.get(key)):
            raise ValueError("Frozen archive-policy inputs differ from the manifest")
        inventory_policy(requested)
        previous = requested.get(key)
    chosen = POLICY_7Z if getattr(args, "archive_7z", False) else previous
    if frozen is not None and chosen != previous:
        raise ValueError("Use a new folder for a changed archive inventory policy")
    return {key: chosen} if chosen is not None else {}


def llm1_min_affinity(inputs):
    """Reading threshold of a manifest or requested inputs; absent (older runs) means every accepted one is read."""
    value = inputs.get("llm1_min_affinity", JEV_THRESHOLD)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not JEV_THRESHOLD <= value <= 100:
        raise ValueError("Invalid frozen LLM1 minimum affinity")
    return value


def requested_llm1_min_affinity(args):
    """Only a non-default value is requested, so earlier manifests, hashes and queue identities stay valid.

    A frozen folder compares ``requested`` as a whole: resuming it with another value, or with none, is refused.
    """
    value = getattr(args, "llm1_min_affinity", JEV_THRESHOLD)
    if not JEV_THRESHOLD <= value <= 100:
        raise ValueError("LLM1 minimum affinity must be between the Jev threshold (60) and 100")
    return {"llm1_min_affinity": int(value) if value == int(value) else value} if value != JEV_THRESHOLD else {}


def plan(args):
    from buenapro_worker.prod2.document_previews import parse_preview_anchor_specs, ANCHORED_PREVIEW_POLICY
    recovery_path, recovery_sha = getattr(args, "runtime_recovery", None), getattr(args, "runtime_recovery_sha256", None)
    if (recovery_path is None) != (recovery_sha is None):
        raise ValueError("Runtime recovery requires both receipt path and SHA256")
    if recovery_path is not None and (not getattr(args, "mapping_handoff", None)
            or not isinstance(recovery_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recovery_sha)):
        raise ValueError("Runtime recovery requires explicit ID import and a valid receipt SHA256")
    anchors = parse_preview_anchor_specs(getattr(args, "preview_anchor", []))
    if anchors and (not args.document_previews or args.selector_policy != "v3"):
        raise ValueError("Explicit preview anchors require document previews and selector V3")
    if not (1 <= args.search_workers <= 4 and 1 <= args.processing_workers <= 8 and 1 <= args.model_workers <= 4):
        raise ValueError("Worker bounds: search1..4 / processing1..8 / models1..4")
    if not (1 <= args.max_accepted <= args.max_candidates <= 100000):
        raise ValueError("Accepted/candidate bounds must be ordered within 100000")
    if not (1 <= args.max_jev_calls <= 200000 and 1 <= args.max_llm1_calls <= 100000):
        raise ValueError("Provider-call ceilings must be positive and bounded")
    if not 1 <= args.max_embedding_calls <= 200000:
        raise ValueError("Embedding-call ceiling must be positive and bounded")
    if not (1 <= args.request_interval <= 60 and 0 <= args.selector_low < args.selector_high <= 100):
        raise ValueError("Invalid aggregate request pacing or selector thresholds")
    if not (16 <= args.max_source_mib <= 128 and args.max_source_mib <= args.max_expanded_mib <= 1024
            and 1 <= args.max_archive_members <= 5000):
        raise ValueError("Invalid bounded source/archive limits")
    if sum((bool(args.keyword), bool(args.lane), args.all_keywords,
            bool(args.business_batch), bool(args.business_scope), bool(args.keyword_preset))) > 1:
        raise ValueError("Choose keywords, lanes, business batches, business scope or all keywords")
    if (args.keyword_preset or args.keyword_status) and not args.sweep_id:
        raise ValueError("Keyword presets/status require an explicit --sweep-id")
    if sum((args.keyword_status, args.preflight, args.prepare_selector_cache, args.prepare_keyword_tracking,
            args.prepare_document_events, getattr(args, "prepare_7z_downloads", False),
            getattr(args, "prepare_embeddings", False))) > 1:
        raise ValueError("Choose one status/preflight/schema preparation mode")
    if args.sweep_id:
        from buenapro_worker.prod2.keyword_tracking import identifier
        identifier(args.sweep_id)
    imported = None
    if any(getattr(args, key, None) is not None for key in ("mapping_handoff", "selection", "selection_sha256")):
        if not all(getattr(args, key, None) for key in ("mapping_handoff", "selection", "selection_sha256")):
            raise ValueError("ID import requires handoff, selection and exact selection SHA256")
        if (args.all_results or args.embedding_resume_from or args.sweep_id or args.keyword or args.lane
                or args.all_keywords or args.business_batch or args.business_scope or args.keyword_preset
                or args.preflight or args.keyword_status or args.prepare_selector_cache
                or args.prepare_keyword_tracking or args.prepare_document_events
                or args.prepare_7z_downloads or args.prepare_embeddings):
            raise ValueError("ID import cannot sweep, preflight source, adopt an old queue or remove finite caps")
        if not args.preserve_prior_readings or not 1 <= (args.max_new_documents or 0) <= 100000:
            raise ValueError("ID import requires historical preservation and a finite document ceiling")
        from buenapro_worker.prod2.handoff_runtime_recovery import load_selection
        imported = load_selection(args.mapping_handoff, args.selection, args.selection_sha256, root=ROOT,
            recovery_path=recovery_path, recovery_sha256=recovery_sha, reader_folder=args.folder)
        if args.folder.resolve() == args.mapping_handoff.resolve().parent:
            raise ValueError("ID import requires a new reading folder, not the mapping folder")
        if args.folder.exists() and not (args.folder / "manifest.private.json").exists() and any(args.folder.iterdir()):
            raise ValueError("ID import requires an empty new folder or its own frozen manifest")
    elif args.max_new_documents is not None:
        raise ValueError("Document admission policy requires explicit ID import mode")
    inventory = read_private(ROOT / "docs/sources/prod2-convenio-keywords.v3.json")
    configured = {kw.casefold(): kw for kw in configured_keywords(inventory)}
    lanes = {row["name"]: row["keywords"] for row in inventory["lanes"]}
    business = {}
    if imported is not None:
        keywords = imported["keywords"]
    elif args.keyword_preset:
        business = select_keyword_preset(read_private(ROOT / "docs/sources/prod2-keyword-presets.v1.json"),
            read_private(ROOT / "docs/sources/prod2-business-batches.v1.json"), inventory, args.keyword_preset)
        keywords = business["keywords"]
    elif args.business_batch or args.business_scope:
        business = select_business_batches(read_private(ROOT / "docs/sources/prod2-business-batches.v1.json"),
            inventory, batch_ids=args.business_batch, scope=args.business_scope)
        keywords = business["keywords"]
    elif args.all_keywords:
        keywords = list(configured.values())
    elif args.lane:
        if any(name not in lanes for name in args.lane):
            raise ValueError("Unknown keyword lane")
        keywords = list(dict.fromkeys(kw for name in args.lane for kw in lanes[name]))
    else:
        requested = args.keyword or ["SOFTWARE"]
        if any(kw.strip().casefold() not in configured for kw in requested):
            raise ValueError("Keyword is absent from current joint inventory")
        keywords = list(dict.fromkeys(configured[kw.strip().casefold()] for kw in requested))
    years = "auto"
    if args.years != "auto":
        if not re.fullmatch(r"[0-9]{4}(,[0-9]{4})*", args.years):
            raise ValueError("Use auto or explicit comma-separated exercise years")
        years = [int(year) for year in args.years.split(",")]
        if len(set(years)) != len(years) or any(not 2000 <= year <= 2100 for year in years):
            raise ValueError("Invalid exercise years")
    if imported is not None:
        if ((args.from_date and args.from_date != imported["published_from"])
                or (args.to_date and args.to_date != imported["published_to"])
                or (years != "auto" and years != imported["years"])):
            raise ValueError("ID import cannot change mapped years or publication window")
        years = imported["years"]
    publication_window(datetime.now(timezone.utc), months=args.months,
        published_from=imported["published_from"] if imported else args.from_date,
        published_to=imported["published_to"] if imported else args.to_date)
    result = {"pipeline": "prod2_selected_llm1_v1", "document_scope": SELECTED_DOCUMENTS_POLICY,
        "terminal_stage": "llm1", "inventory_hash": digest(inventory), "configured_keywords": len(configured),
        "keywords": keywords, "lanes": args.lane or [], "years": years, "objects": ["good", "service"],
        "months": args.months, "from_date": imported["published_from"] if imported else args.from_date,
        "to_date": imported["published_to"] if imported else args.to_date,
        "cache_folder": str(args.cache_folder.resolve()),
        "probe_empty_scopes": len(keywords) > 1 and not args.skip_empty_scope_probe,
        "jev_threshold": 60, "selector_high": args.selector_high, "selector_low": args.selector_low,
        **requested_llm1_min_affinity(args),
        **({"document_previews": True, "selector_policy_version":
            DOCUMENT_SELECTION_POLICY_V3 if args.selector_policy == "v3" else DOCUMENT_SELECTION_POLICY_V2}
           if args.document_previews else {}),
        **({"document_preview_anchors": {"policy_version": ANCHORED_PREVIEW_POLICY,
            "pages_by_source_sha256": anchors}} if anchors else {}),
        **({"llm1_ficha_context_policy": LLM1_FICHA_CONTEXT_POLICY} if args.llm1_ficha_context else {}),
        **({"llm1_reading_policy": "compact_unique_requirements_v1"}
           if getattr(args, "compact_unique_requirements", False) else {}),
        **requested_model_routing(args),
        **requested_reading_policies(args),
        **requested_commercial_taxonomy(args),
        **requested_embedding_reading(args),
        **requested_luna_fallback(args),
        **requested_storage_policy(args),
        **requested_archive_policy(args),
        **({"prior_reading_policy": "prod2_preserve_prior_readings_v1"}
           if args.preserve_prior_readings else {}),
        **{name: getattr(args, name) for name in ("search_workers", "processing_workers", "model_workers",
            "request_interval", "max_accepted", "max_candidates", "max_jev_calls", "max_llm1_calls",
            "max_source_mib", "max_expanded_mib", "max_archive_members")},
        **({"business_plan": business["business_plan"]} if business else {}),
        **({"keyword_preset": business["keyword_preset"]} if args.keyword_preset else {}),
        **({"keyword_tracking": {"sweep_id": args.sweep_id, "cache_folder": str(args.keyword_cache_folder.resolve()),
            "policy": "prod2_keyword_tracking_v1"}} if args.sweep_id else {})}
    if imported is not None:
        if not official_links_only(result) or result.get("llm1_luna_fallback"):
            raise ValueError("ID import requires official temporary storage and --llm1-fallback none")
        if "llm1_min_affinity" in result:
            raise ValueError("ID import reads every accepted candidate; keep the default LLM1 minimum affinity")
        result.update(handoff_selection=imported["binding"],
            handoff_guard_policy="prod2_handoff_guards_v1", probe_empty_scopes=False)
        # Only this opt-in mode separates cumulative operational ceilings from
        # paid/source identities. Expansion retains exactly the same queued IDs.
        for key in ("max_accepted", "max_candidates", "max_jev_calls", "max_llm1_calls"):
            del result[key]
    return result


@contextmanager
def runtime(args):
    if args.local:
        values = dotenv_values(ROOT / ".env.local")
        redis_url = os.environ.get("REDIS_URL") or values.get("REDIS_URL")
        if not redis_url:
            raise ValueError("Local REDIS_URL is required")
        settings = Settings(_env_file=ROOT / ".env.local")
        yield settings, redis_url
    else:
        with production_staging_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            yield settings


@contextmanager
def source_factory(pacer):
    with seace_http_client(timeout=60, verify=True, event_hooks=source_hooks(pacer)) as http:
        yield Prod2HttpClient(http)


def shared_turn():
    """Take the SEACE turn shared with PROD6 and the web; no-op unless the gate is on.

    A pause opened by another process is handled as the 403 it stands for, and a Redis
    failure as a failed egress; in both cases the request is never sent.
    """
    try:
        shared_gate.acquire()
    except shared_gate.SharedSourceBlocked as blocked:
        STOP.set()
        if HANDOFF_GUARD is not None:
            HANDOFF_GUARD.stop_access(blocked.status)
        # Same error the client raises for a received 403/429, so sweeps classify it as source_<status>.
        raise Prod2ProtocolError(f"PROD2 HTTP status {blocked.status}") from None
    except shared_gate.SharedGateUnavailable:
        raise Prod2EgressUnavailable("PROD2 SEACE egress unavailable") from None


def source_hooks(pacer):
    def request(value):
        if STOP.is_set():
            raise LimitReached("Source stop requested")
        pacer(value)
        shared_turn()
        if STOP.is_set():
            raise LimitReached("Source stop requested")
    def response(value):
        if value.status_code in {403, 429}:
            STOP.set()
            if HANDOFF_GUARD is not None:
                HANDOFF_GUARD.stop_access(value.status_code)
            shared_gate.block(value.status_code, "prod2")
    return {"request": [request], "response": [response]}


def guarded_issue(issue):
    def before_request():
        if STOP.is_set():
            raise LimitReached("Stop requested before provider call")
        issue()
    return before_request


def staging_identity(manifest):
    if not manifest.get("keyword_tracking"):
        return manifest["run_id"]
    # The campaign queue survives new bounded processing runs. New semantic
    # inputs receive their own queue, while source-query evidence stays reusable.
    return "kw-" + digest({"sweep_id": manifest["keyword_tracking"]["sweep_id"],
        "commercial_profiles": manifest["commercial_profiles"], "gemini_model": manifest["gemini_model"],
        "pipeline": manifest["pipeline"], "document_scope": manifest["document_scope"],
        "jev_threshold": manifest["jev_threshold"], "selector_high": manifest["selector_high"],
        "selector_low": manifest["selector_low"],
        **({"llm1_commercial_taxonomy": manifest["llm1_commercial_taxonomy"]}
           if "llm1_commercial_taxonomy" in manifest else {}),
        **({"llm1_luna_fallback": manifest["llm1_luna_fallback"]} if "llm1_luna_fallback" in manifest else {}),
        **({"prior_reading_policy": manifest["prior_reading_policy"]} if "prior_reading_policy" in manifest else {}),
        **({"llm1_min_affinity": manifest["llm1_min_affinity"]} if "llm1_min_affinity" in manifest else {}),
        **({"llm1_input_policy": manifest["llm1_input_policy"],
            "embedding_config_sha256": manifest["embedding_reading"]["config_sha256"]}
           if manifest.get("embedding_reading") else {})})


def embedding_resume_binding(requested, folder):
    """Freeze source-complete evidence; a changed reading never replays old work."""
    source = requested.get("embedding_resume_from")
    if source is None:
        return None
    from buenapro_worker.prod2.embedding_resume import load_resume_binding
    if Path(source).absolute() == Path(folder).absolute():
        raise ValueError("Embedding resume requires a new run folder")
    binding = load_resume_binding(Path(source))
    previous = binding["source_manifest"]
    # Execution caps and reading policy may change. Source/commercial inputs may not.
    for key in ("pipeline", "document_scope", "keywords", "objects", "months", "from_date", "to_date",
                "keyword_tracking", "business_plan", "keyword_preset", "jev_threshold", "llm1_min_affinity",
                "selector_high", "selector_low", "document_previews", "selector_policy_version", "llm1_model_routing",
                "llm1_ficha_context_policy", "llm1_luna_fallback", "document_storage_policy", "archive_inventory_policy"):
        if requested.get(key) != previous.get(key):
            raise ValueError("Embedding resume changed a frozen source/commercial input: " + key)
    return binding


def staged_this_run(folder, identity, total_seen, *, tracked):
    if not tracked:
        return total_seen
    marker = folder / "staging-origin.private.json"
    if marker.exists():
        origin = read_private(marker)
        if origin.get("staging_identity") != identity or type(origin.get("initial_seen")) is not int:
            raise ValueError("Frozen campaign queue changed")
    else:
        origin = {"staging_identity": identity, "initial_seen": total_seen}
        private_write(marker, origin)
    if not 0 <= origin["initial_seen"] <= total_seen:
        raise ValueError("Campaign Redis history missing; restore it before resuming")
    return total_seen - origin["initial_seen"]


def queue_checkpoint(folder, identity, total_seen):
    """Persist a shared high-water mark, including the first run's zero origin."""
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = folder / (identity + ".private.json")
    if marker.exists():
        value = read_private(marker)
        if (value.get("staging_identity") != identity or type(value.get("highest_seen")) is not int
                or not 0 <= value["highest_seen"] <= total_seen):
            raise ValueError("Campaign Redis history regressed; restore its backup")
    highest = total_seen
    def update(seen):
        nonlocal highest
        try:
            if type(seen) is not int or seen < highest:
                raise ValueError("Campaign Redis history regressed")
            write_queue_checkpoint(marker, {"staging_identity": identity, "highest_seen": seen})
            highest = seen
        except Exception:
            STOP.set()
            raise
    update(total_seen)
    return update


def write_queue_checkpoint(marker, value):
    # This is mutable coordination state, separate from immutable model receipts.
    if marker.is_symlink() or marker.parent.is_symlink():
        raise ValueError("Unsafe queue checkpoint")
    temporary = marker.parent / (".queue-writing-" + os.urandom(12).hex())
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker)
        directory = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_years(requested, pacer, *, cache_folder=None, scope=None):
    marker = Path(cache_folder) / "native-years.private.json" if cache_folder else None
    if marker and marker.exists():
        observation = read_private(marker)
        if observation.get("scope") != scope:
            raise ValueError("Frozen native selector scope changed")
        observed_years = observation.get("available_years")
        if (not isinstance(observed_years, list) or not observed_years
                or any(type(year) is not int or not 2000 <= year <= 2100 for year in observed_years)
                or len(set(observed_years)) != len(observed_years)):
            raise ValueError("Frozen native selector is invalid")
    else:
        with source_factory(pacer) as source:
            source.bootstrap()
            observed_years = source.available_convocation_years()
        if marker:
            marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_write(marker, {"scope": scope, "available_years": observed_years,
                "source_observed_at": datetime.now(timezone.utc).isoformat()})
    years = observed_years if requested["years"] == "auto" else requested["years"]
    if not years or set(years) - set(observed_years):
        raise ValueError("Requested exercise year absent from native selector")
    return years, observed_years


def official_inventory(detail):
    return [{"document_uuid": doc["uuid"], "filename": doc["filename"], "type": doc["type"],
             "stage": doc["stage"], "published_at": doc["published_at"],
             "source_url": METADATA_URL + "?id=" + doc["uuid"]}
            for doc in sorted(detail["documents"], key=lambda row: row["uuid"])]


def snapshot(path, value):
    """Replay an identical frozen stage; conflicting inputs require a new run."""
    if path.exists():
        if read_private(path) != value:
            raise ValueError("Frozen stage inputs changed; use a new run")
        return
    private_write(path, value)


HANDOFF_METADATA_OBSERVATION_FIELDS = frozenset({"temporal_state", "temporal_observed_at", "confirmed_open",
    "requires_fresh_lifecycle", "matching_eligible", "temporal_reason_codes", "closes_at"})


def handoff_metadata_identity(value):
    """Keep all source metadata frozen; clock-derived lifecycle is an observation."""
    if not isinstance(value, dict):
        raise ValueError("Handoff metadata must be a structured snapshot")
    return {key: item for key, item in value.items() if key not in HANDOFF_METADATA_OBSERVATION_FIELDS}


def snapshot_handoff_metadata(folder, common):
    """Replay source-identical work while appending fresh lifecycle evidence.

    An earlier version stored the first observation in metadata.private.json.
    Preserve those bytes, comparing its complete source projection. New folders
    store the static projection there; both retain strict content checks.
    """
    identity = handoff_metadata_identity(common)
    marker = folder / "metadata.private.json"
    if marker.exists():
        if handoff_metadata_identity(read_private(marker)) != identity:
            raise ValueError("Frozen handoff source metadata changed; preserve inputs for review")
    else:
        snapshot(marker, identity)
    observation = {"policy_version": "prod2_handoff_metadata_observation_v1",
        "metadata_identity_sha256": digest(identity), "metadata": common}
    path = folder / "metadata-observations" / (digest(observation) + ".private.json")
    snapshot(path, observation)
    return {"relative_path": str(path.relative_to(folder)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def download(conn, folder, conv, doc, manifest, pacer):
    guard = manifest.get("_handoff_runtime_guard")
    if guard is not None:
        guard.admit_document(doc["document_uuid"])
    suffix = Path(doc["filename"]).suffix.lower()
    kind = {".pdf": "pdf", ".doc": "ole", ".docx": "zip", ".zip": "zip", ".rar": "rar"}.get(suffix)
    if suffix == ".7z" and inventory_policy(manifest) == POLICY_7Z:
        kind = "7z"
    if kind is None:
        raise ValueError("Unsupported official source format")
    target, receipt = folder / (doc["document_uuid"] + suffix), folder / (doc["document_uuid"] + ".download.private.json")
    if receipt.exists():
        result = read_private(receipt)
        if result.get("document_uuid") != doc["document_uuid"] or hashlib.sha256(target.read_bytes()).hexdigest() != result["sha256"]:
            raise RuntimeError("Downloaded original changed")
    else:
        # A crash after file write but before receipt is reconciled by a fresh official download,
        # never by treating an unverified partial local file as current source evidence.
        if target.exists():
            if target.is_symlink():
                raise ValueError("Source symlink is forbidden")
            target.rename(folder / (target.name + ".unreconciled-" + str(time.time_ns())))
        with seace_http_client(timeout=90, verify=True, event_hooks=source_hooks(pacer)) as http:
            result = download_document(http, doc["document_uuid"], target, expected_format=kind,
                                       max_bytes=manifest["max_source_mib"] * 1024**2)
        private_write(receipt, result)
    if guard is not None:
        guard.bind_document(doc["document_uuid"], result["sha256"])
    record_document_download(conn, conv, result)
    return target, result


def preview_candidates(conn, folder, conv, candidates, parents, archives, downloads, manifest, pacer):
    """Read bounded local evidence; preserve original bytes and publication provenance."""
    from buenapro_worker.prod2.document_previews import document_preview, preview_anchor_mapping, _source_sha
    from buenapro_worker.prod2.document_selection import MAX_CANDIDATES
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError("Document inventory exceeds the bounded selector candidate policy")
    anchors = preview_anchor_mapping(manifest)
    files = {}
    for parent_id, (original, inventory) in archives.items():
        ids = [row["candidate_id"] for row in candidates
               if row["document_uuid"] == parent_id and row.get("inventory_status") == "candidate"
               and Path(row["filename"]).suffix.lower() in {".pdf", ".doc", ".docx"}]
        guard = manifest.get("_handoff_runtime_guard")
        if guard is not None:
            for row in candidates:
                if row["candidate_id"] in ids:
                    guard.admit_document(parent_id, member=row["member_path"], archive_sha256=row["archive_sha256"])
        for extracted in extract_members(original, inventory, candidate_ids=ids,
                                         destination=folder / (parent_id + ".members")):
            row = next(row for row in candidates if row["candidate_id"] == extracted["candidate_id"])
            row.update(original_sha256=extracted["original_sha256"], member_sha256=extracted["original_sha256"])
            if guard is not None:
                guard.bind_document(parent_id, extracted["original_sha256"], member=row["member_path"],
                    archive_sha256=row["archive_sha256"])
            files[row["candidate_id"]] = Path(extracted["path"])
    for row in candidates:
        if row["source_kind"] == "loose" and Path(row["filename"]).suffix.lower() in {".pdf", ".doc", ".docx"}:
            parent = next(doc for doc in parents if doc["document_uuid"] == row["document_uuid"])
            original, receipt = download(conn, folder, conv, parent, manifest, pacer)
            parent["original_sha256"] = receipt["sha256"]
            downloads[parent["document_uuid"]] = receipt
            row["original_sha256"] = receipt["sha256"]
            files[row["candidate_id"]] = original
    # Dynamic length keeps Jev below its 80k state ceiling, even for large inventories.
    maximum = min(3500, max(200, 55000 // max(1, len(files))))
    previews, anchored_by_source = {}, {}
    for row in candidates:
        if row["candidate_id"] not in files:
            continue
        sha = row["original_sha256"]
        options = {"anchor_source_sha256": sha, "anchor_pages": anchors[sha]} if sha in anchors else {}
        if options and _source_sha(files[row["candidate_id"]]) != sha:
            raise ValueError("Explicit preview anchor differs from the actual candidate bytes")
        if options and sha in anchored_by_source:
            value = anchored_by_source[sha]
        else:
            value = document_preview(files[row["candidate_id"]], folder / "text-previews" / digest(row["candidate_id"]),
                                     max_characters=maximum, max_ocr_pages=3, **options)
            if options:
                anchored_by_source[sha] = value
        if value["source_sha256"] != row["original_sha256"]:
            raise ValueError("Document preview does not bind its original source")
        row["preview_text"] = value["preview_text"]
        row["preview_content_hash"] = value["content_hash"]
        previews[row["candidate_id"]] = value
    snapshot(folder / "document-previews.private.json", previews)
    return files


def llm1_generation_options(manifest):
    """Legacy calls keep their limits; recovery can freeze larger explicit ones."""
    if "llm1_generation_limits" not in manifest:
        return {}
    value = manifest["llm1_generation_limits"]
    expected = {"policy_version": "prod2_explicit_extended_output_v1",
                "max_output_tokens": 65536, "timeout_ms": 300000}
    if not isinstance(value, dict) or value != expected or any(
            type(value.get(key)) is not int for key in ("max_output_tokens", "timeout_ms")):
        raise ValueError("Invalid frozen LLM1 generation limits")
    return {key: value[key] for key in ("max_output_tokens", "timeout_ms")}


def enrich(conn, settings, folder, item, manifest, pacer, limits, jev_key, *, capture=None, defer_reading=False):
    from buenapro_worker.prod2.selected_extractor import SelectedGeminiExtractor
    generation_options = llm1_generation_options(manifest)
    handoff_guard = getattr(limits, "handoff_guard", None)
    if handoff_guard is not None:
        # Runtime-only handle is never frozen, hashed or sent to a provider.
        manifest = {**manifest, "_handoff_runtime_guard": handoff_guard}
        generation_options = {**generation_options, "after_attempt": handoff_guard.observe_attempt}
    from buenapro_worker.prod2.luna_fallback import LUNA_FALLBACK_KEY, validate_luna_fallback_config
    fallback_config = validate_luna_fallback_config(manifest[LUNA_FALLBACK_KEY]) if LUNA_FALLBACK_KEY in manifest else None
    links_only = official_links_only(manifest)
    if links_only and (folder / "temporary-cleanup.private.json").exists():
        raise RuntimeError("Completed files were purged; resume through the durable outcome, not document preparation")
    from buenapro_worker.prod2.reading_policy import reading_policy
    compact_policy = reading_policy(manifest)
    from buenapro_worker.prod2.extraction_policy import get_scope_policy
    from buenapro_worker.prod2.input_policy import get_input_policy, prepare_input
    from buenapro_worker.extraction.commercial_classification import get_commercial_policy
    scope_policy, input_policy = get_scope_policy(manifest), get_input_policy(manifest)
    commercial_policy = get_commercial_policy(manifest)
    if compact_policy and (scope_policy or input_policy or commercial_policy):
        raise ValueError("Legacy compact reading cannot use the new scope/input policies")
    compact_binding = None
    if "compact_sources" in manifest:
        from buenapro_worker.prod2.compact_reading import load_compact_source
        bindings = manifest["compact_sources"]
        if (compact_policy is None or not isinstance(bindings, dict) or not 1 <= len(bindings) <= 10
                or not isinstance(item, dict) or str(item["row"]["convocation_id"]) not in bindings):
            raise ValueError("Compact reading has no exact frozen source binding")
        compact_binding = bindings[str(item["row"]["convocation_id"])]
        if compact_binding.get("convocation_id") != item["row"]["convocation_id"]:
            raise ValueError("Compact source is bound to another candidate")
        load_compact_source(compact_binding)
    from buenapro_worker.prod2.document_previews import preview_anchor_mapping
    preview_anchor_mapping(manifest)
    archive_policy = inventory_policy(manifest)
    if archive_policy == POLICY_7Z:
        from buenapro_worker.prod2.sevenzip_schema import require_schema
        require_schema(conn)
    word_options = {}
    if "word_conversion_policy" in manifest:
        if manifest["word_conversion_policy"] != "word_footnotes_at_end_v1":
            raise ValueError("Invalid frozen Word conversion policy")
        word_options["word_conversion_policy"] = manifest["word_conversion_policy"]
    if "llm1_model_routing" in manifest:
        resolve_llm1_model(manifest, 1, settings.gemini_model)
    conv = item["row"]["convocation_id"]
    capture = capture if capture is not None else get_detail(item, folder, pacer, source_hooks(pacer))
    detail = capture["detail"]
    ficha_description = None
    if "llm1_ficha_context_policy" in manifest:
        if manifest["llm1_ficha_context_policy"] != LLM1_FICHA_CONTEXT_POLICY:
            raise ValueError("Unsupported selected LLM1 ficha context policy")
        ficha_description = ficha_reading_description(detail.get("description"))
        if ficha_description != ficha_reading_description(item["row"]["description"]):
            raise ValueError("Official ficha description differs from the discovery used")
    saved_detail = persist_detail(conn, detail, observed_at=datetime.fromisoformat(capture["observed_at"]), source_url=SEARCH_URL)
    temporal = decide(detail, datetime.now(timezone.utc) if handoff_guard is not None
                      else datetime.fromisoformat(capture["observed_at"]))
    save_participation_assessment(conn, conv, detail_hash=saved_detail["detail_hash"], assessment=temporal)
    parents = official_inventory(detail)
    common = {"schedule": detail["schedule"], "item_states": detail["item_states"],
        "temporal_state": temporal["state"], "document_scope": SELECTED_DOCUMENTS_POLICY,
        "document_inventory_complete": detail["document_inventory_complete"],
        "documents": parents, "detail_hash": saved_detail["detail_hash"]}
    if links_only:
        common["document_storage_policy"] = OFFICIAL_LINKS_POLICY
    if manifest.get("keyword_tracking"):
        common.update(temporal_observed_at=capture["observed_at"],
            confirmed_open=temporal.get("confirmed_open", False), temporal_reason_codes=temporal.get("reason_codes", []))
    if handoff_guard is not None:
        from buenapro_worker.prod2.handoff_guards import lifecycle_fields
        common.update(lifecycle_fields(temporal))
        common["matching_eligible"] = common["matching_eligible"] and detail["document_inventory_complete"] is True
    if handoff_guard is not None:
        common["metadata_observation"] = snapshot_handoff_metadata(folder, common)
    else:
        snapshot(folder / "metadata.private.json", common)
    if handoff_guard is not None:
        from buenapro_worker.prod2.handoff_guards import lifecycle_gate
        blocked = lifecycle_gate(temporal)
        if blocked is not None:
            return {**common, **blocked}
    if defer_reading:
        # Below llm1_min_affinity: ficha and participation window are saved; nothing is downloaded or paid for.
        return {**common, "status": "accepted_unread", "llm1_min_affinity": llm1_min_affinity(manifest)}
    if detail["document_inventory_complete"] is not True:
        raise RuntimeError("Official inventory incomplete; selector/LLM1 not called")
    candidates, files, archives, downloads = [], {}, {}, {}
    for parent in parents:
        suffix = Path(parent["filename"]).suffix.lower()
        if suffix in {".zip", ".rar"} or (suffix == ".7z" and archive_policy == POLICY_7Z):
            original, receipt = download(conn, folder, conv, parent, manifest, pacer)
            parent["original_sha256"] = receipt["sha256"]
            downloads[parent["document_uuid"]] = receipt
            inventory = list_archive(original, parent["document_uuid"],
                max_source_bytes=manifest["max_source_mib"] * 1024**2,
                max_expanded_bytes=manifest["max_expanded_mib"] * 1024**2,
                max_member_bytes=manifest["max_source_mib"] * 1024**2,
                max_members=manifest["max_archive_members"], policy_version=archive_policy)
            snapshot(folder / (parent["document_uuid"] + ".archive.private.json"), inventory)
            archives[parent["document_uuid"]] = (original, inventory)
            for member in inventory["members"]:
                if member["status"] == "directory":
                    continue
                candidates.append({**parent, "candidate_id": member["candidate_id"], "source_kind": "archive_member",
                    "filename": Path(member["member_path"]).name, "parent_filename": parent["filename"],
                    "member_path": member["member_path"], "member_index": member["member_index"],
                    "archive_inventory_hash": inventory["inventory_hash"], "archive_sha256": receipt["sha256"],
                    "inventory_status": member["status"]})
                # Parent SHA is not the original member SHA.
                candidates[-1].pop("original_sha256", None)
                if archive_policy == POLICY_7Z:
                    candidates[-1]["archive_inventory_policy"] = archive_policy
        else:
            candidates.append({**parent, "candidate_id": "loose:" + parent["document_uuid"], "source_kind": "loose"})
    if not candidates:
        return {**common, "status": "documents_missing", "requires_human_review": True}
    if manifest.get("document_previews"):
        files = preview_candidates(conn, folder, conv, candidates, parents, archives, downloads, manifest, pacer)
    policy_version = manifest.get("selector_policy_version", DOCUMENT_SELECTION_POLICY)
    selector_hash = document_selection_request_hash(item["row"]["description"], candidates, policy_version=policy_version)
    if compact_binding is not None:
        from buenapro_worker.prod2.compact_reading import load_compact_source
        previous = load_compact_source(compact_binding)
        if selector_hash != previous["selection_context"]["selector_request_hash"]:
            raise ValueError("Compact reading changed the original document selector request")
    with limits.model("selector:" + selector_hash), provider_permission(conn, folder, limits,
            provider="jev", kind="selector", request_hash=selector_hash) as issue:
        http = AuditedJevClient(folder, limits, purpose="document_selection", before_request=guarded_issue(issue),
            **({"after_attempt": handoff_guard.observe_attempt} if handoff_guard is not None else {}))
        try:
            selector = JevDocumentSelector(jev_key, client=http, policy_version=policy_version)
            selection, reused = paid_document_selection(conn, folder, convocation_id=conv,
                description=item["row"]["description"], candidates=candidates, selector=selector,
                high_threshold=manifest["selector_high"], low_threshold=manifest["selector_low"])
        finally:
            http.close()
    snapshot(folder / "selection.private.json", selection)
    common.update(selector_cached=reused, selected_ids=selection["selected_ids"],
        pending_ids=selection["pending_ids"], excluded_ids=selection["excluded_ids"],
        selector_request_hash=selector_hash, selector_cost_usd=0 if reused else selection["cost_usd"],
        selector_policy_version=policy_version, document_previews=bool(manifest.get("document_previews")))
    if not selection["selected_ids"]:
        return {**common, "status": "selection_needs_review", "requires_human_review": True}
    if manifest.get("selection_only"):
        return {**common, "status": "selection_complete", "requires_human_review": selection["requires_human_review"]}
    selected = [dict(row) for row in candidates if row["candidate_id"] in selection["selected_ids"]]
    for parent_id, (original, inventory) in archives.items():
        ids = [row["candidate_id"] for row in selected if row["document_uuid"] == parent_id]
        if not ids:
            continue
        if handoff_guard is not None:
            for row in selected:
                if row["candidate_id"] in ids:
                    handoff_guard.admit_document(parent_id, member=row["member_path"], archive_sha256=row["archive_sha256"])
        for extracted in extract_members(original, inventory, candidate_ids=ids, destination=folder / (parent_id + ".members")):
            row = next(row for row in selected if row["candidate_id"] == extracted["candidate_id"])
            row.update(original_sha256=extracted["original_sha256"], member_sha256=extracted["original_sha256"])
            if handoff_guard is not None:
                handoff_guard.bind_document(parent_id, extracted["original_sha256"], member=row["member_path"],
                    archive_sha256=row["archive_sha256"])
            files[row["candidate_id"]] = Path(extracted["path"])
    for row in selected:
        if row["source_kind"] != "loose":
            continue
        parent = next(doc for doc in parents if doc["document_uuid"] == row["document_uuid"])
        original, receipt = download(conn, folder, conv, parent, manifest, pacer)
        parent["original_sha256"] = receipt["sha256"]
        downloads[parent["document_uuid"]] = receipt
        row["original_sha256"] = receipt["sha256"]
        files[row["candidate_id"]] = original
    context = {"official_inventory": parents, "selector_request_hash": selector_hash,
        "selector_receipt_hash": canonical_hash(selection["raw_response"]), "candidates": candidates,
        "decisions": selection["decisions"], "thresholds": selection["thresholds"]}
    if archive_policy == POLICY_7Z:
        context["archive_inventory_policy"] = archive_policy
    if ficha_description is not None:
        context["ficha_description"] = ficha_description
    if compact_policy is not None:
        context["llm1_reading_policy"] = compact_policy
    if scope_policy is not None:
        context["llm1_scope_policy"] = scope_policy
    if commercial_policy is not None:
        context["llm1_commercial_taxonomy"] = commercial_policy
    if input_policy is not None:
        context["llm1_input_policy"] = input_policy
    if "llm1_visual_review_policy" in manifest:
        context["llm1_visual_review_policy"] = manifest["llm1_visual_review_policy"]
    if "llm1_retrieved_transport_policy" in manifest:
        context["llm1_retrieved_transport_policy"] = manifest["llm1_retrieved_transport_policy"]
    if "llm1_source_coverage_policy" in manifest:
        context["llm1_source_coverage_policy"] = manifest["llm1_source_coverage_policy"]
    if links_only and input_policy != "prod2_embedding_retrieved_text_v1":
        prior = reuse_completed_read(conn, settings, folder, conv, manifest["profile_id"],
            item["query"]["object_type"], selected, files, context, manifest)
        if prior is not None:
            first = selected[0]
            anchor = first["document_uuid"]
            anchor_sha = first.get("archive_sha256", first["original_sha256"])
            bundle = prior["bundle"]
            saved = persist_completed_llm1(conn, convocation_id=conv, document_uuid=anchor,
                extraction={**bundle, "evaluation_id": prior["evaluation_id"], "allowed_models": [prior["model"]]},
                document_sha256=anchor_sha, source_url=downloads[anchor]["source_url"])
            return {**common, **saved, "status": "llm1_complete", "llm1_cached": True,
                "llm1_reused_before_preparation": True, "llm1_cost_usd": 0,
                "llm1_evaluation_id": prior["evaluation_id"], "llm1_request_hash": prior["request_hash"],
                "llm1_receipt_hash": canonical_hash(bundle), "llm1_model": prior["model"],
                **({"llm1_model_routing": prior["routing"]} if prior["routing"] is not None else {}),
                "facet_count": len(bundle["facets"]), "analysis_page_count": prior["page_count"],
                **{key: prior[key] for key in ("analysis_bytes", "transport_parts") if prior.get(key) is not None},
                "document_set_hash": prior["document_set_hash"],
                "source_document_manifest": prior["source_document_manifest"]}
    preparation_options = {"max_source_bytes": manifest["max_source_mib"] * 1024**2,
                           "cache_folder": folder / "preparation-cache" if links_only else Path(manifest["cache_folder"]),
                           **word_options}
    if compact_binding is None:
        prepared = prepare_selected_document_set(folder, selected, files, context, **preparation_options)
    else:
        from buenapro_worker.prod2.compact_reading import prepare_compact_document_set
        prepared = prepare_compact_document_set(folder, selected, files, context, compact_binding, **preparation_options)
    if input_policy == "prod2_embedding_retrieved_text_v1":
        from buenapro_worker.prod2.embedding_runtime import prepare_retrieved_input, validate_frozen_config
        if manifest.get("embedding_ocr_cache_sources"):
            prepared["embedding_ocr_cache_sources"] = manifest["embedding_ocr_cache_sources"].get(str(conv), ())
        # The memo gives the same context and hash; without it every query re-scans all blocks for references.
        prepared = prepare_retrieved_input(conn, settings, prepared, folder, limits,
            config=validate_frozen_config(manifest), convocation_id=conv, stop_requested=STOP.is_set,
            memoize_expansion=True)
    prepared, local_text_result = prepare_input(prepared, folder)
    model, routing = resolve_llm1_model(manifest, prepared["page_count"], settings.gemini_model)
    if compact_binding is not None and model != compact_binding["model"]:
        raise ValueError("Compact reading changed the model of the frozen source")
    # Pydantic copies isolate concurrent candidates; the shared settings never change.
    llm1_settings = settings if model == settings.gemini_model else settings.model_copy(update={"gemini_model": model})
    pdf = prepared["pdf_path"].read_bytes()
    document_context = {key: prepared[key] for key in ("document_scope", "document_set", "document_set_hash",
                                                     "document_set_identity", "selection_context")}
    if "llm1_input" in prepared:
        document_context["llm1_input"] = prepared["llm1_input"]
    first = selected[0]
    anchor = first["document_uuid"]
    anchor_sha = first.get("archive_sha256", first["original_sha256"])
    if not links_only:
        preview = upload_preview(pdf, convocation_id=conv, document_uuid=anchor,
            original_sha256=anchor_sha, env_path=ROOT / ".env.local")
        record_document_download(conn, conv, downloads[anchor], storage_key=preview["storage_key"])
    request_hash = extraction_request_hash(llm1_settings, pdf, object_type=item["query"]["object_type"],
        original_sha256=anchor_sha, manifest=prepared["source_document_manifest"], document_context=document_context)
    if compact_binding is not None and request_hash == compact_binding["request_hash"]:
        raise ValueError("Compact reading must own a new document/prompt request")
    def before_llm1(issue):
        def compact_issue():
            from buenapro_worker.prod2.compact_reading import load_compact_source
            load_compact_source(compact_binding)
            return guarded_issue(issue)()
        return compact_issue if compact_binding is not None else guarded_issue(issue)
    from buenapro_worker.prod2.selected_read import allowed_llm1_models, paid_selected_read
    evaluation_id, bundle, hit, request_hash, execution = paid_selected_read(conn, llm1_settings, folder,
        convocation_id=conv, profile_id=manifest["profile_id"], object_type=item["query"]["object_type"],
        anchor=anchor, anchor_sha=anchor_sha, pdf=pdf, prepared=prepared, document_context=document_context,
        limits=limits, before_request_factory=before_llm1, local_text_result=local_text_result,
        generation_options=generation_options, fallback_config=fallback_config, include_transport_facts=links_only,
        paid_stage=paid_llm1)
    model = bundle["model"]
    saved = persist_completed_llm1(conn, convocation_id=conv, document_uuid=anchor,
        extraction={**bundle, "evaluation_id": evaluation_id, "allowed_models": allowed_llm1_models(model)},
        document_sha256=anchor_sha, source_url=downloads[anchor]["source_url"])
    return {**common, **saved, "status": "llm1_complete", "llm1_cached": hit,
        **({"llm1_evaluation_id": evaluation_id, "llm1_request_hash": request_hash,
            "llm1_receipt_hash": canonical_hash(bundle)} if links_only else {}),
        "llm1_model": model,
        **({"llm1_primary_model_routing" if model == "gpt-6-luna" else "llm1_model_routing": routing}
           if routing is not None else {}),
        **({"llm1_provider": "openai", "llm1_reasoning_effort": "low",
            "llm1_fallback_from": execution.get("primary_failure", bundle.get("llm1_fallback_from"))}
           if model == "gpt-6-luna" else {}),
        "llm1_cost_usd": (0 if hit else bundle["cost_usd"]) + execution["primary_cost_usd"],
        "facet_count": len(bundle["facets"]),
        "analysis_page_count": prepared["page_count"], "analysis_bytes": prepared["analysis_bytes"],
        "transport_parts": len(prepared["chunks"]), "document_set_hash": prepared["document_set_hash"],
        **({"llm1_input": prepared["llm1_input"], "embedding_stage": read_private(folder / "embedding-stage.private.json")}
           if input_policy == "prod2_embedding_retrieved_text_v1" else {}),
        "source_document_manifest": prepared["source_document_manifest"]}


def cleanup_finished_case(conn, private, record, manifest):
    if record.get("llm1_prior_preserved"):
        # The read-only guard did not create/download analysis inputs here.
        # Historical folders and their pending-payment inputs stay untouched.
        return None
    if not official_links_only(manifest) or record.get("status") != "llm1_complete":
        return None
    from buenapro_worker.prod2.temporary_storage import cleanup_completed_case
    try:
        proof = verify_completed_storage(conn, private, record)
        roots = (ROOT / "tmp",) if private.resolve().is_relative_to(ROOT / "tmp") else (private.parent,)
        report = cleanup_completed_case(private, verification=proof, reference_roots=roots)
    except Exception as error:
        # A cleanup failure cannot invalidate a paid/persisted read or cause another payment.
        report = {"status": "retained_verification_failed", "error_type": type(error).__name__,
                  "deleted_bytes": 0, "deleted_files": 0}
    private_write(private / ("temporary-cleanup-check-" + str(time.time_ns()) + ".private.json"), report)
    summary = {key: report.get(key) for key in ("status", "deleted_bytes", "deleted_files", "cumulative_deleted_bytes", "error_type")}
    summary["retained_files"] = len(report.get("retained", []))
    print(json.dumps({"convocation_id": record["convocation_id"], "temporary_cleanup": summary}), flush=True)
    return report


def handoff_detail(item, private, pacer):
    """Reacquire only this ID's native ficha; never trust an old open window."""
    root = private / "ficha-observations"
    capture = None
    now = datetime.now(timezone.utc)
    for path in sorted(root.glob("*/detail.private.json"), reverse=True):
        value = read_private(path)
        observed = datetime.fromisoformat(value["observed_at"])
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 60:
            continue
        capture = value
        break
    if capture is None:
        fresh = root / str(time.time_ns())
        fresh.mkdir(parents=True, mode=0o700)
        try:
            capture = get_detail(item, fresh, pacer, source_hooks(pacer))
        except RuntimeError as error:
            from buenapro_worker.prod2.handoff_guards import BOUNDED_ID_NOT_LOCATED, BoundedNativeIdentityNotLocated
            if type(error) is not RuntimeError or str(error) != BOUNDED_ID_NOT_LOCATED:
                raise
            receipt = {"policy_version": "prod2_handoff_guards_v1",
                "reason": "bounded_native_identity_not_located", "convocation_id": item["row"]["convocation_id"],
                "procedure_id": item["row"]["procedure_id"], "query_hash": digest(item["query"]),
                "captured_page_number": item["page_number"], "neighbor_radius": 2,
                "mapped_observed_at": item["observed_at"], "attempted_at": now.isoformat(),
                "provider_calls": 0, "pdf_downloads": 0,
                "interpretation": "bounded_lookup_inconclusive_not_evidence_of_deletion_or_closed_status"}
            receipt_path = private / "bounded-native-identity-review.private.json"
            if receipt_path.exists():
                raise ValueError("Bounded identity review requires its durable outcome; do not repeat source lookup") from None
            private_write(receipt_path, receipt)
            proof = {"reason": receipt["reason"], "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
            raise BoundedNativeIdentityNotLocated(proof) from None
    detail = capture["detail"]
    if (detail.get("convocatoria_id") != item["row"]["convocation_id"]
            or detail.get("procedure_id") != item["row"]["procedure_id"]
            or ficha_reading_description(detail.get("description")) != ficha_reading_description(item["row"]["description"])):
        raise ValueError("Frozen handoff ficha identity or title changed")
    return capture


def consume(settings, staging, manifest, folder, pacer, limits, jev_key):
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
            application_name="buenapro-prod2-consumer:" + manifest.get("run_id", folder.name), connect_timeout=15,
            keepalives_idle=30, keepalives_interval=10, keepalives_count=3) as conn:
        while not STOP.is_set():
            job = staging.next()
            if STOP.is_set():
                # STOP may arrive while reserving: leave this payload pending
                # for recovery after all consumers finish, without ACK/paying.
                return
            if job is None:
                return
            candidate_id, item = job
            conv = item["row"]["convocation_id"]
            private = folder / str(conv)
            private.mkdir(mode=0o700, exist_ok=True)
            outcome_path = private / "outcome.private.json"
            if outcome_path.exists():
                previous = read_private(outcome_path)
                if manifest.get("handoff_selection"):
                    if (previous.get("convocation_id") != conv
                            or previous.get("procedure_id") != item["row"]["procedure_id"]
                            or previous.get("handoff_provenance") != item["handoff_provenance"]):
                        raise ValueError("Handoff outcome does not bind the frozen selected identity")
                cleanup_finished_case(conn, private, previous, manifest)
                staging.ack(candidate_id)
                continue
            if not limits.can_process(conv):
                # Still pending; coordinator recovers it only after ALL consumers stop.
                return
            record = {"convocation_id": conv, "description": item["row"]["description"],
                "object_type": item["row"]["object_type"], "accepted": False, "status": "needs_review"}
            if manifest.get("handoff_selection"):
                record.update(procedure_id=item["row"]["procedure_id"],
                    handoff_provenance=item["handoff_provenance"], source_query=item["query"],
                    source_page_number=item["page_number"], mapped_observed_at=item["observed_at"],
                    requires_fresh_lifecycle=True, confirmed_open=False, matching_eligible=False)
            business = item.get("discovery_business_context") or manifest.get("business_plan", {}).get("keyword_index", {}).get(item["query"]["keyword"])
            if business:
                record.update(discovery_keyword=item["query"]["keyword"],
                    business_batch_id=business["search_batch_id"], keyword_business_context=business)
            try:
                handoff_guard = getattr(limits, "handoff_guard", None)
                capture = None
                if handoff_guard is not None:
                    from buenapro_worker.prod2.handoff_guards import preserve_by_id, lifecycle_gate, lifecycle_fields
                    handoff_guard.admit_candidate(conv)
                    prior = preserve_by_id(conn, item, profile_id=manifest["profile_id"])
                    if prior is not None:
                        record.update(prior)
                        if prior["status"] == "llm1_complete":
                            if limits.admit(conv, lambda: True) is None:
                                return
                            record.update(accepted=True, requires_fresh_lifecycle=True,
                                          confirmed_open=False, matching_eligible=False)
                            capture = handoff_detail(item, private, pacer)
                            assessment = decide(capture["detail"], datetime.now(timezone.utc))
                            saved = persist_detail(conn, capture["detail"],
                                observed_at=datetime.fromisoformat(capture["observed_at"]), source_url=SEARCH_URL)
                            save_participation_assessment(conn, conv, detail_hash=saved["detail_hash"], assessment=assessment)
                            canonical = prior["prior_reading_preserved"]["canonical"]
                            listed = canonical["document_uuid"] in {
                                value.get("uuid") for value in capture["detail"].get("documents", [])}
                            inventory_complete = capture["detail"].get("document_inventory_complete") is True
                            record.update(lifecycle_fields(assessment), detail_hash=saved["detail_hash"],
                                schedule=capture["detail"].get("schedule", []),
                                canonical_document_still_listed=listed,
                                document_inventory_complete=inventory_complete)
                            record["matching_eligible"] = record["matching_eligible"] and listed and inventory_complete
                        private_write(private / "prior-reading-preserved.private.json", prior)
                        private_write(outcome_path, record)
                        staging.ack(candidate_id)
                        continue
                    capture = handoff_detail(item, private, pacer)
                    assessment = decide(capture["detail"], datetime.now(timezone.utc))
                    private_write(private / ("participation-gate-" + str(time.time_ns()) + ".private.json"), assessment)
                    record.update(lifecycle_fields(assessment), matching_eligible=False)
                    blocked = lifecycle_gate(assessment)
                    if blocked is not None:
                        record.update(blocked, temporal_state=assessment["state"],
                            temporal_observed_at=assessment["as_of"], confirmed_open=False,
                            temporal_reason_codes=assessment["reason_codes"],
                            schedule=capture["detail"].get("schedule", []))
                        private_write(outcome_path, record)
                        staging.ack(candidate_id)
                        continue
                request_hash = jev_request_hash(item["row"]["description"], commercial_profiles=manifest["commercial_profiles"])
                with limits.model("affinity:" + request_hash), provider_permission(conn, private, limits,
                        provider="jev", kind="affinity", request_hash=request_hash) as issue:
                    http = AuditedJevClient(private, limits, before_request=guarded_issue(issue),
                        **({"after_attempt": handoff_guard.observe_attempt} if handoff_guard is not None else {}))
                    try:
                        analyzer = JevDescriptionAnalyzer(jev_key, client=http)
                        request_hash, result, hit = paid_jev(conn, private, item["row"], manifest["commercial_profiles"],
                            manifest["profile_id"], analyzer, repo)
                    finally:
                        http.close()
                is_accepted = accepted(result)
                if is_accepted:
                    result["request_context"] = {"description": item["row"]["description"], "commercial_profiles": manifest["commercial_profiles"]}
                record.update(affinity_index=result["affinity_index"], accepted=is_accepted, jev_cached=hit,
                              jev_cost_usd=0 if hit else result["cost_usd"], status="rejected_below_60")
                if is_accepted:
                    promotion = limits.admit(conv, lambda: repo.promote_accepted(conn, run_id=manifest["run_id"],
                        row=item["row"], query_manifest=item["query"],
                        observed_at=datetime.fromisoformat(item["observed_at"]), jev_result=result))
                    if promotion is None:
                        return  # paid decision reusable; deferred candidate never disappears
                    record["opportunity_created"] = promotion["created"]
                    from buenapro_worker.prod2.prior_reading_guard import preserve_prior_reading
                    prior = preserve_prior_reading(conn, convocation_id=conv,
                        profile_id=manifest["profile_id"], policy=manifest.get("prior_reading_policy"))
                    if prior is not None:
                        private_write(private / "prior-reading-preserved.private.json", prior)
                    # A prior reading keeps its outcome; otherwise a low-affinity candidate is listed unread.
                    unread = result["affinity_index"] < llm1_min_affinity(manifest)
                    record.update(prior if prior is not None else
                                  enrich(conn, settings, private, item, manifest, pacer, limits, jev_key,
                                      **({"capture": capture} if handoff_guard is not None else {}),
                                      **({"defer_reading": True} if unread else {})))
                else:
                    repo.record_run_member(conn, run_id=manifest["run_id"], convocation_id=conv,
                        request_hash=request_hash, accepted=False, result=result)
            except Exception as error:
                if egress_failure(error):
                    EGRESS_DOWN.set()
                    STOP.set()
                if EGRESS_DOWN.is_set() and (egress_failure(error) or isinstance(error, LimitReached)):
                    # A proxy outage is not a case outcome or a source denial: keep the
                    # payload pending and write no guard marker or needs_review result.
                    return
                handoff_guard = getattr(limits, "handoff_guard", None)
                if handoff_guard is not None:
                    try:
                        handoff_guard.check_audit(audit_summary(folder))
                    except Exception as audit_error:
                        stop_handoff_after_local_error(handoff_guard, audit_error,
                            phase="consumer_cost_audit", convocation_id=conv)
                        return
                    if isinstance(error, LimitReached):
                        if not STOP.is_set():
                            handoff_guard.budget_deferred = True
                        return  # pending payload and paid receipts remain resumable
                    if STOP.is_set():
                        return  # preserve the in-flight reservation and its receipts
                # Local OCR checks STOP at page boundaries and checkpoints its
                # text. Keep the Redis payload queued, rather than manufacturing
                # a terminal error or retrying any paid stage.
                from buenapro_worker.prod2.embedding_corpus import CorpusStopped
                if isinstance(error, CorpusStopped) and STOP.is_set():
                    return
                dispatch_stop = stop_after_uncertain_embedding_error(error)
                metadata = private / "metadata.private.json"
                if metadata.exists():
                    saved_metadata = read_private(metadata)
                    record.update(handoff_metadata_identity(saved_metadata) if handoff_guard is not None
                        else saved_metadata)
                from buenapro_worker.prod2.handoff_guards import BoundedNativeIdentityNotLocated, document_preparation_review
                if (handoff_guard is not None and type(error) is BoundedNativeIdentityNotLocated
                        and not STOP.is_set()):
                    record.update(status="opening_not_confirmed", requires_human_review=True,
                        requires_fresh_lifecycle=True, confirmed_open=False, matching_eligible=False,
                        new_document_requests=0, reading_gate={"policy_version": "prod2_handoff_guards_v1",
                            "allowed": False, **error.proof})
                else:
                    record.update(status="needs_review", **safe_error_facts(error))
                    if handoff_guard is not None:
                        record["matching_eligible"] = False
                        proof = document_preparation_review(error, private) if not STOP.is_set() else None
                        if proof is not None:
                            record.update(document_preparation_review=proof, requires_human_review=True)
                        else:
                            stop_handoff_after_local_error(handoff_guard, error,
                                phase="consumer_case", convocation_id=conv)
                            return  # no terminal outcome or ACK for an unexpected failure
                if dispatch_stop is not None:
                    record["embedding_dispatch_stop"] = dispatch_stop
            private_write(outcome_path, record)
            cleanup_finished_case(conn, private, record, manifest)
            staging.ack(candidate_id)
            summary = {key: record.get(key) for key in ("convocation_id", "affinity_index", "accepted", "status")}
            if business:
                summary["business_batch_id"] = business["search_batch_id"]
            print(json.dumps(summary), flush=True)


def planned_queries(manifest):
    return [{"keyword": keyword, "object_type": object_type, "year": year,
        "published_from": manifest["published_from"], "published_to": manifest["published_to"], "seace_version": "3"}
        for keyword in manifest["keywords"] for year in manifest["resolved_years"] for object_type in manifest["objects"]]


def run_sweeps(manifest, folder, pacer, staging, limits, tracker=None):
    from buenapro_worker.prod2.keyword_sweep import sweep_query
    from buenapro_worker.prod2.empty_scope_probe import probe_empty_scope, scope_is_verified_zero
    if tracker:
        errors = tracker.completed_cache_errors()
        if errors:
            by_query = {digest(error["query"]): error for error in errors}
            return [by_query.get(digest(query)) or {"query": query, "status": "blocked", "coverage_complete": False,
                "source_atomic_snapshot": False, "errors": [{"code": "campaign_completed_cache_invalid"}]}
                for query in planned_queries(manifest)], []
    proofs = []
    if manifest["probe_empty_scopes"]:
        for year in manifest["resolved_years"]:
            for object_type in manifest["objects"]:
                if STOP.is_set():
                    break
                query = {"keyword": "", "object_type": object_type, "year": year,
                    "published_from": manifest["published_from"], "published_to": manifest["published_to"], "seace_version": "3"}
                proof_folder = tracker.folder if tracker else folder
                proof = probe_empty_scope(query, source_factory=lambda: source_factory(pacer), folder=proof_folder / "scope-probes")
                proofs.append(proof)
                if any(error["code"] in {"source_403", "source_429"} for error in proof["errors"]):
                    STOP.set()
                if any(error["code"] == "egress_unavailable" for error in proof["errors"]):
                    EGRESS_DOWN.set()
                    STOP.set()
    queries = planned_queries(manifest)
    def staging_payload(item):
        business = manifest.get("business_plan", {}).get("keyword_index", {}).get(item["query"]["keyword"])
        return {**item, **({"discovery_business_context": business} if business else {})}
    def emit(item):
        return limits.stage(staging, str(item["row"]["convocation_id"]), staging_payload(item))
    def emit_many(items):
        try:
            return limits.stage_many(staging, [(str(item["row"]["convocation_id"]), staging_payload(item))
                for item in items])
        except Exception:
            # A response may be lost after Redis applied some commands. Leave
            # the dedup/queue durable and do not start paid consumers on an
            # incomplete replay; the next exclusive owner reconciles it.
            STOP.set()
            raise
    def execute(query, query_folder):
        for proof in proofs:
            if scope_is_verified_zero(proof, query):
                return {"query": query, "status": "complete", "coverage_complete": True,
                    "coverage_kind": "verified_empty_scope_observed", "source_atomic_snapshot": False,
                    "scope_proof_hash": proof["proof_hash"], "http_searches": 0, "http_pages": 0, "errors": [],
                    "source_observed_at": max(row["observed_at"] for row in proof["observations"])}
        if STOP.is_set() or limits.sweep_stopped():
            return {"query": query, "status": "stopped", "coverage_complete": False,
                "source_atomic_snapshot": False, "errors": [{"code": "stop_requested_or_candidate_cap"}]}
        result = sweep_query(query, source_factory=lambda: source_factory(pacer),
            emit=emit,
            emit_many=emit_many if limits.max_candidates is None else None,
            folder=query_folder, stop_requested=lambda: STOP.is_set() or limits.sweep_stopped())
        if any(error["code"] in {"source_403", "source_429"} for error in result["errors"]):
            STOP.set()
        if any(error["code"] == "egress_unavailable" for error in result["errors"]):
            EGRESS_DOWN.set()
            STOP.set()
        return result
    def sweep(query):
        if tracker:
            # Leave unstarted queries pending; stopped is never completion.
            if STOP.is_set() or limits.sweep_stopped():
                return {"query": query, "status": "stopped", "coverage_complete": False,
                    "source_atomic_snapshot": False, "errors": [{"code": "stop_requested_or_candidate_cap"}]}
            return tracker.run(query, lambda path: execute(query, path))
        return execute(query, folder / "queries" / digest(query))
    with ThreadPoolExecutor(max_workers=manifest["search_workers"]) as pool:
        return list(pool.map(sweep, queries)), proofs


def main():
    global HANDOFF_GUARD
    HANDOFF_GUARD = None
    EGRESS_DOWN.clear()
    args = parser().parse_args()
    os.umask(0o077)
    requested = plan(args)
    policy = execution_policy(args)
    start, end = publication_window(datetime.now(timezone.utc), months=args.months,
        published_from=requested["from_date"], published_to=requested["to_date"])
    handoff_import = None
    if requested.get("handoff_selection"):
        from buenapro_worker.prod2.handoff_runtime_recovery import load_selection
        handoff_import = load_selection(args.mapping_handoff, args.selection, args.selection_sha256, root=ROOT,
            recovery_path=args.runtime_recovery, recovery_sha256=args.runtime_recovery_sha256, reader_folder=args.folder)
        if handoff_import["binding"] != requested["handoff_selection"]:
            raise ValueError("ID handoff inputs changed while planning")
    resume_binding = embedding_resume_binding(requested, args.folder)
    if args.dry_run:
        print(json.dumps({**requested, "published_from": start, "published_to": end, "dry_run": True,
            "execution_policy": policy,
            "coverage": "sha_verified_selected_IDs; lifecycle_from_fresh_ficha" if handoff_import else "configured_keywords_date_window; lifecycle_from_ficha",
            "base_queries": 0 if handoff_import else len(requested["keywords"]) * 2 * len(requested["years"]) if requested["years"] != "auto" else "keywords*2*native_year_count",
            "cost_example_usd": {"jev_100000_at_1750_input_tokens": 7.35,
                "selector_100000_at_3000_input_tokens": 12.60,
                "llm1_3_1_flash_lite_each_10000_input_2000_output_tokens": .0055,
                    "llm1_2_5_flash_lite_each_10000_input_2000_output_tokens": .0018,
                    "llm1_luna_low_each_10000_input_2000_total_output_tokens": .002},
            "cost_note": "Hypothetical tokens; accepted-document tokens, cached requests and provider failures change actual cost."}, ensure_ascii=False))
        return 0
    if args.prepare_selector_cache:
        from buenapro_worker.prod2.selected_repository import prepare_schema
        with runtime(args) as (settings, _):
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
                result = prepare_schema(conn, ROOT / "db/migrations/0039_prod2_document_selection_cache.sql")
        print(json.dumps({**result, "provider_calls": 0, "source_calls": 0}))
        return 0
    if args.prepare_keyword_tracking:
        from buenapro_worker.prod2.keyword_tracking import prepare_schema
        with runtime(args) as (settings, _):
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
                result = prepare_schema(conn, ROOT / "db/migrations/0040_prod2_keyword_tracking.sql")
        print(json.dumps({**result, "provider_calls": 0, "source_calls": 0}))
        return 0
    if args.prepare_document_events:
        from buenapro_worker.prod2.document_events import prepare_schema
        with runtime(args) as (settings, _):
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
                result = prepare_schema(conn, ROOT / "db/migrations/0041_prod2_document_events.sql")
        print(json.dumps({**result, "provider_calls": 0, "source_calls": 0}))
        return 0
    if args.prepare_7z_downloads:
        from buenapro_worker.prod2.sevenzip_schema import prepare_schema
        with runtime(args) as (settings, _):
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
                result = prepare_schema(conn, ROOT / "db/migrations/0042_prod2_7z_downloads.sql")
        print(json.dumps({**result, "provider_calls": 0, "source_calls": 0}))
        return 0
    if args.prepare_embeddings:
        from buenapro_worker.prod2.embedding_store import prepare_schema
        with runtime(args) as (settings, _):
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                    raise RuntimeError("Another PROD2 coordinator owns the executor")
                try:
                    result = prepare_schema(conn, ROOT / "db/migrations/0043_prod2_embeddings.sql")
                finally:
                    conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")
        print(json.dumps({**result, "provider_calls": 0, "source_calls": 0}))
        return 0
    if args.keyword_status:
        from buenapro_worker.prod2.keyword_tracking import read_progress, progress_summary, completed_cache_errors
        from buenapro_worker.prod2.keyword_history import read_history
        with runtime(args) as (settings, _):
            with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                ready = conn.execute("SELECT to_regclass('prod2_keyword_tasks') IS NOT NULL AND to_regclass('prod2_keyword_sweeps') IS NOT NULL AS ready").fetchone()["ready"]
                sweep = conn.execute("SELECT frozen_scope FROM prod2_keyword_sweeps WHERE sweep_id=%s", (args.sweep_id,)).fetchone() if ready else None
                result = {"sweep_id": args.sweep_id, "scope": sweep["frozen_scope"] if sweep else None,
                    "tracking_schema_ready": ready,
                    "keywords": read_progress(conn, args.sweep_id, requested["keywords"]) if ready else progress_summary([], requested["keywords"]),
                    "historical_keywords": read_history(conn, requested["keywords"]),
                    "cache_errors": completed_cache_errors(conn, args.sweep_id, args.keyword_cache_folder) if ready else []}
                conn.rollback()
        print(json.dumps({**result, "source_calls": 0, "provider_calls": 0, "database_writes": 0}, ensure_ascii=False))
        return 0
    # Stop between units; an in-flight paid response is saved before exit.
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    pacer = SharedPacer(args.request_interval)
    if args.preflight:
        _, observed_years = resolve_years(requested, pacer)
        print(json.dumps({"status": "native_http_access_ok", "available_years": observed_years,
                          "provider_calls": 0, "database_writes": 0}))
        return 0
    if args.folder.is_symlink() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", args.folder.name):
        raise ValueError("Use a safe private run folder name")
    folder = args.folder.resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    jev_key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY")
    if not jev_key:
        raise ValueError("Jev key is required in ignored configuration")
    with runtime(args) as (settings, redis_url):
        resolve_llm1_model(requested, 1, settings.gemini_model)
        with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                application_name="buenapro-prod2:" + folder.name, connect_timeout=15,
                keepalives_idle=30, keepalives_interval=10, keepalives_count=3) as conn:
            if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                raise RuntimeError("Another PROD2 accepted coordinator owns the executor")
            try:
                from buenapro_worker.prod2.embedding_runtime import validate_frozen_config
                embedding_config = validate_frozen_config(requested)
                if embedding_config is not None:
                    from buenapro_worker.prod2.embedding_store import require_schema
                    require_schema(conn)
                    if not getattr(settings, "openai_api_key", ""):
                        raise ValueError("Embeddings requires OPENAI_API_KEY before source/provider calls")
                    if not official_links_only(requested):
                        raise ValueError("Embeddings requires temporary official-link storage")
                if requested.get("llm1_luna_fallback") and not getattr(settings, "openai_api_key", ""):
                    raise ValueError("Configured Luna backup requires OPENAI_API_KEY before starting ingestion")
                if not conn.execute("SELECT to_regclass('prod2_document_selection_cache') AS relation").fetchone()["relation"]:
                    raise RuntimeError("Apply migration0039 through the normal migration workflow first")
                if not conn.execute("SELECT to_regclass('prod2_document_events') AS relation").fetchone()["relation"]:
                    raise RuntimeError("Apply migration0041 before source/provider calls")
                if inventory_policy(requested) == POLICY_7Z:
                    from buenapro_worker.prod2.sevenzip_schema import require_schema
                    require_schema(conn)
                marker = folder / "manifest.private.json"
                frozen_manifest = read_private(marker) if marker.exists() else None
                if frozen_manifest and frozen_manifest["requested"] != requested:
                    raise ValueError("Resume requires identical flags and inventory; use a new run for changes")
                epoch_cache = epoch_scope = None
                if requested.get("keyword_tracking"):
                    from buenapro_worker.prod2.keyword_tracking import completed_cache_errors, scope_for
                    tracking = requested["keyword_tracking"]
                    epoch_cache = Path(tracking["cache_folder"]) / tracking["sweep_id"]
                    epoch_scope = scope_for(frozen_manifest or {**requested, "published_from": start, "published_to": end})
                    previous = conn.execute("SELECT frozen_scope FROM prod2_keyword_sweeps WHERE sweep_id=%s", (tracking["sweep_id"],)).fetchone()
                    if previous and previous["frozen_scope"] != epoch_scope:
                        raise ValueError("Keyword sweep dates/policy changed; use a new sweep ID")
                    cache_errors = completed_cache_errors(conn, tracking["sweep_id"], Path(tracking["cache_folder"]))
                    if cache_errors:
                        print(json.dumps({"status": "blocked", "reason": "completed_query_cache_missing_or_invalid",
                            "source_calls": 0, "provider_calls": 0, "queries": cache_errors}, ensure_ascii=False))
                        return 2
                if frozen_manifest:
                    manifest = frozen_manifest
                else:
                    # The coordinator owns the executor before any source I/O.
                    if handoff_import is not None:
                        years = handoff_import["years"]
                    elif resume_binding is not None:
                        previous = resume_binding["source_manifest"]
                        if (previous["published_from"], previous["published_to"]) != (start, end):
                            raise ValueError("Embedding fork changed source dates")
                        if previous["commercial_profiles"] != commercial_snapshot(conn):
                            raise ValueError("Embedding fork changed the paid commercial profile")
                        years = previous["resolved_years"]
                    else:
                        years, _ = resolve_years(requested, pacer, cache_folder=epoch_cache, scope=epoch_scope)
                    manifest = {**requested, "requested": requested, "run_id": folder.name, "resolved_years": years,
                        "published_from": start, "published_to": end, "profile_id": MEMBER_IDS[0],
                        "commercial_profiles": commercial_snapshot(conn), "gemini_model": settings.gemini_model,
                        "frozen_at": datetime.now(timezone.utc).isoformat()}
                    private_write(marker, manifest)
                if manifest["gemini_model"] != settings.gemini_model:
                    raise ValueError("Frozen run model changed")
                if resume_binding is not None and resume_binding["source_manifest"]["gemini_model"] != manifest["gemini_model"]:
                    raise ValueError("Embedding fork changed the reading model")
                private_write(folder / ("execution-policy-" + str(time.time_ns()) + ".private.json"),
                    {**policy, "run_id": manifest["run_id"], "manifest_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
                     "started_at": datetime.now(timezone.utc).isoformat()})
                if handoff_import is not None:
                    from buenapro_worker.prod2.handoff_guards import HandoffGuards
                    HANDOFF_GUARD = HandoffGuards(folder, binding=manifest["handoff_selection"],
                        max_candidates=args.max_candidates, max_new_documents=args.max_new_documents, stop=STOP)
                    HANDOFF_GUARD.check_audit(audit_summary(folder))
                redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
                try:
                    queue_id = staging_identity(manifest)
                    staging = RedisStage(redis_client, queue_id)
                    if resume_binding is not None:
                        from buenapro_worker.prod2.embedding_resume import seed_resume
                        seed_resume(redis_client, staging, resume_binding, manifest, folder)
                    staging.recover()
                    total_seen = staging.stats()["seen"]
                    after_stage = queue_checkpoint(epoch_cache / "queues", queue_id, total_seen) if epoch_cache else None
                    accepted_ids = [row["convocation_id"] for row in conn.execute("""SELECT convocation_id FROM
                        prod2_accepted_run_members WHERE run_id=%s AND accepted AND opportunity_id IS NOT NULL""", (manifest["run_id"],)).fetchall()]
                    if handoff_import is not None:
                        accepted_ids = sorted(set(accepted_ids) | {value["convocation_id"]
                            for path in folder.glob("*/outcome.private.json")
                            if (value := read_private(path)).get("accepted") is True})
                    costs = audit_summary(folder)
                    limits = RunLimits(**policy["effective_limits"],
                        model_workers=args.model_workers, accepted_ids=accepted_ids,
                        seen=staged_this_run(folder, queue_id, total_seen, tracked=bool(manifest.get("keyword_tracking"))),
                        after_stage=after_stage,
                        jev_calls=costs["jev_calls"], llm1_calls=costs["llm1_calls"],
                        max_embedding_calls=None if policy["all_results"] else args.max_embedding_calls,
                        embedding_calls=costs.get("embedding_calls", 0))
                    if HANDOFF_GUARD is not None:
                        limits.handoff_guard = HANDOFF_GUARD
                    began = time.monotonic()
                    tracker = None
                    if manifest.get("keyword_tracking") and resume_binding is None:
                        from buenapro_worker.prod2.keyword_tracking import KeywordTracker, seed_queries
                        tracking = manifest["keyword_tracking"]
                        seed_queries(conn, tracking["sweep_id"], manifest, planned_queries(manifest))
                        tracker = KeywordTracker(lambda: psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                            application_name="buenapro-prod2-search:" + manifest["run_id"], connect_timeout=15,
                            keepalives_idle=30, keepalives_interval=10, keepalives_count=3),
                            tracking["sweep_id"], manifest["run_id"], Path(tracking["cache_folder"]))
                    try:
                        if handoff_import is not None:
                            # Freeze every selected payload once. Candidate caps
                            # govern processing admissions, not evidence import.
                            staging.stage_many([(str(item["row"]["convocation_id"]), item)
                                for item in handoff_import["items"]])
                            coverage, proofs = handoff_import["coverage"], handoff_import["scope_proofs"]
                        elif resume_binding is not None:
                            coverage, proofs = resume_binding["coverage"], resume_binding["scope_proofs"]
                        else:
                            coverage, proofs = run_sweeps(manifest, folder, pacer, staging, limits, tracker=tracker)
                    finally:
                        if tracker:
                            tracker.close()
                    print(json.dumps({"phase": "search_finished", "queries_total": len(coverage),
                        "queries_complete": sum(q.get("coverage_complete", False) for q in coverage),
                        "redis": staging.stats(), "execution_policy": policy}), flush=True)
                    if embedding_config is not None and not STOP.is_set() and handoff_import is None:
                        from buenapro_worker.prod2.embedding_queries import prepare_query_vectors
                        warmup = prepare_query_vectors(embedding_config, settings, conn, folder, limits,
                            stop_requested=STOP.is_set)
                        print(json.dumps({"phase": "embedding_queries_ready",
                            **{key: warmup[key] for key in ("cached_count", "new_token_count", "known_cost_usd", "pg_verified")}}), flush=True)
                    # Separate phases: no recovery while consumers are live. Atomic next() shares work.
                    with ThreadPoolExecutor(max_workers=args.processing_workers) as pool:
                        workers = [pool.submit(consume, settings, staging, manifest, folder, pacer, limits, jev_key)
                                   for _ in range(args.processing_workers)]
                        wait_consumers(workers, HANDOFF_GUARD)
                    staging.recover()
                    outcomes = [read_private(path) for path in sorted(folder.glob("*/outcome.private.json"))]
                    report = {"run_id": manifest["run_id"], "pipeline": manifest["pipeline"], "terminal_stage": "llm1",
                        "execution_policy": policy,
                        "embedding_dispatch_stop_requested": EMBEDDING_DISPATCH_STOP.is_set(),
                        "processed": len(outcomes), "accepted": sum(row["accepted"] for row in outcomes),
                        "llm1_complete": sum(row["status"] == "llm1_complete" for row in outcomes),
                        "accepted_unread": sum(row["status"] == "accepted_unread" for row in outcomes),
                        "requires_review": sum(row.get("requires_human_review", row["status"] == "needs_review") for row in outcomes),
                        "redis": staging.stats(), "costs": audit_summary(folder), "coverage": coverage,
                        "source_coverage_complete": all(query["coverage_complete"] for query in coverage),
                        "queries_total": len(coverage), "queries_complete": sum(query["coverage_complete"] for query in coverage),
                        "queries_skipped_verified_zero": sum(query.get("coverage_kind") == "verified_empty_scope_observed" for query in coverage),
                        "scope_proofs": proofs,
                        "elapsed_this_execution_seconds": time.monotonic() - began,
                        "search_workers": args.search_workers, "processing_workers": args.processing_workers,
                        "model_workers": args.model_workers, "records": outcomes}
                    recovery_context = runtime_recovery_report_context(args, folder)
                    if recovery_context is not None:
                        report["runtime_recovery"] = recovery_context
                    if resume_binding is not None:
                        report["source_resume"] = {"policy": resume_binding["version"],
                            "source_folder": resume_binding["source_folder"],
                            "source_report_sha256": resume_binding["report_sha256"],
                            "handled_preserved": resume_binding["handled_count"],
                            "source_queries_repeated": 0, "handled_paid_reads_repeated": 0}
                    if HANDOFF_GUARD is not None:
                        report["handoff_guards"] = HANDOFF_GUARD.report()
                        report["handoff_selection"] = manifest["handoff_selection"]
                        report["source_queries_repeated"] = 0
                        report["source_query_counter_scope"] = "global_mapping_sweep_only"
                        report["native_ficha_reentry_may_search"] = True
                        report["execution_status"] = (
                            "attention_required" if HANDOFF_GUARD.marker.exists()
                            else "budget_deferred" if report["redis"]["queued"] or report["redis"]["pending"]
                            else "complete_with_reviews" if report["requires_review"] else "complete")
                    if official_links_only(manifest):
                        latest_checks = {}
                        for path in sorted(folder.glob("*/temporary-cleanup-check-*.private.json")):
                            latest_checks[path.parent] = read_private(path)
                        cleanup_reports = list(latest_checks.values())
                        cleanup_history = [read_private(path) for path in folder.glob("*/temporary-cleanup.private.json")]
                        report["temporary_storage"] = {"policy": OFFICIAL_LINKS_POLICY,
                            "r2_uploads": 0, "cases_checked": len(cleanup_reports),
                            "deleted_bytes_total": sum(r.get("cumulative_deleted_bytes", 0) for r in cleanup_history),
                            "cases_retained": sum(r.get("status") != "complete" for r in cleanup_reports)}
                    if manifest.get("business_plan"):
                        report["business_batches"] = report_business_batches(manifest, coverage, outcomes)
                    if manifest.get("keyword_tracking"):
                        from buenapro_worker.prod2.keyword_tracking import read_progress
                        report["keyword_progress"] = read_progress(conn, manifest["keyword_tracking"]["sweep_id"], manifest["keywords"])
                        report["lifecycle_counts"] = {state: sum(row.get("temporal_state") == state for row in outcomes)
                            for state in sorted({row["temporal_state"] for row in outcomes if row.get("temporal_state")})}
                    private_write(folder / ("report-" + str(time.time_ns()) + ".private.json"), report)
                    print(json.dumps({key: value for key, value in report.items() if key not in {"records", "coverage", "scope_proofs"}}, ensure_ascii=False), flush=True)
                    return EGRESS_EXIT_CODE if EGRESS_DOWN.is_set() else completion_exit_code(report, policy)
                finally:
                    redis_client.close()
            finally:
                # A broken connection releases ownership at backend cleanup;
                # issuing an unlock on it would hide the original failure.
                if not conn.closed and not conn.broken:
                    conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if egress_failure(error):
            print(json.dumps({"status": "egress_unavailable", **safe_error_facts(error)}), file=sys.stderr)
            raise SystemExit(EGRESS_EXIT_CODE)
        print(json.dumps({"status": "failed", **safe_error_facts(error)}), file=sys.stderr)
        raise SystemExit(1)
