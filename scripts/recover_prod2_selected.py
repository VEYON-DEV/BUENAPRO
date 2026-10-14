"""Finish explicitly accepted PROD2 members through LLM1 without discovery or Redis.

The original run and detail recovery captures remain immutable. The new private
folder owns only its enrichment attempts, paid receipts and bounded report.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row

from recover_prod2_details import member_proof
import run_prod2_selected as selected
from buenapro_worker.prod2.accepted_repository import _run_id
from buenapro_worker.prod2.analysis import jev_request_hash
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2 import document_selection
from buenapro_worker.prod2.archive_inventory import ArchiveInventoryError, inventory_policy, POLICY_7Z
from buenapro_worker.prod2 import document_previews
from buenapro_worker.prod2.document_previews import DocumentPreviewError
from buenapro_worker.prod2.pdf_preparation import PdfPreparationError
from buenapro_worker.prod2.llm1_completion import Llm1CompletionError
from buenapro_worker.prod2.documents import METADATA_URL
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2.selected_control import RunLimits, SharedPacer, LimitReached

POLICY = "prod2_accepted_selected_recovery_v1"
STOP = selected.STOP


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--original-folder", type=Path, required=True)
    p.add_argument("--detail-recovery-folder", type=Path, action="append", default=[])
    p.add_argument("--download-cache-folder", type=Path, action="append", default=[])
    p.add_argument("--prior-recovery-folder", type=Path, action="append", default=[])
    p.add_argument("--selector-policy", choices=["auto", "v2", "v3"], default="auto")
    p.add_argument("--archive-7z", action="store_true",
                   help="Explicit new recovery inventory policy for bounded 7z archives; original run stays frozen")
    p.add_argument("--preview-anchor", action="append", default=[], metavar="SHA:PAGE[,PAGE]",
                   help="Explicit SHA-bound PDF sample for recovery; up to ten sources and three global pages each")
    p.add_argument("--selector-only", action="store_true", help="One accepted member; stop after audited document selection")
    p.add_argument("--extended-llm1-output", action="store_true",
                   help="Freeze explicit 65536 output tokens and 300 s for new LLM1 calls; preserve exact caches")
    p.add_argument("--compact-unique-requirements", action="store_true",
                   help="Explicit new reading policy: unique requirements, complete PDF and unchanged schema")
    p.add_argument("--compact-source-folder", type=Path, action="append", default=[],
                   help="Explicit known MAX_TOKENS owner folder; repeat for up to eight source roots")
    p.add_argument("--word-footnotes-at-end", action="store_true",
                   help="Explicit Word pagination workaround preserving full notes at the document end")
    p.add_argument("--convocation-id", type=int, action="append", required=True)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--max-selector-calls", type=int, default=20)
    p.add_argument("--max-llm1-calls", type=int, default=10)
    p.add_argument("--max-source-requests", type=int, default=250)
    p.add_argument("--processing-workers", type=int, default=2)
    p.add_argument("--model-workers", type=int, default=2)
    p.add_argument("--request-interval", type=float, default=2)
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    p.add_argument("--dry-run", action="store_true")
    return p


def _path(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private recovery paths must not be symlinks")
    # Check ancestors as well: resolve must not disguise a symlink escape.
    if any(parent.is_symlink() for parent in path.absolute().parents):
        raise ValueError("Private recovery path has a symlink ancestor")
    return path.resolve()


def plan(args):
    anchors = document_previews.parse_preview_anchor_specs(getattr(args, "preview_anchor", []))
    compact_sources = getattr(args, "compact_source_folder", [])
    if (len(compact_sources) > 8 or (compact_sources and not getattr(args, "compact_unique_requirements", False))
            or (getattr(args, "compact_unique_requirements", False) and not compact_sources)):
        raise ValueError("Compact recovery requires one to eight explicit compact source folders")
    run_id = _run_id(args.run_id)
    ids = args.convocation_id
    if not 1 <= len(ids) <= 10 or len(set(ids)) != len(ids) or any(type(n) is not int or n <= 0 for n in ids):
        raise ValueError("Select one to ten distinct already accepted convocatorias")
    if args.selector_only and len(ids) != 1:
        raise ValueError("Selector-only recovery requires exactly one accepted member")
    if not (1 <= args.max_selector_calls <= 20 and 1 <= args.max_llm1_calls <= 10
            and 1 <= args.max_source_requests <= 500 and 1 <= args.processing_workers <= 2
            and 1 <= args.model_workers <= 2 and 2 <= args.request_interval <= 60):
        raise ValueError("Recovery workers, provider ceilings and pacing must be bounded")
    if any(len(paths) > 8 for paths in (args.detail_recovery_folder, args.download_cache_folder, args.prior_recovery_folder)):
        raise ValueError("Recovery input folders must be bounded to eight per purpose")
    original, folder = _path(args.original_folder), _path(args.folder)
    details, downloads, priors = [list(dict.fromkeys(_path(p) for p in paths))
        for paths in (args.detail_recovery_folder, args.download_cache_folder, args.prior_recovery_folder)]
    compact_roots = [_path(p) for p in compact_sources]
    if len(compact_roots) != len(set(compact_roots)):
        raise ValueError("Compact source folders must not repeat")
    sources = list(dict.fromkeys([original, *details, *downloads, *priors, *compact_roots]))
    if len(sources) > 16:
        raise ValueError("Recovery input folders must be bounded to sixteen distinct roots")
    if any(folder == source or folder.is_relative_to(source) or source.is_relative_to(folder) for source in sources):
        raise ValueError("Use a separate private recovery folder, outside all original captures")
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(sources) for b in sources[i+1:]):
        raise ValueError("Recovery input roots must not nest inside each other")
    if not original.is_dir() or any(not source.is_dir() for source in sources):
        raise ValueError("Original and detail-recovery capture folders must exist")
    if folder.exists() and (not folder.is_dir() or (any(folder.iterdir()) and not (folder / "manifest.private.json").is_file())):
        raise ValueError("New recovery folder is nonempty or is not a directory")
    old = read_private(original / "manifest.private.json")
    if old.get("run_id") != run_id or old.get("pipeline") != "prod2_selected_llm1_v1" or old.get("terminal_stage") != "llm1":
        raise ValueError("Original manifest does not bind the requested selected run")
    required = ("gemini_model", "profile_id", "commercial_profiles", "published_from", "published_to",
                "cache_folder", "max_source_mib", "max_expanded_mib", "max_archive_members",
                "selector_high", "selector_low")
    if any(field not in old for field in required):
        raise ValueError("Original manifest lacks frozen model/profile/window or source limits")
    # Inherit page routing only when it belongs to the original frozen run.
    selected.resolve_llm1_model(old, 1, old["gemini_model"])
    years = old.get("resolved_years", old.get("years"))
    if (not isinstance(years, list) or not years or len(years) != len(set(years))
            or any(type(year) is not int or not 2000 <= year <= 2100 for year in years)):
        raise ValueError("Original manifest lacks the frozen resolved native exercise years")
    if not (16 <= old["max_source_mib"] <= 128 and old["max_source_mib"] <= old["max_expanded_mib"] <= 1024
            and 1 <= old["max_archive_members"] <= 5000 and 0 <= old["selector_low"] < old["selector_high"] <= 100):
        raise ValueError("Original source/selector limits are invalid")
    existing = read_private(folder / "manifest.private.json") if (folder / "manifest.private.json").exists() else None
    policies = {"v2": document_selection.DOCUMENT_SELECTION_POLICY_V2,
                "v3": getattr(document_selection, "DOCUMENT_SELECTION_POLICY_V3", None)}
    policy = (existing.get("selector_policy_version") if existing else policies["v3"] or policies["v2"]) \
        if args.selector_policy == "auto" else policies[args.selector_policy]
    if policy is None or policy not in policies.values():
        raise ValueError("Requested document selector policy is not available")
    # Keep the old model/profile/context; this new policy supplies real previews.
    manifest = {**old, "recovery_policy": POLICY, "original_manifest_hash": hash_request(old),
        "original_folder": str(original), "detail_recovery_folders": [str(p) for p in details],
        "recovery_folder": str(folder), "recovery_ids": ids, "resolved_years": years,
        "selector_policy_version": policy,
        "document_previews": True, "processing_workers": args.processing_workers,
        "model_workers": args.model_workers, "request_interval": args.request_interval,
        "max_jev_calls": args.max_selector_calls, "max_llm1_calls": args.max_llm1_calls,
        "max_source_requests": args.max_source_requests, "no_discovery": True, "no_redis": True}
    if downloads:
        manifest["download_cache_folders"] = [str(p) for p in downloads]
    if priors:
        manifest["prior_recovery_folders"] = [str(p) for p in priors]
        manifest["prior_authorized_ids"] = validate_priors(priors, manifest, original)
    if args.selector_only:
        manifest.update(selection_only=True, terminal_stage="document_selection")
    if anchors:
        manifest["document_preview_anchors"] = {"policy_version": document_previews.ANCHORED_PREVIEW_POLICY,
            "pages_by_source_sha256": anchors}
    document_previews.preview_anchor_mapping(manifest)
    from buenapro_worker.prod2.reading_policy import reading_policy, COMPACT_UNIQUE_REQUIREMENTS_POLICY
    if getattr(args, "compact_unique_requirements", False):
        manifest["llm1_reading_policy"] = COMPACT_UNIQUE_REQUIREMENTS_POLICY
        from buenapro_worker.prod2.compact_reading import compact_source_bindings
        manifest["compact_source_folders"] = [str(root) for root in compact_roots]
        from buenapro_worker.settings import Settings
        fallback_model = Settings(_env_file=ROOT / ".env.local").gemini_fallback_model
        manifest["compact_sources"] = compact_source_bindings(original, old, compact_roots, ids, fallback_model=fallback_model)
    reading_policy(manifest)
    if getattr(args, "extended_llm1_output", False):
        manifest["llm1_generation_limits"] = {"policy_version": "prod2_explicit_extended_output_v1",
                                               "max_output_tokens": 65536, "timeout_ms": 300000}
    selected.llm1_generation_options(manifest)
    if getattr(args, "word_footnotes_at_end", False):
        manifest["word_conversion_policy"] = "word_footnotes_at_end_v1"
    if "word_conversion_policy" in manifest and manifest["word_conversion_policy"] != "word_footnotes_at_end_v1":
        raise ValueError("Invalid frozen Word conversion policy")
    if getattr(args, "archive_7z", False):
        manifest["archive_inventory_policy"] = POLICY_7Z
    inventory_policy(manifest)
    if existing is not None and existing != manifest:
        raise ValueError("Pinned recovery inputs changed; use a new private folder")
    return manifest


def validate_priors(priors, manifest, original):
    """Prior payment evidence cannot enlarge the accepted processing scope."""
    reports = sorted(original.glob("report-*.private.json"))
    if not reports:
        raise ValueError("Prior recovery auditing requires an original accepted report")
    original_report = read_private(reports[-1])
    if original_report.get("run_id") != manifest["run_id"]:
        raise ValueError("Original accepted report belongs to another run")
    authorized = {row["convocation_id"] for row in original_report.get("records", [])
                  if row.get("accepted") is True}
    ids = set()
    same = ("original_manifest_hash", "run_id", "profile_id", "gemini_model", "commercial_profiles",
            "published_from", "published_to", "resolved_years", "max_source_mib", "max_expanded_mib", "max_archive_members",
            "llm1_model_routing")
    for prior in priors:
        path = prior / "manifest.private.json"
        if path.is_symlink():
            raise ValueError("Prior recovery manifest must not be a symlink")
        value = read_private(path)
        prior_ids = value.get("recovery_ids")
        if (value.get("recovery_policy") != POLICY or any(value.get(k) != manifest.get(k) for k in same)
                or not isinstance(prior_ids, list) or not 1 <= len(prior_ids) <= 10
                or any(type(i) is not int or i <= 0 for i in prior_ids)
                or len(prior_ids) != len(set(prior_ids)) or not set(prior_ids).issubset(authorized)):
            raise ValueError("Prior recovery does not bind the same authorized original accepted context")
        ids.update(prior_ids)
    return sorted(ids)


def combined_audit(folder, priors=()):
    """Count unique actual attempts, never aggregate a previous report again."""
    jev, model_attempts = {}, {}
    roots = list(dict.fromkeys(_path(path) for path in [folder, *priors]))
    for root in roots:
        for path in root.rglob("jev-attempt-*.private.json"):
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("Payment audit receipt must not be a symlink")
            if not path.name.endswith((".started.private.json", ".outcome.private.json")):
                continue
            receipt = read_private(path)
            identity = receipt.get("attempt_id", path.name)
            part = "outcome" if path.name.endswith(".outcome.private.json") else "started"
            previous = jev.setdefault(identity, {}).get(part)
            if previous is not None and previous != receipt:
                raise ValueError("The same paid attempt has conflicting receipts")
            jev[identity][part] = receipt
        paths = list(root.rglob("gemini-selected-*.private.json")) + list(root.rglob("openai-selected-*.private.json"))
        for path in paths:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("Payment audit receipt must not be a symlink")
            if not path.name.endswith((".started.private.json", ".response.private.json", ".outcome.private.json")):
                continue
            receipt = read_private(path)
            identity, part = receipt["attempt_id"], path.name.split(".")[-3]
            previous = model_attempts.setdefault(identity, {}).get(part)
            if previous is not None and previous != receipt:
                raise ValueError("The same paid attempt has conflicting receipts")
            model_attempts[identity][part] = receipt
    records = [parts.get("outcome", parts.get("started")) for parts in jev.values()]
    for identity, parts in model_attempts.items():
        outcome, response, started = (parts.get(part, {}) for part in ("outcome", "response", "started"))
        if not started and not response and not outcome.get("model_call_started"):
            continue
        records.append({**(outcome or response or started),
            "provider": "openai" if identity.startswith("openai-selected-") else "gemini",
            "usage_known": response.get("provider_usage_known", outcome.get("provider_usage_known", False)),
            "cost_usd": response.get("cost_usd", outcome.get("cost_usd"))})
    return {"attempts": len(records), "jev_calls": sum(r.get("provider") == "jev" for r in records),
        "llm1_calls": sum(r.get("provider") in {"gemini", "openai"} for r in records),
        "openai_calls": sum(r.get("provider") == "openai" for r in records),
        "known_cost_usd": sum(r.get("cost_usd") or 0 for r in records),
        "usage_unknown_attempts": sum(not r.get("usage_known", False) for r in records),
        "elapsed_seconds": sum(r.get("elapsed_seconds") or 0 for r in records)}


def cached_previews(roots, private):
    """Copy only self-verified current samples; extraction checks actual source SHA."""
    count = 0
    for root in roots:
        parent = root / "text-previews"
        if not parent.exists():
            continue
        if parent.is_symlink():
            raise ValueError("Preview cache root must not be a symlink")
        paths = sorted(parent.glob("*/document-preview-cache/*.private.json"))
        if len(paths) > 256:
            raise ValueError("Prior preview cache exceeds the bounded candidate policy")
        for path in paths:
            if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)) or path.stat().st_size > 65536:
                raise ValueError("Prior preview cache paths/bytes are unsafe")
            value = read_private(path)
            if value.get("preview_policy") not in {document_previews.PREVIEW_POLICY, document_previews.ANCHORED_PREVIEW_POLICY}:
                continue  # A changed sample policy must regenerate its local evidence.
            maximum, ocr_pages = value.get("max_characters"), value.get("max_ocr_pages")
            if (type(maximum) is not int or not 200 <= maximum <= 4000
                    or type(ocr_pages) is not int or not 0 <= ocr_pages <= 3
                    or value.get("extension") not in {".pdf", ".doc", ".docx"}):
                raise DocumentPreviewError("Prior preview sampling options are invalid")
            options = {}
            if value["preview_policy"] == document_previews.ANCHORED_PREVIEW_POLICY:
                options = {"anchor_source_sha256": value.get("anchor_source_sha256"),
                    "anchor_pages": value.get("anchor_pages")}
            document_previews._validate(value, value.get("source_sha256"), maximum, ocr_pages, **options)
            identity = {field: value[field] for field in ("source_sha256", "preview_policy", "extension",
                                                        "max_characters", "max_ocr_pages")}
            if options:
                identity.update(options)
            if path.name != hash_request(identity) + ".private.json":
                raise DocumentPreviewError("Prior preview filename differs from its sample identity")
            relative = path.relative_to(root)
            frozen(private / relative, value)
            count += 1
    return count


def safe_error_reason(error):
    if not isinstance(error, (document_selection.DocumentSelectionError, DocumentPreviewError,
                              PdfPreparationError, ArchiveInventoryError, Llm1CompletionError)):
        return None
    try:
        return document_selection._text(str(error), limit=2000)[:500]
    except ValueError:
        return "Structured document stage validation failed"


def frozen(path, value):
    if path.is_symlink():
        raise ValueError("Private receipt may not be a symlink")
    if path.exists():
        if read_private(path) != value:
            raise ValueError("Pinned recovery inputs changed")
    else:
        private_write(path, value)


def validate_capture(capture, proof):
    if not isinstance(capture, dict) or not isinstance(capture.get("detail"), dict):
        raise ValueError("Expected a structured frozen ficha capture")
    observed = datetime.fromisoformat(capture["observed_at"])
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Ficha observation must have a timezone")
    if "proof_hash" in capture and capture["proof_hash"] != hash_request(proof):
        raise ValueError("Recovered ficha proof differs from the accepted member")
    detail, row = capture["detail"], proof["row"]
    for official, accepted in (("convocatoria_id", "convocation_id"), ("procedure_id", "procedure_id"),
                              ("nomenclature", "nomenclature"), ("entity", "buyer_name"),
                              ("object", "object_label"), ("description", "description")):
        if detail.get(official) != row.get(accepted):
            raise ValueError("Frozen ficha identity differs from the accepted source observation")
    if detail.get("seace_version") != "3" or detail.get("document_inventory_complete") is not True:
        raise ValueError("Recovery requires the complete SEACE3 official document inventory")
    if not isinstance(detail.get("documents"), list) or not isinstance(detail.get("schedule"), list):
        raise ValueError("Frozen ficha has no structured documents/schedule")
    return capture


def _copy_exact(source, target, sha):
    if source.is_symlink() or target.is_symlink():
        raise ValueError("Original document symlinks are forbidden")
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != sha:
        raise ValueError("Cached original SHA changed")
    if target.exists():
        if target.read_bytes() != content:
            raise ValueError("Pinned cached original changed")
        return
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def prepare_member(proof, manifest, private):
    """Copy only a bound ficha and SHA-verified official originals; no HTTP/DB."""
    private = _path(private)
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    frozen(private / "accepted-proof.private.json", proof)
    if (proof.get("run_id") != manifest["run_id"]
            or jev_request_hash(proof["row"]["description"], commercial_profiles=manifest["commercial_profiles"]) != proof["request_hash"]
            or any(proof["query"].get(field) != manifest[field] for field in ("published_from", "published_to"))
            or proof["query"].get("object_type") not in {"good", "service"}
            or proof["query"].get("keyword") not in manifest["keywords"]
            or proof["query"].get("year") not in manifest["resolved_years"]):
        raise ValueError("Accepted decision/query does not bind the original commercial search manifest")
    conv = proof["convocation_id"]
    source_roots = [Path(p) for p in manifest["detail_recovery_folders"]] + [Path(manifest["original_folder"])]
    capture_path = next((root / str(conv) / "detail.private.json" for root in source_roots
                         if (root / str(conv) / "detail.private.json").exists()), None)
    if capture_path is None or capture_path.is_symlink() or capture_path.parent.is_symlink():
        raise ValueError("No trusted frozen ficha for the explicitly accepted member")
    capture = validate_capture(read_private(capture_path), proof)
    # get_detail reads exactly observed_at/detail; keep proof_hash if present.
    frozen(private / "detail.private.json", capture)
    frozen(private / "input-provenance.private.json", {"accepted_proof_hash": hash_request(proof),
        "capture_hash": hash_request(capture), "capture_path": str(capture_path),
        "original_manifest_hash": manifest["original_manifest_hash"]})
    cached = []
    seen = set()
    archive_policy = inventory_policy(manifest)
    for doc in capture["detail"]["documents"]:
        code, suffix = doc["uuid"], Path(doc["filename"]).suffix.lower()
        if str(UUID(code)) != code or code in seen:
            raise ValueError("Frozen official document resources must have unique canonical UUIDs")
        seen.add(code)
        if suffix not in {".pdf", ".zip", ".rar", ".doc", ".docx"} and not (
                suffix == ".7z" and archive_policy == POLICY_7Z):
            continue
        roots = list(dict.fromkeys([Path(p) / str(conv) for p in manifest.get("download_cache_folders", [])]
                                  + [Path(manifest["original_folder"]) / str(conv)]))
        copied = False
        for root in roots:
            receipt = root / (code + ".download.private.json")
            if not receipt.exists():
                continue
            if root.is_symlink() or receipt.is_symlink():
                raise ValueError("Original cache receipts must not be symlinks")
            facts = read_private(receipt)
            original = root / (code + suffix)
            expected_format = {".pdf": "pdf", ".zip": "zip", ".rar": "rar", ".doc": "ole",
                               ".docx": "zip", ".7z": "7z"}[suffix]
            if (facts.get("document_uuid") != code or facts.get("source_url") != METADATA_URL + "?id=" + code
                    or facts.get("format") != expected_format or type(facts.get("bytes")) is not int
                    or not 0 < facts["bytes"] <= manifest["max_source_mib"] * 1024**2
                    or original.is_symlink() or original.stat().st_size != facts["bytes"]):
                raise ValueError("Cached original receipt does not bind its official UUID/format/bytes")
            _copy_exact(original, private / original.name, facts["sha256"])
            frozen(private / receipt.name, facts)
            copied = True
        if copied:
            cached.append(code)
    roots = [Path(p) / str(conv) for p in manifest.get("download_cache_folders", [])]
    cached_previews(roots, private)
    return {"row": proof["row"], "query": proof["query"], "observed_at": capture["observed_at"],
            "cached_original_ids": cached}


class RecoveryPacer:
    """Shared HTTP-download budget; no source search/ficha requests are needed."""
    def __init__(self, maximum, interval):
        self.maximum, self.requests = maximum, 0
        self.lock = threading.Lock()
        self.pacer = SharedPacer(interval)

    def __call__(self, request=None):
        with self.lock:
            if STOP.is_set() or self.requests >= self.maximum:
                STOP.set()
                raise LimitReached("Accepted-recovery source request ceiling reached")
            self.pacer(request)
            self.requests += 1


def recover_member(settings, proof, manifest, private, pacer, limits, jev_key):
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    outcome_path = private / "outcome.private.json"
    binding = {"accepted_proof_hash": hash_request(proof), "recovery_manifest_hash": hash_request(manifest)}
    if outcome_path.exists():
        previous = read_private(outcome_path)
        if any(previous.get(key) != value for key, value in binding.items()):
            raise ValueError("Recovery outcome has different accepted inputs")
        if previous.get("status") == ("selection_complete" if manifest.get("selection_only") else "llm1_complete"):
            return previous
        # Never silently destroy the evidence of an incomplete prior attempt.
        frozen(private / ("outcome-attempt-" + str(time.time_ns()) + ".private.json"), previous)
        outcome_path.unlink()
    record = {"convocation_id": proof["convocation_id"], "description": proof["row"]["description"],
        "affinity_index": proof["affinity_index"], "accepted": True, "status": "needs_review", **binding}
    try:
        item = prepare_member(proof, manifest, private)
        if STOP.is_set():
            raise LimitReached("Recovery stopped before enrichment")
        with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                application_name="buenapro-prod2-selected-recovery-member", connect_timeout=15,
                keepalives_idle=30, keepalives_interval=10, keepalives_count=3) as conn:
            record.update(selected.enrich(conn, settings, private, item, manifest, pacer, limits, jev_key))
    except Exception as error:
        record.update(status="needs_review", requires_human_review=True, error_type=type(error).__name__)
        reason = safe_error_reason(error)
        if reason:
            record["error_reason"] = reason
    private_write(outcome_path, record)
    print(json.dumps({key: record.get(key) for key in ("convocation_id", "affinity_index", "status", "error_type", "error_reason")}), flush=True)
    return record


def main():
    args = parser().parse_args()
    manifest = plan(args)
    if args.dry_run:
        print(json.dumps({"policy": POLICY, "run_id": args.run_id, "ids": args.convocation_id,
            "terminal_stage": manifest["terminal_stage"], "selector_policy_version": manifest["selector_policy_version"],
            "document_previews": True, "search_calls": 0, "affinity_calls": 0, "redis_calls": 0,
            "source_calls": 0, "provider_calls": 0, "max_selector_calls": args.max_selector_calls,
            **({"archive_inventory_policy": manifest["archive_inventory_policy"]} if "archive_inventory_policy" in manifest else {}),
            "max_llm1_calls": args.max_llm1_calls}, ensure_ascii=False))
        return 0
    os.umask(0o077)
    STOP.clear()
    folder = Path(manifest["recovery_folder"])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    frozen(folder / "manifest.private.json", manifest)
    priors = manifest.get("prior_recovery_folders", [])
    costs = combined_audit(folder, priors)
    limits = RunLimits(max_accepted=len(args.convocation_id), max_candidates=len(args.convocation_id),
        max_jev_calls=args.max_selector_calls, max_llm1_calls=args.max_llm1_calls,
        model_workers=args.model_workers, accepted_ids=args.convocation_id,
        jev_calls=costs["jev_calls"], llm1_calls=costs["llm1_calls"])
    pacer = RecoveryPacer(args.max_source_requests, args.request_interval)
    started = time.monotonic()
    prior_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in prior_handlers:
        signal.signal(sig, lambda *_: STOP.set())
    try:
        with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            settings = settings.model_copy(update={"gemini_model": manifest["gemini_model"]})
            jev_key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY")
            if not jev_key:
                raise ValueError("Local Jev configuration is missing")
            with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                    application_name="buenapro-prod2-selected-recovery", connect_timeout=15,
                    keepalives_idle=30, keepalives_interval=10, keepalives_count=3) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                    raise RuntimeError("Another PROD2 coordinator owns the executor")
                try:
                    for name in ("prod2_document_selection_cache", "prod2_document_events"):
                        if not conn.execute("SELECT to_regclass(%s) AS relation", (name,)).fetchone()["relation"]:
                            raise RuntimeError("Required selector/publication schema is missing")
                    if inventory_policy(manifest) == POLICY_7Z:
                        from buenapro_worker.prod2.sevenzip_schema import require_schema
                        require_schema(conn)
                    proof_ids = list(dict.fromkeys([*args.convocation_id, *manifest.get("prior_authorized_ids", [])]))
                    bound = {conv: member_proof(conn, args.run_id, conv) for conv in proof_ids}
                    proofs = [bound[conv] for conv in args.convocation_id]
                    frozen(folder / "members.private.json", proofs)
                    with ThreadPoolExecutor(max_workers=args.processing_workers) as pool:
                        futures = [pool.submit(recover_member, settings, proof, manifest,
                            folder / str(proof["convocation_id"]), pacer, limits, jev_key) for proof in proofs]
                        records = [future.result() for future in futures]
                    costs = combined_audit(folder, priors)
                    report = {"policy": POLICY, "run_id": args.run_id, "terminal_stage": manifest["terminal_stage"],
                        "accepted": len(proofs), "llm1_complete": sum(r["status"] == "llm1_complete" for r in records),
                        "selection_complete": sum(r["status"] == "selection_complete" for r in records),
                        "records": records, "costs": costs, "local_costs": combined_audit(folder),
                        "prior_recovery_folders": priors, "search_calls": 0, "affinity_calls": 0,
                        "redis_calls": 0, "source_requests_this_execution": pacer.requests,
                        "processing_workers": args.processing_workers, "model_workers": args.model_workers,
                        "stopped": STOP.is_set(), "elapsed_seconds": round(time.monotonic() - started, 3),
                        "completed_at": datetime.now(timezone.utc).isoformat()}
                    private_write(folder / ("report-" + str(time.time_ns()) + ".private.json"), report)
                    print(json.dumps({**report, "records": [{key: r.get(key) for key in (
                        "convocation_id", "affinity_index", "status", "error_type", "error_reason", "analysis_page_count",
                        "extraction_id", "requires_human_review", "projection_pending")} for r in records]}, ensure_ascii=False))
                    completed = report["selection_complete"] if manifest.get("selection_only") else report["llm1_complete"]
                    return 0 if completed == len(proofs) and costs["usage_unknown_attempts"] == 0 else 2
                finally:
                    if not conn.closed and not conn.broken:
                        conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")
    finally:
        for sig, handler in prior_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
