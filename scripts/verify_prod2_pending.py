"""Read-only audit for an exact frozen PROD2 pending-candidate continuation.

Never opens Redis, calls a model, writes PostgreSQL, or downloads official files.
Only private local audit evidence is written; R2 validation uses HEAD requests.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg
from psycopg.rows import dict_row

import run_prod2_selected as selected
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.analysis import jev_request_hash
from buenapro_worker.prod2.model_routing import resolve_llm1_model
from buenapro_worker.prod2.selected_payments import audit_summary
from buenapro_worker.extraction.gemini import estimate_cost
from buenapro_worker.normalization.facets import derive_summary


class PendingVerificationError(RuntimeError):
    """A bounded, non-sensitive audit condition failed."""


def require(condition, code):
    if not condition:
        raise PendingVerificationError(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False, default=str).encode()).hexdigest()


def exact_ids(value, label):
    require(isinstance(value, list) and value and all(type(i) is int and i > 0 for i in value), label + "_invalid_ids")
    require(len(value) == len(set(value)), label + "_duplicate_ids")
    return sorted(value)


def scope(preflight_path, original_folder):
    preflight = read_private(preflight_path)
    ids = exact_ids(preflight["pending_ids"], "pending")
    prior_ids = exact_ids(preflight["done_ids"], "prior")
    require(len(ids) == 90 and len(prior_ids) == 10 and not set(ids) & set(prior_ids), "exact_90_10_scope")
    manifest_path = original_folder / "manifest.private.json"
    manifest = read_private(manifest_path)
    actual_hash = hash_request(manifest)
    require(actual_hash == preflight["original_manifest_hash"], "original_manifest_changed")
    return ids, prior_ids, manifest, actual_hash


def prior_snapshot(conn, prior_ids, prior_folder):
    """Hashes of existing paid data, never their potentially sensitive contents."""
    rows = conn.execute("SELECT * FROM prod2_document_extractions WHERE id_convocatoria=ANY(%s) ORDER BY id", (prior_ids,)).fetchall()
    current = {int(row["id_convocatoria"]): int(row["id"]) for row in rows if row["is_current"]}
    require(len(current) == 10 and set(current.values()) == set(range(31, 41)), "prior_10_current_extractions_changed")
    for conv, extraction in current.items():
        outcome = read_private(prior_folder / str(conv) / "outcome.private.json")
        require(outcome["extraction_id"] == extraction and outcome["status"] == "llm1_complete", "prior_outcome_binding")
    facets = conn.execute("SELECT * FROM prod2_requirement_facets WHERE id_convocatoria=ANY(%s) ORDER BY id", (prior_ids,)).fetchall()
    evaluations = conn.execute("SELECT * FROM prod2_evaluations WHERE id=ANY(%s) ORDER BY id", ([row["evaluation_id"] for row in rows],)).fetchall()
    participation = conn.execute("""SELECT e.id_convocatoria, o.consortium_status, o.subcontracting_status,
        o.participation_terms_json FROM prod2_document_extractions e JOIN opportunities o ON o.id=e.opportunity_id
        WHERE e.id_convocatoria=ANY(%s) AND e.is_current ORDER BY e.id_convocatoria""", (prior_ids,)).fetchall()
    receipts = {}
    for path in sorted(prior_folder.rglob("*.private.json")):
        if path.name == "manifest.private.json" or path.name == "outcome.private.json" or path.name.endswith((".response.private.json", ".claim.private.json")):
            require(not path.is_symlink(), "prior_receipt_symlink")
            receipts[str(path.relative_to(prior_folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"current_extraction_ids": current, "extractions_sha256": fingerprint(rows),
        "facets_sha256": fingerprint(facets), "evaluations_sha256": fingerprint(evaluations),
        "participation_sha256": fingerprint(participation), "private_receipts_sha256": receipts}


def counts(conn, ids):
    tables = ("prod2_discoveries", "prod2_details", "prod2_documents", "prod2_document_downloads",
        "prod2_document_events", "prod2_evaluations", "prod2_document_extractions", "prod2_requirement_facets", "prod2_match_provenance")
    result = {table: conn.execute("SELECT count(*) AS n FROM " + table + " WHERE id_convocatoria=ANY(%s)", (ids,)).fetchone()["n"] for table in tables}
    result["prod2_document_selection_cache"] = conn.execute("SELECT count(*) AS n FROM prod2_document_selection_cache WHERE convocation_id=ANY(%s)", (ids,)).fetchone()["n"]
    return result


def baseline(args):
    ids, prior_ids, original, original_hash = scope(args.preflight, args.original_folder)
    require(not args.output.exists(), "baseline_already_exists")
    with selected.runtime(args) as (settings, _redis_url):
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='30s'")
            prior = prior_snapshot(conn, prior_ids, args.prior_folder)
            table_counts = counts(conn, ids)
    result = {"audit_policy": "prod2_exact_pending90_readonly_v1", "verified_at": datetime.now(timezone.utc).isoformat(),
        "original_manifest_hash": original_hash, "original_run_id": original["run_id"], "pending_ids": ids,
        "prior_ids": prior_ids, "prior_folder": str(args.prior_folder.resolve()), "prior_snapshot": prior,
        "pending_table_counts": table_counts, "source_calls": 0, "provider_calls": 0, "db_writes": 0, "redis_calls": 0}
    private_write(args.output, result)
    print(json.dumps({"status": "baseline_verified", "pending": len(ids), "prior_current": len(prior_ids),
        "prior_extractions": "31-40", "source_calls": 0, "provider_calls": 0, "db_writes": 0, "redis_calls": 0}))
    return result


def number(value, code):
    require(type(value) in (float, int) and math.isfinite(value), code)
    return value


def near(left, right, code, *, tolerance=0.000000011):
    require(abs(float(left) - float(right)) <= tolerance, code)


def same_costs(left, right, code):
    for key in ("attempts", "jev_calls", "llm1_calls", "usage_unknown_attempts"):
        require(left[key] == right[key], code)
    near(left["known_cost_usd"], right["known_cost_usd"], code)


def validate_canonical_summary(raw, canonical, paid):
    require(canonical == derive_summary(raw), "pg_current_summary_derivation")
    changed = {key for key in set(canonical) | set(paid) if canonical.get(key) != paid.get(key)}
    require(changed.issubset({"penalidad_tope_pct"}), "pg_unexpected_canonical_summary_change")


def validate_recovery_options(manifest):
    """Extra recovery policies are explicit, validated inputs, never loose flags."""
    from buenapro_worker.prod2.document_previews import preview_anchor_mapping, DocumentPreviewError
    from buenapro_worker.local_document_conversion import WORD_FOOTNOTES_AT_END_POLICY
    from buenapro_worker.prod2.reading_policy import reading_policy
    try:
        preview_anchor_mapping(manifest)
    except DocumentPreviewError:
        require(False, "recovery_preview_anchor_policy_invalid")
    if "word_conversion_policy" in manifest:
        require(manifest["word_conversion_policy"] == WORD_FOOTNOTES_AT_END_POLICY, "recovery_word_policy_invalid")
    try:
        reading_policy(manifest)
    except ValueError:
        require(False, "recovery_reading_policy_invalid")


def validate_word_derivative(bundle, prepared, manifest):
    """Use completion's exact pure provenance contract for opt-in Word only."""
    from buenapro_worker.local_document_conversion import WORD_FOOTNOTES_AT_END_POLICY
    from buenapro_worker.prod2.llm1_completion import _selected_context, Llm1CompletionError
    declared = "word_conversion_policy" in manifest
    identity = prepared["document_set_identity"]
    require(isinstance(identity, dict) and ("word_conversion_policy" in identity) == declared,
        "recovery_word_identity_policy_binding")
    if not declared:
        return
    require(manifest["word_conversion_policy"] == identity["word_conversion_policy"] == WORD_FOOTNOTES_AT_END_POLICY,
        "recovery_word_identity_policy_binding")
    if bundle is None:  # A global cache hit is checked through its PG bundle.
        return
    require(bundle.get("document_set_identity") == identity
        and bundle.get("raw_extraction_json", {}).get("source_document_manifest") == prepared.get("source_document_manifest"),
        "recovery_word_preparation_receipt_binding")
    try:
        context = _selected_context(bundle, bundle["document_uuid"], bundle["original_sha256"])
    except (Llm1CompletionError, ValueError, KeyError, TypeError):
        require(False, "recovery_word_provenance_invalid")
    require(context["analysis_page_count"] == prepared["page_count"], "recovery_word_page_count_binding")


def validate_reading_derivative(bundle, prepared, manifest):
    """Bind an explicit reading policy to its actual complete selected context."""
    from buenapro_worker.prod2.reading_policy import reading_policy
    from buenapro_worker.prod2.llm1_completion import _selected_context, Llm1CompletionError
    try:
        policy = reading_policy(manifest)
        actual = reading_policy(prepared.get("selection_context", {}))
        require(policy == actual, "recovery_reading_policy_binding")
        if policy is None:
            return
        require(reading_policy(prepared["document_set_identity"]["selection_context"]) == policy,
            "recovery_reading_identity_binding")
    except (ValueError, KeyError, TypeError):
        require(False, "recovery_reading_policy_invalid")
    if bundle is None:
        return
    require(bundle.get("document_set_identity") == prepared["document_set_identity"]
        and bundle.get("raw_extraction_json", {}).get("source_document_manifest") == prepared.get("source_document_manifest"),
        "recovery_reading_preparation_receipt_binding")
    try:
        context = _selected_context(bundle, bundle["document_uuid"], bundle["original_sha256"])
    except (Llm1CompletionError, ValueError, KeyError, TypeError):
        require(False, "recovery_reading_provenance_invalid")
    require(context["analysis_page_count"] == prepared["page_count"], "recovery_reading_page_count_binding")


def validate_compact_new_payment(conv, bundle, manifest, *, evaluation_id=None):
    from buenapro_worker.prod2.reading_policy import reading_policy
    if reading_policy(manifest) is None:
        return
    source = manifest["compact_sources"][str(conv)]
    require(bundle["model"] == source["model"] and bundle.get("prompt_hash") != source["prompt_hash"]
        and bundle["cache_key"] != "prod2:llm1:" + source["request_hash"], "recovery_compact_reused_legacy_request")
    if evaluation_id is not None:
        require(evaluation_id != source["evaluation_id"], "recovery_compact_reused_legacy_evaluation")


def recovery_overlay(args, manifest, original_outcomes):
    """Overlay explicitly accepted recovery evidence without rewriting originals."""
    import recover_prod2_selected as recovery
    roots = [recovery._path(path) for path in getattr(args, "recovery_folder", [])]
    require(len(roots) <= 8 and len(roots) == len(set(roots)), "recovery_folder_bounds")
    original_root = recovery._path(args.folder)
    payloads = {int(member["candidate_id"]): member["payload"] for member in manifest["pending_snapshot"]}
    outcomes, folders, recovered, audit_roots, proofs = dict(original_outcomes), {i: original_root for i in original_outcomes}, [], [], {}
    same = ("run_id", "profile_id", "gemini_model", "commercial_profiles", "published_from", "published_to",
        "resolved_years", "keywords", "objects", "cache_folder", "max_source_mib", "max_expanded_mib", "max_archive_members",
        "selector_high", "selector_low", "selector_policy_version", "document_previews", "llm1_ficha_context_policy", "llm1_model_routing")
    validated = {}
    def validate(root, *, terminal):
        root = recovery._path(root)
        require(root != original_root and not root.is_relative_to(original_root) and not original_root.is_relative_to(root),
            "recovery_source_tree_overlap")
        value = read_private(root / "manifest.private.json")
        validate_recovery_options(value)
        ids = exact_ids(value.get("recovery_ids"), "recovery")
        require(len(ids) <= 10 and all(i in original_outcomes and original_outcomes[i].get("accepted") is True for i in ids),
            "recovery_ids_not_originally_accepted")
        require(value.get("recovery_policy") == recovery.POLICY and value.get("original_manifest_hash") == hash_request(manifest)
            and recovery._path(value.get("original_folder", "")) == original_root
            and recovery._path(value.get("recovery_folder", "")) == root
            and all(value.get(key) == manifest.get(key) for key in same), "recovery_frozen_context_mismatch")
        require(value.get("no_discovery") is True and value.get("no_redis") is True, "recovery_discovery_scope")
        from buenapro_worker.prod2.reading_policy import reading_policy
        compact_roots = []
        if reading_policy(value):
            from buenapro_worker.prod2.compact_reading import compact_source_bindings
            sources = value.get("compact_source_folders")
            require(isinstance(sources, list) and 1 <= len(sources) <= 8, "recovery_compact_source_roots")
            compact_roots = [recovery._path(path) for path in sources]
            require(len(compact_roots) == len(set(compact_roots)), "recovery_compact_source_roots")
            bindings = value.get("compact_sources")
            require(isinstance(bindings, dict) and set(bindings) == {str(conv) for conv in ids}
                and all(isinstance(binding, dict) and "fallback_model" in binding for binding in bindings.values()),
                "recovery_compact_source_binding_shape")
            fallbacks = [binding["fallback_model"] for binding in bindings.values()]
            require(all(type(model) is str and model.startswith("gemini-") and len(model) <= 200
                for model in fallbacks) and len(set(fallbacks)) == 1, "recovery_compact_source_fallback_metadata")
            try:
                expected = compact_source_bindings(original_root, manifest, compact_roots, ids, fallback_model=fallbacks[0])
            except (ValueError, KeyError, TypeError, OSError):
                require(False, "recovery_compact_source_evidence_invalid")
            require(value.get("compact_sources") == expected, "recovery_compact_source_binding_changed")
        else:
            require("compact_source_folders" not in value and "compact_sources" not in value, "recovery_undeclared_compact_sources")
        if terminal:
            require(value.get("terminal_stage") == "llm1" and not value.get("selection_only"), "recovery_terminal_stage")
        if root not in validated:
            require(len(validated) < 16, "recovery_audit_root_bounds")
            validated[root] = value
            audit_roots.append(root)
            for prior in value.get("prior_recovery_folders", []):
                validate(prior, terminal=False)
            for source in compact_roots:
                if source != original_root:
                    validate(source, terminal=False)  # Include the failed paid source in the cost union.
        return value, ids
    for root in roots:
        value, ids = validate(root, terminal=True)
        require(all(original_outcomes[i]["status"] not in {"llm1_complete", "rejected_below_60"} for i in ids),
            "recovery_reprocesses_finished_original")
        reports = sorted(root.glob("report-*.private.json"))
        require(bool(reports), "recovery_report_missing")
        report = read_private(reports[-1])
        require(report.get("policy") == recovery.POLICY and report.get("run_id") == manifest["run_id"]
            and report.get("terminal_stage") == "llm1", "recovery_report_context")
        require(report.get("search_calls") == report.get("affinity_calls") == report.get("redis_calls") == 0,
            "recovery_report_discovery_scope")
        local_costs = recovery.combined_audit(root)
        same_costs(local_costs, report["local_costs"], "recovery_local_cost_mismatch")
        same_costs(recovery.combined_audit(root, value.get("prior_recovery_folders", [])), report["costs"], "recovery_report_cost_mismatch")
        records = report.get("records")
        require(isinstance(records, list) and exact_ids([row.get("convocation_id") for row in records], "recovery_report") == ids,
            "recovery_report_ids")
        private_records = []
        for conv in value["recovery_ids"]:
            require(outcomes[conv]["status"] != "llm1_complete", "recovery_overwrites_successful_recovery")
            record = read_private(root / str(conv) / "outcome.private.json")
            proof = read_private(root / str(conv) / "accepted-proof.private.json")
            expected = jev_request_hash(payloads[conv]["row"]["description"], commercial_profiles=manifest["commercial_profiles"])
            require(proof.get("policy") == "prod2_accepted_detail_recovery_v1" and proof.get("run_id") == manifest["run_id"]
                and proof.get("convocation_id") == conv and proof.get("row") == payloads[conv]["row"]
                and proof.get("query") == payloads[conv]["query"] and proof.get("request_hash") == expected
                and proof.get("affinity_index") == original_outcomes[conv].get("affinity_index"), "recovery_accepted_proof_mismatch")
            require(record.get("convocation_id") == conv and record.get("accepted") is True
                and record.get("affinity_index") == proof["affinity_index"] and proof["affinity_index"] >= 60
                and record.get("accepted_proof_hash") == hash_request(proof)
                and record.get("recovery_manifest_hash") == hash_request(value)
                and record.get("status") in {"llm1_complete", "needs_review", "documents_missing", "selection_needs_review"},
                "recovery_outcome_binding")
            outcomes[conv], folders[conv] = record, root
            proofs[conv] = proof
            recovered.append(conv)
            private_records.append(record)
        require(records == private_records and report["accepted"] == len(ids)
            and report["llm1_complete"] == sum(row["status"] == "llm1_complete" for row in records), "recovery_report_outcome_binding")
    return outcomes, folders, sorted(set(recovered)), audit_roots, proofs


def failure_costs(roots):
    """Keep known truncation usage separate from unknown timeout billing."""
    parts = {}
    for root in roots:
        for path in Path(root).rglob("gemini-selected-*.private.json"):
            if not path.name.endswith((".started.private.json", ".response.private.json", ".outcome.private.json")):
                continue
            value = read_private(path)
            parts.setdefault(value["attempt_id"], {})[path.name.split(".")[-3]] = value
    failed = []
    for attempt, value in parts.items():
        outcome, response = value.get("outcome", {}), value.get("response", {})
        if not outcome.get("error_type") or outcome.get("model_call_started") is False:
            continue
        known = response.get("provider_usage_known", outcome.get("provider_usage_known", False))
        failed.append({"attempt_id": attempt, "model": outcome.get("model"), "error_type": outcome["error_type"],
            "usage_known": known, "cost_usd": response.get("cost_usd", outcome.get("cost_usd")),
            "finish_reason": response.get("finish_reason", outcome.get("finish_reason"))})
    return {"failed_model_calls": len(failed), "known_failure_calls": sum(row["usage_known"] for row in failed),
        "unknown_failure_calls": sum(not row["usage_known"] for row in failed),
        "known_failure_cost_usd": sum(row["cost_usd"] or 0 for row in failed), "records": failed}


def original_paid_inputs(original, conv, outcome, prepared, recovery_manifest=None):
    """A recovery keeps previously attempted PDF, prompt, model and selection."""
    folder = original / str(conv)
    bindings = []
    from buenapro_worker.prod2.reading_policy import reading_policy
    compact = reading_policy(recovery_manifest or {}) is not None
    if compact:
        from buenapro_worker.prod2.compact_reading import load_compact_source, compare_compact_prepared
        source = (recovery_manifest or {}).get("compact_sources", {}).get(str(conv))
        require(isinstance(source, dict) and source.get("convocation_id") == conv, "recovery_compact_source_missing")
        try:
            previous = load_compact_source(source)
            compare_compact_prepared(previous, prepared)
        except (ValueError, KeyError, TypeError, OSError):
            require(False, "recovery_compact_prepared_changed")
        require(outcome["llm1_model"] == source["model"] and prepared["page_count"] == source["page_count"],
            "recovery_compact_original_model_pages_changed")
    for path in folder.glob("gemini-selected-*.started.private.json"):
        receipt = read_private(path)
        if compact:
            old_cache = Path(source["prepared_folder"]).parent / ("selected-analysis-" + receipt["document_set_hash"])
            old_prepared = read_private(old_cache / "manifest.private.json")
            require(receipt.get("model") == outcome["llm1_model"] and all(receipt.get(key) == old_prepared.get(key) for key in
                ("page_count", "analysis_sha256", "document_set_hash", "document_set_identity", "selection_context")),
                "recovery_compact_original_paid_input_changed")
            try:
                compare_compact_prepared(old_prepared, prepared)
            except (ValueError, KeyError, TypeError):
                require(False, "recovery_compact_original_preparation_changed")
            continue  # A distinct authorized compact prompt is not the legacy paid prompt.
        expected = {"model": outcome["llm1_model"], "page_count": prepared["page_count"],
            "analysis_sha256": prepared["analysis_sha256"], "document_set_hash": prepared["document_set_hash"],
            "document_set_identity": prepared["document_set_identity"], "selection_context": prepared["selection_context"]}
        require(all(receipt.get(key) == value for key, value in expected.items()), "recovery_original_paid_input_changed")
        require(isinstance(receipt.get("prompt_hash"), str) and len(receipt["prompt_hash"]) == 64, "recovery_original_prompt_missing")
        bindings.append(receipt)
    metadata = folder / "metadata.private.json"
    if metadata.exists():
        require(outcome.get("detail_hash") == read_private(metadata)["detail_hash"], "recovery_original_detail_changed")
    selection = folder / "selection.private.json"
    if selection.exists():
        old = read_private(selection)
        from buenapro_worker.prod2.document_previews import preview_anchor_mapping
        anchored_without_paid_llm1 = bool(preview_anchor_mapping(recovery_manifest or {})) \
            and not list(folder.glob("gemini-selected-*.started.private.json"))
        if not anchored_without_paid_llm1:
            require(outcome.get("selector_request_hash") == old["request_hash"] and outcome.get("selected_ids") == old["selected_ids"],
                "recovery_original_selection_changed")
    return bindings


def retry_artifacts(args, manifest, original_outcomes, recovery_roots=()):
    """Account for explicit retries only through their immutable owner references."""
    import continue_prod2_pending as runner
    import retry_prod2_llm1 as retry
    source = runner.safe_path(args.folder)
    roots = [runner.safe_path(path) for path in getattr(args, "retry_folder", [])]
    require(len(roots) <= 90 and len(roots) == len(set(roots)), "retry_folder_bounds")
    owners = {}
    for conv in original_outcomes:
        path = source / str(conv) / retry.AUTHORIZATION_NAME
        if path.exists():
            authorization = read_private(path)
            owners[conv] = runner.safe_path(authorization["retry_folder"])
    require(set(roots) == set(owners.values()), "retry_authorization_unaccounted")
    require(len(owners) == len(roots), "retry_authorization_duplicate_root")
    summaries = []
    for root in roots:
        require(root != source and not root.is_relative_to(source) and not source.is_relative_to(root), "retry_source_tree_overlap")
        value = read_private(root / "manifest.private.json")
        conv = value.get("convocation_id")
        require(type(conv) is int and conv in original_outcomes and original_outcomes[conv].get("accepted") is True
            and original_outcomes[conv]["status"] == "needs_review" and owners.get(conv) == root, "retry_exact_accepted_scope")
        owner = source / str(conv)
        authorization = read_private(owner / retry.AUTHORIZATION_NAME)
        require(authorization == value and value.get("policy") == retry.POLICY and value.get("source_folder") == str(source)
            and value.get("retry_folder") == str(root) and value.get("source_manifest_hash") == hash_request(manifest), "retry_owner_authorization_binding")
        require(value.get("max_attempts") == 1 and type(value.get("max_output_tokens")) is int
            and value["max_output_tokens"] in {24576, 65536} and type(value.get("timeout_ms")) is int
            and value["timeout_ms"] in {180000, 300000} and value.get("global_max_llm1_calls") == min(manifest["max_llm1_calls"], 90),
            "retry_explicit_execution_controls")
        refs = value.get("recovery_audit_folders", [])
        require(isinstance(refs, list) and len(refs) <= 8, "retry_recovery_reference_bounds")
        referred = []
        for reference in refs:
            require(isinstance(reference, dict) and set(reference) == {"folder", "manifest_sha256"}, "retry_recovery_reference_shape")
            related = runner.safe_path(reference["folder"])
            require(related in recovery_roots and hashlib.sha256((related / "manifest.private.json").read_bytes()).hexdigest() == reference["manifest_sha256"],
                "retry_recovery_cost_reference_unaccounted")
            referred.append(related)
        require(len(referred) == len(set(referred)), "retry_recovery_reference_duplicate")
        request_hash = value.get("request_hash")
        require(isinstance(request_hash, str) and len(request_hash) == 64 and all(char in "0123456789abcdef" for char in request_hash), "retry_request_hash")
        claim_path = owner / ("gemini_extraction-" + request_hash + ".claim.private.json")
        claim = read_private(claim_path)
        attempts = list(owner.glob("gemini-selected-*.outcome.private.json"))
        require(len(attempts) == 1, "retry_original_attempt_count")
        old_attempt = read_private(attempts[0])
        old_starts = list(owner.glob("gemini-selected-*.started.private.json"))
        old_responses = list(owner.glob("gemini-selected-*.response.private.json"))
        require(len(old_starts) == 1 and len(old_responses) <= 1, "retry_original_receipt_count")
        pinned = Path(manifest["cache_folder"]) / ("selected-analysis-" + old_attempt["document_set_hash"]) / "manifest.private.json"
        info = read_private(pinned)
        require(value["original_outcome_sha256"] == hashlib.sha256((owner / "outcome.private.json").read_bytes()).hexdigest()
            and value["owner_claim_sha256"] == hashlib.sha256(claim_path.read_bytes()).hexdigest()
            and value["owner_attempt_sha256"] == hashlib.sha256(attempts[0].read_bytes()).hexdigest()
            and value.get("owner_started_sha256") == hashlib.sha256(old_starts[0].read_bytes()).hexdigest()
            and value.get("owner_response_audit_sha256") == (hashlib.sha256(old_responses[0].read_bytes()).hexdigest() if old_responses else None)
            and value["prepared_manifest_sha256"] == hashlib.sha256(pinned.read_bytes()).hexdigest(), "retry_original_evidence_changed")
        model, routing = resolve_llm1_model(manifest, info["page_count"], manifest["gemini_model"])
        require(value.get("evaluation_id") == claim.get("id") and value.get("evaluation_key") == claim.get("evaluation_key")
            and value.get("model") == model == old_attempt["model"] and value.get("page_count") == info["page_count"]
            and value.get("model_routing") == routing and value.get("analysis_sha256") == info["analysis_sha256"] == old_attempt["analysis_sha256"]
            and value.get("prompt_hash") == old_attempt["prompt_hash"], "retry_original_model_input_binding")
        known = old_attempt.get("provider_usage_known") is True
        require(value.get("original_usage_known") is known and (known or value.get("allow_unknown_billing") is True),
            "retry_unknown_billing_authorization")
        if known:
            require(old_attempt.get("finish_reason") == "MAX_TOKENS" and value["max_output_tokens"] == 65536, "retry_known_failure_controls")
        issued_path = root / "retry-issued.private.json"
        issued = read_private(issued_path) if issued_path.exists() else None
        costs = audit_summary(root)
        require(costs["llm1_calls"] <= 1 and costs["jev_calls"] == 0, "retry_more_than_one_generation")
        if issued:
            require(issued.get("policy") == retry.POLICY and issued.get("request_hash") == request_hash
                and issued.get("evaluation_id") == claim["id"] and issued.get("max_attempts") == 1, "retry_issued_binding")
        else:
            require(costs["attempts"] == 0, "retry_generation_without_issued_receipt")
        for path in root.glob("gemini-selected-*.started.private.json"):
            started = read_private(path)
            require(all(started.get(key) == value.get(key) for key in
                ("model", "page_count", "analysis_sha256", "prompt_hash", "max_output_tokens", "timeout_ms")), "retry_actual_generation_controls")
        result_path = root / "retry-result.private.json"
        result = read_private(result_path) if result_path.exists() else None
        status = "authorized_without_generation" if not issued else "attempt_unresolved"
        if result:
            response = read_private(root / "retry-response.private.json")
            require(response == read_private(owner / ("gemini_extraction-" + request_hash + ".response.private.json")), "retry_owned_response_binding")
            require(result.get("policy") == retry.POLICY and result.get("status") == "evaluation_reconciled"
                and result.get("convocation_id") == conv and result.get("evaluation_id") == claim["id"]
                and result.get("request_hash") == request_hash and result.get("model") == model
                and result.get("original_outcome_preserved") is True and result.get("requires_normal_recovery") is True,
                "retry_result_binding")
            require(response.get("cache_key") == "prod2:llm1:" + request_hash and response.get("model") == model
                and response.get("analysis_sha256") == value["analysis_sha256"] and response.get("prompt_hash") == value["prompt_hash"],
                "retry_response_model_input_binding")
            require(result.get("retry_response_sha256") == hashlib.sha256((root / "retry-response.private.json").read_bytes()).hexdigest(),
                "retry_response_bytes_binding")
            require(result.get("attempt_id") in {read_private(path).get("attempt_id") for path in root.glob("gemini-selected-*.started.private.json")},
                "retry_result_attempt_binding")
            require(result["input_tokens"] == response["input_tokens"] and result["output_tokens"] == response["output_tokens"], "retry_result_usage_binding")
            near(result["cost_usd"], response["cost_usd"], "retry_result_price_binding")
            near(response["cost_usd"], estimate_cost(model, response["input_tokens"], response["output_tokens"]), "retry_model_price")
            status = "evaluation_reconciled"
        elif list(root.glob("gemini-selected-*.outcome.private.json")):
            status = "attempt_failed" if any(read_private(path).get("error_type") for path in root.glob("gemini-selected-*.outcome.private.json")) \
                else "attempt_completed_unreconciled"
        summaries.append({"convocation_id": conv, "retry_folder": str(root), "evaluation_id": claim["id"],
            "status": status, "model": model, "original_usage_known": known, "new_model_calls": costs["llm1_calls"]})
    return roots, summaries


def local_run(args):
    """Verify exact scope and receipts without requesting any external service."""
    import continue_prod2_pending as runner
    ids, prior_ids, original, original_hash = scope(args.preflight, args.original_folder)
    baseline_value = read_private(args.baseline_file)
    require(baseline_value["original_manifest_hash"] == original_hash and baseline_value["pending_ids"] == ids
        and baseline_value["prior_ids"] == prior_ids, "baseline_scope_changed")
    require(args.folder is not None and not args.folder.is_symlink(), "continuation_folder_missing")
    manifest = read_private(args.folder / "manifest.private.json")
    require(exact_ids(manifest["pending_ids"], "manifest") == ids, "manifest_pending_scope_changed")
    require(exact_ids([row["convocation_id"] for row in manifest["previous_done"]], "manifest_prior") == prior_ids,
        "manifest_prior_scope_changed")
    require(manifest["original_manifest_hash"] == original_hash and manifest["original_run_id"] == original["run_id"],
        "manifest_original_binding")
    require(manifest.get("no_discovery") is True and manifest.get("discovery_sweep_calls") == 0
        and manifest.get("new_candidates") == 0, "continuation_discovery_scope")
    runner.validate_frozen(manifest, original)
    require(manifest["previous_done"] == runner.original_proofs(args.original_folder, original), "original_paid_outcomes_changed")
    reports = sorted(args.folder.glob("report-*.private.json"))
    require(bool(reports), "continuation_report_missing")
    report = read_private(reports[-1])
    outcomes = runner.read_outcomes(args.folder, manifest)
    require(set(outcomes) == set(ids), "not_all_90_have_outcomes")
    require(exact_ids(report["authorized_ids"], "report") == ids and report["processed"] == 90, "report_scope_changed")
    require(report["run_id"] == manifest["run_id"] and report["original_run_id"] == original["run_id"], "report_run_binding")
    require(report["records"] == [outcomes[i] for i in manifest["pending_ids"]], "report_outcome_binding")
    require(report["discovery_sweep_calls"] == report["new_candidates"] == report["previous_members_reprocessed"] == 0,
        "report_discovery_or_previous_reprocessing")
    require(report["queue_after"] == {"queued": 0, "pending": 0, "payloads": 0, "seen": 100},
        "report_queue_not_drained")
    actual_costs = audit_summary(args.folder)
    for key in ("attempts", "jev_calls", "llm1_calls", "usage_unknown_attempts"):
        require(actual_costs[key] == report["costs"][key], "report_provider_attempt_counts")
    near(actual_costs["known_cost_usd"], report["costs"]["known_cost_usd"], "report_provider_cost")
    complete = [i for i in ids if outcomes[i]["status"] == "llm1_complete"]
    rejected = [i for i in ids if outcomes[i]["status"] == "rejected_below_60"]
    blocked = sorted(set(ids) - set(complete) - set(rejected))
    require(report["llm1_complete"] == len(complete) and report["rejected_below_60"] == len(rejected), "report_stage_counts")
    require(not report["pending_ids"] and report["needs_review"] == len(blocked), "report_pending_counts")
    for conv, outcome in outcomes.items():
        if outcome["status"] in {"llm1_complete", "rejected_below_60"}:
            affinity = number(outcome.get("affinity_index"), "affinity_invalid")
            require(0 <= affinity <= 100 and outcome["accepted"] == (affinity >= 60), "affinity_threshold_mismatch")
    original_outcomes, original_costs = outcomes, actual_costs
    outcomes, outcome_folders, recovered_ids, recovery_roots, recovery_proofs = recovery_overlay(args, manifest, original_outcomes)
    retry_roots, retry_records = retry_artifacts(args, manifest, original_outcomes, recovery_roots)
    extra_roots = list(dict.fromkeys([*recovery_roots, *retry_roots]))
    if extra_roots:
        from recover_prod2_selected import combined_audit
        actual_costs = combined_audit(args.folder, extra_roots)
    require(actual_costs["llm1_calls"] <= min(manifest["max_llm1_calls"], 90), "original_and_recovery_llm1_budget")
    if not getattr(args, "allow_incomplete", False):
        require(actual_costs["usage_unknown_attempts"] == 0, "provider_usage_unknown")
    complete = [i for i in ids if outcomes[i]["status"] == "llm1_complete"]
    rejected = [i for i in ids if outcomes[i]["status"] == "rejected_below_60"]
    blocked = sorted(set(ids) - set(complete) - set(rejected))
    bundles, prepared, original_bindings, completion_manifests = {}, {}, {}, {}
    for conv in complete:
        outcome = outcomes[conv]
        cache = Path(manifest["cache_folder"]) / ("selected-analysis-" + outcome["document_set_hash"])
        prepared[conv] = read_private(cache / "manifest.private.json")
        info = prepared[conv]
        completion_manifests[conv] = read_private(outcome_folders[conv] / "manifest.private.json")
        validate_word_derivative(None, info, completion_manifests[conv])
        validate_reading_derivative(None, info, completion_manifests[conv])
        model, routing = resolve_llm1_model(manifest, info["page_count"], manifest["gemini_model"])
        require(outcome["analysis_page_count"] == info["page_count"] and outcome["llm1_model"] == model
            and outcome["llm1_model_routing"] == routing, "llm1_page_routing_mismatch")
        require(outcome["document_set_hash"] == info["document_set_hash"] and outcome["analysis_bytes"] == info["analysis_bytes"],
            "prepared_document_binding")
        if conv in recovered_ids:
            original_bindings[conv] = original_paid_inputs(args.folder, conv, outcome, info, completion_manifests[conv])
        responses = []
        for path in (outcome_folders[conv] / str(conv)).glob("gemini_extraction-*.response.private.json"):
            bundle = read_private(path)
            if bundle.get("document_set_hash") == outcome["document_set_hash"] and bundle.get("model") == model:
                require(bundle["cache_key"] == "prod2:llm1:" + path.name.removeprefix("gemini_extraction-").removesuffix(".response.private.json"),
                    "llm1_cache_key_binding")
                responses.append(bundle)
        # Global cache hits have no new local response; PostgreSQL verifies them.
        if responses:
            require(len(responses) == 1, "llm1_duplicate_paid_response")
            bundle = responses[0]
            require(bundle["analysis_sha256"] == info["analysis_sha256"] and bundle["document_set_identity"] == info["document_set_identity"],
                "llm1_analysis_hash_binding")
            near(bundle["cost_usd"], estimate_cost(model, bundle["input_tokens"], bundle["output_tokens"]), "llm1_price_mismatch")
            require(all(row["prompt_hash"] == bundle.get("prompt_hash") for row in original_bindings.get(conv, [])),
                "recovery_original_prompt_changed")
            bundles[conv] = bundle
            validate_word_derivative(bundle, info, completion_manifests[conv])
            validate_reading_derivative(bundle, info, completion_manifests[conv])
            validate_compact_new_payment(conv, bundle, completion_manifests[conv])
        else:
            require(outcome.get("llm1_cached") is True, "llm1_response_missing")
        source = cache / "analysis.pdf"
        if source.exists():
            require(not source.is_symlink(), "analysis_pdf_symlink")
            content = source.read_bytes()
            require(len(content) == info["analysis_bytes"] and hashlib.sha256(content).hexdigest() == info["analysis_sha256"],
                "analysis_pdf_bytes_changed")
        for path in (outcome_folders[conv] / str(conv)).glob("gemini-selected-*.outcome.private.json"):
            receipt = read_private(path)
            require((receipt.get("provider_usage_known") is True or receipt.get("model_call_started") is False
                or getattr(args, "allow_incomplete", False))
                and receipt.get("remote_cleanup_pending") == 0 and receipt.get("remote_cleanup_unknown") is False,
                "llm1_payment_or_remote_cleanup_unresolved")
    return {"ids": ids, "prior_ids": prior_ids, "original": original, "baseline": baseline_value,
        "manifest": manifest, "report": report, "outcomes": outcomes, "complete": complete,
        "rejected": rejected, "blocked": blocked, "bundles": bundles, "prepared": prepared, "costs": actual_costs,
        "original_outcomes": original_outcomes, "original_costs": original_costs, "recovered_ids": recovered_ids,
        "outcome_folders": outcome_folders, "recovery_roots": recovery_roots,
        "recovery_proofs": recovery_proofs,
        "retry_roots": retry_roots, "retry_records": retry_records,
        "original_paid_bindings": original_bindings,
        "completion_manifests": completion_manifests,
        "failure_costs": failure_costs([args.folder, *extra_roots]),
        "allow_incomplete": bool(getattr(args, "allow_incomplete", False))}


def pg_readback(conn, local, prior_folder):
    """Read exact paid bindings, affinity memberships and official metadata."""
    ids, manifest, outcomes = local["ids"], local["manifest"], local["outcomes"]
    prior = prior_snapshot(conn, local["prior_ids"], prior_folder)
    require(fingerprint(prior) == fingerprint(local["baseline"]["prior_snapshot"]), "prior_10_paid_data_changed")
    members = conn.execute("SELECT * FROM prod2_accepted_run_members WHERE run_id=%s ORDER BY convocation_id", (manifest["run_id"],)).fetchall()
    require(len(members) == 90 and {int(row["convocation_id"]) for row in members} == set(ids), "pg_90_membership_missing")
    payloads = {int(row["candidate_id"]): row["payload"] for row in manifest["pending_snapshot"]}
    for member in members:
        conv, outcome = int(member["convocation_id"]), outcomes[int(member["convocation_id"])]
        result = member["result_json"]
        expected = jev_request_hash(payloads[conv]["row"]["description"], commercial_profiles=manifest["commercial_profiles"])
        require(member["request_hash"] == expected and member["accepted"] == outcome["accepted"], "pg_jev_request_binding")
        require(result["affinity_index"] == outcome["affinity_index"] and member["accepted"] == (result["affinity_index"] >= 60),
            "pg_jev_affinity_threshold")
        require((member["opportunity_id"] is not None) == member["accepted"], "pg_rejected_canonical_promotion")
        if conv in local.get("recovery_proofs", {}):
            require(str(member["opportunity_id"]) == local["recovery_proofs"][conv]["opportunity_id"],
                "pg_recovery_accepted_proof_opportunity")
        receipt = conn.execute("SELECT status,result_json FROM prod2_jev_decision_cache WHERE cache_key=%s", (expected,)).fetchone()
        require(receipt and receipt["status"] == "done" and receipt["result_json"]["affinity_index"] == outcome["affinity_index"],
            "pg_jev_receipt_incomplete")
    rows = conn.execute("""SELECT e.*, v.status AS evaluation_status,v.request_hash AS evaluation_request_hash,
        v.model AS evaluation_model,v.input_tokens AS evaluation_input_tokens,v.output_tokens AS evaluation_output_tokens,
        v.cost_usd AS evaluation_cost_usd,v.result_json AS paid_bundle,d.storage_key,
        o.consortium_status,o.subcontracting_status,o.participation_terms_json,
        (SELECT count(*) FROM prod2_requirement_facets f WHERE f.extraction_id=e.id AND f.is_current) AS actual_facets,
        (SELECT jsonb_array_length(schedule_json) FROM prod2_details p WHERE p.id_convocatoria=e.id_convocatoria) AS schedule_count,
        (SELECT count(*) FROM prod2_documents p WHERE p.id_convocatoria=e.id_convocatoria AND p.is_listed) AS document_count,
        (SELECT count(*) FROM prod2_document_events p WHERE p.id_convocatoria=e.id_convocatoria AND p.is_listed) AS publication_count
        FROM prod2_document_extractions e JOIN prod2_evaluations v ON v.id=e.evaluation_id
        JOIN opportunities o ON o.id=e.opportunity_id JOIN prod2_document_downloads d ON d.id_convocatoria=e.id_convocatoria
        AND d.codigo_alfresco=e.codigo_alfresco AND d.sha256_original=e.sha256_original
        WHERE e.id_convocatoria=ANY(%s) AND e.is_current ORDER BY e.id_convocatoria""", (local["complete"],)).fetchall()
    require({int(row["id_convocatoria"]) for row in rows} == set(local["complete"]), "pg_current_llm1_missing")
    for row in rows:
        conv = int(row["id_convocatoria"])
        outcome, info, bundle = outcomes[conv], local["prepared"][conv], row["paid_bundle"]
        require(row["id"] == outcome["extraction_id"] and row["evaluation_status"] == "done", "pg_llm1_paid_binding")
        require(row["model"] == row["evaluation_model"] == bundle["model"] == outcome["llm1_model"], "pg_llm1_model_binding")
        require(bundle["cache_key"] == "prod2:llm1:" + row["evaluation_request_hash"], "pg_llm1_request_hash")
        require(bundle["analysis_sha256"] == info["analysis_sha256"] and bundle["document_set_hash"] == outcome["document_set_hash"],
            "pg_document_hash_binding")
        require(bundle["original_sha256"] == row["sha256_original"] and bundle["document_uuid"] == str(row["codigo_alfresco"]),
            "pg_official_original_binding")
        require(bundle["document_set_identity"] == info["document_set_identity"] and bundle["raw_extraction_json"] == row["raw_extraction_json"],
            "pg_llm1_structured_result_binding")
        validate_word_derivative(bundle, info, local.get("completion_manifests", {}).get(conv, {}))
        validate_reading_derivative(bundle, info, local.get("completion_manifests", {}).get(conv, {}))
        validate_compact_new_payment(conv, bundle, local.get("completion_manifests", {}).get(conv, {}), evaluation_id=row["evaluation_id"])
        validate_canonical_summary(row["raw_extraction_json"], row["summary_json"], bundle["summary_json"])
        require(all(prior["prompt_hash"] == bundle.get("prompt_hash") for prior in local.get("original_paid_bindings", {}).get(conv, [])),
            "pg_recovery_original_prompt_changed")
        require(row["input_tokens"] == row["evaluation_input_tokens"] == bundle["input_tokens"]
            and row["output_tokens"] == row["evaluation_output_tokens"] == bundle["output_tokens"], "pg_llm1_usage_binding")
        near(row["cost_usd"], row["evaluation_cost_usd"], "pg_llm1_paid_cost")
        near(row["cost_usd"], estimate_cost(row["model"], row["input_tokens"], row["output_tokens"]), "pg_llm1_model_price")
        require(row["actual_facets"] == row["facet_count"] == outcome["facet_count"], "pg_llm1_facets_missing")
        require(row["schedule_count"] == len(outcome["schedule"]) and row["document_count"] == len(outcome["documents"]),
            "pg_official_metadata_missing")
        require(row["requires_human_review"] is True and outcome["projection_pending"] is True
            and row["consortium_status"] == row["subcontracting_status"] == "not_identified"
            and row["participation_terms_json"]["extraction_id"] == row["id"]
            and row["participation_terms_json"]["projection_pending"] is True, "pg_review_gate_bypassed")
        require(row["storage_key"] and row["storage_key"].endswith("/" + info["analysis_sha256"] + ".pdf"), "pg_r2_analysis_binding")
        selector = conn.execute("SELECT status,policy_version FROM prod2_document_selection_cache WHERE request_hash=%s", (outcome["selector_request_hash"],)).fetchone()
        require(selector and selector["status"] == "done" and selector["policy_version"] == manifest["selector_policy_version"],
            "pg_selector_incomplete")
    table_counts = counts(conn, ids)
    require(table_counts["prod2_match_provenance"] == local["baseline"]["pending_table_counts"]["prod2_match_provenance"],
        "unexpected_llm2_or_matching")
    active = conn.execute("""SELECT
        (SELECT count(*) FROM prod2_evaluations WHERE id_convocatoria=ANY(%s) AND status='running') +
        (SELECT count(*) FROM prod2_document_selection_cache WHERE convocation_id=ANY(%s) AND status='running') +
        (SELECT count(*) FROM prod2_jev_decision_cache WHERE convocation_id=ANY(%s) AND status='running') AS n""", (ids, ids, ids)).fetchone()["n"]
    uncertain = conn.execute("""SELECT
        (SELECT count(*) FROM prod2_evaluations WHERE id_convocatoria=ANY(%s) AND status='uncertain') +
        (SELECT count(*) FROM prod2_document_selection_cache WHERE convocation_id=ANY(%s) AND status='uncertain') +
        (SELECT count(*) FROM prod2_jev_decision_cache WHERE convocation_id=ANY(%s) AND status='uncertain') AS n""", (ids, ids, ids)).fetchone()["n"]
    coordinator = conn.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
        AND classid=(hashtext('prod2_accepted_executor')::bigint & 4294967295)::oid
        AND objid=0 AND objsubid=2 AND granted) AS active""").fetchone()["active"]
    require(active == 0 and coordinator is False, "active_provider_or_coordinator")
    local["payment_ledger"] = {"running_count": active, "uncertain_count": uncertain, "coordinator_active": coordinator,
        "scope": "all_authorized_ids_including_preexisting_history"}
    return rows, table_counts


def r2_readback(rows, prepared, env_path):
    import boto3
    from botocore.config import Config
    from dotenv import dotenv_values
    env = dotenv_values(env_path)
    require(all(env.get(key) for key in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")), "r2_configuration_missing")
    client = boto3.client("s3", endpoint_url=env["R2_ENDPOINT"], aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"], region_name=env.get("R2_REGION") or "auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=15, read_timeout=30,
            retries={"max_attempts": 1}))
    try:
        for row in rows:
            info = prepared[int(row["id_convocatoria"])]
            head = client.head_object(Bucket=env["R2_BUCKET"], Key=row["storage_key"])
            require(head["ContentLength"] == info["analysis_bytes"] and head.get("Metadata", {}).get("sha256") == info["analysis_sha256"],
                "r2_analysis_missing_or_changed")
    finally:
        client.close()
    return len(rows)


def verify(args):
    require(not args.output.exists(), "verification_output_already_exists")
    local = local_run(args)
    with selected.runtime(args) as (settings, _redis_url):
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='30s'")
            rows, table_counts = pg_readback(conn, local, args.prior_folder)
    r2 = r2_readback(rows, local["prepared"], ROOT / ".env.local")
    result = {"audit_policy": "prod2_exact_pending90_readonly_v1", "verified_at": datetime.now(timezone.utc).isoformat(),
        "verified_ids": local["ids"], "previous_current_extractions_unchanged": "31-40",
        "processed": 90, "llm1_complete": len(local["complete"]), "rejected_below_60": len(local["rejected"]),
        "blocked_ids": local["blocked"], "status_counts": dict(Counter(o["status"] for o in local["outcomes"].values())),
        "complete": not local["blocked"], "pg_verified": True,
        "completed_subset_verified": True, "allow_incomplete": local["allow_incomplete"],
        "original_status_counts": dict(Counter(o["status"] for o in local["original_outcomes"].values())),
        "original_llm1_complete": sum(o["status"] == "llm1_complete" for o in local["original_outcomes"].values()),
        "recovered_ids": local["recovered_ids"], "recovered_count": len(local["recovered_ids"]),
        "retry_records": local["retry_records"], "retry_count": len(local["retry_records"]),
        "retry_model_calls": sum(row["new_model_calls"] for row in local["retry_records"]),
        "original_costs": local["original_costs"],
        "failure_costs": {key: value for key, value in local["failure_costs"].items() if key != "records"},
        "failed_attempts": local["failure_costs"]["records"],
        "r2_head_verified": r2, "models": dict(Counter(row["model"] for row in rows)),
        "reading_policies": dict(Counter(value.get("llm1_reading_policy", "legacy") for value in local["completion_manifests"].values())),
        "facets": sum(row["actual_facets"] for row in rows), "schedule_stages": sum(row["schedule_count"] for row in rows),
        "official_documents": sum(row["document_count"] for row in rows), "publication_events": sum(row["publication_count"] for row in rows),
        "analysis_pages": sum(info["page_count"] for info in local["prepared"].values()), "costs": local["costs"],
        "pending_table_counts_before": local["baseline"]["pending_table_counts"], "pending_table_counts_after": table_counts,
        "source_calls": 0, "provider_calls": 0, "db_writes": 0, "redis_calls": 0,
        "usage_unknown_attempts": local["costs"]["usage_unknown_attempts"],
        "costs_complete": local["costs"]["usage_unknown_attempts"] == 0,
        "active_payments": local["payment_ledger"]["running_count"],
        "uncertain_count": local["payment_ledger"]["uncertain_count"],
        "payment_ledger_scope": local["payment_ledger"]["scope"],
        "coordinator_active": local["payment_ledger"]["coordinator_active"],
        "records": [{"convocation_id": int(row["id_convocatoria"]), "extraction_id": row["id"], "model": row["model"],
            "schedule_count": row["schedule_count"], "document_count": row["document_count"], "facet_count": row["actual_facets"],
            "page_count": local["prepared"][int(row["id_convocatoria"])]["page_count"],
            "analysis_sha256": local["prepared"][int(row["id_convocatoria"])]["analysis_sha256"]} for row in rows]}
    private_write(args.output, result)
    print(json.dumps({key: value for key, value in result.items() if key not in ("verified_ids", "blocked_ids", "records", "recovered_ids", "failed_attempts", "retry_records",
        "pending_table_counts_before", "pending_table_counts_after")}, ensure_ascii=False))
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", action="store_true")
    p.add_argument("--allow-incomplete", action="store_true",
        help="Audit a drained 90-outcome run with unresolved billing; operational complete is separate from costs_complete")
    p.add_argument("--recovery-folder", action="append", type=Path, default=[],
        help="Overlay an exact accepted recovery within the 90 IDs; repeat in attempt order, never rewrite original outcomes")
    p.add_argument("--retry-folder", action="append", type=Path, default=[],
        help="Audit an explicit retry referenced by its original owner authorization, including its distinct provider attempt")
    p.add_argument("--original-folder", type=Path, required=True)
    p.add_argument("--folder", type=Path)
    p.add_argument("--prior-folder", type=Path, default=ROOT / "tmp/prod2-selected-recovery-v3-20261006")
    p.add_argument("--preflight", type=Path, default=ROOT / "tmp/prod2-pending90-audit-20261006/preflight.private.json")
    p.add_argument("--baseline-file", type=Path, default=ROOT / "tmp/prod2-pending90-audit-20261006/baseline.private.json")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--local", action="store_true")
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    return p


def main():
    args = parser().parse_args()
    if args.baseline:
        baseline(args)
    else:
        result = verify(args)
        if not result["complete"] or not result["costs_complete"]:
            sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "verification_failed", "error_type": type(error).__name__,
            "error_code": str(error) if isinstance(error, PendingVerificationError) else "private_audit_error"}))
        sys.exit(1)
