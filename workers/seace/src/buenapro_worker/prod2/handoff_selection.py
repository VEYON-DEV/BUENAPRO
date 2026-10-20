"""Offline, SHA-bound import of explicitly selected mapped IDs.

A map is metadata evidence, not an embedding resume or payment authorization.
This adapter never searches, downloads, connects to a database or changes a map.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .batch_plan import digest
from .discovery import validate_capture
from .discovery_registry import normalize_entry

VERSION = "prod2_handoff_selection_v1"


class HandoffSelectionError(ValueError):
    """Bounded machine-readable integrity failure without source contents."""


def _fail(code):
    raise HandoffSelectionError(code)


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        _fail("invalid_sha256")
    return value


def file_sha(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        _fail("unsafe_or_missing_input")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        _fail("unsafe_or_missing_input")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if expected is not None and actual != _sha(expected):
        _fail("input_sha256_mismatch")
    try:
        value = json.loads(data, parse_constant=lambda _: _fail("nonfinite_json"))
    except (ValueError, UnicodeError):
        _fail("invalid_input_json")
    if not isinstance(value, dict):
        _fail("invalid_input_object")
    return value, actual


def _unique(values, key):
    if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
        _fail("invalid_input_records")
    result = {}
    for value in values:
        identity = value.get(key)
        if type(identity) is not int or identity <= 0 or identity in result:
            _fail("duplicate_or_invalid_identity")
        result[identity] = value
    return result


def runtime_bindings(root):
    """Bind code and prompt/schema resources without changing paid fingerprints."""
    root = Path(root).resolve()
    paths = [root / "scripts/run_prod2_selected.py", root / "scripts/run_prod2_accepted.py"]
    paths += sorted((root / "workers/seace/src/buenapro_worker").rglob("*.py"))
    paths += sorted((root / "workers/seace/src/buenapro_worker").rglob("*.json"))
    paths += sorted((root / "prompts").rglob("*.txt"))
    paths += sorted((root / "prompts").rglob("*.md"))
    paths += sorted((root / "workers/seace/prompts").rglob("*.txt"))
    paths += sorted((root / "workers/seace/prompts").rglob("*.md"))
    return {str(path.relative_to(root)): file_sha(path) for path in paths}


def load_selection(handoff_path, selection_path, selection_sha256, *, root):
    """Return frozen binding and native staging items, preserving selection order.

    The inventory's selected cache reference is checked against both the raw
    native page and the registry observation. All associations remain evidence;
    one exact leaf query/page is used to reacquire a fresh official ficha.
    """
    root = Path(root).resolve()
    if Path(handoff_path).is_symlink() or Path(selection_path).is_symlink():
        _fail("unsafe_or_missing_input")
    handoff_path, selection_path = Path(handoff_path).resolve(), Path(selection_path).resolve()
    selection, selection_sha = _read(selection_path, selection_sha256)
    required = {"version", "scope_id", "mapping_run_id", "report_sha256", "handoff_sha256",
                "inventory_sha256", "inventory_path", "candidates"}
    if (not required <= set(selection) or set(selection) - required - {"selection_review"}
            or selection.get("version") != VERSION
            or not isinstance(selection["scope_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", selection["scope_id"])):
        _fail("invalid_selection_contract")
    handoff, handoff_sha = _read(handoff_path, selection["handoff_sha256"])
    report_path = handoff_path.parent / "report.private.json"
    report, report_sha = _read(report_path, selection["report_sha256"])
    inventory_path = Path(selection["inventory_path"])
    if not inventory_path.is_absolute():
        inventory_path = root / inventory_path
    inventory_path = inventory_path.resolve()
    inventory, inventory_sha = _read(inventory_path, selection["inventory_sha256"])
    run_id = selection["mapping_run_id"]
    metadata = report.get("metadata", {})
    if (not isinstance(run_id, str) or run_id != report.get("run_id")
            or run_id != handoff.get("run_id") or run_id != inventory.get("run_id")
            or run_id != metadata.get("run_id")
            or report.get("status") != "complete" or report.get("mode") != "metadata_only"
            or report.get("source_coverage_complete") is not True
            or handoff.get("source_coverage_complete") is not True
            or report.get("plan_sha256") != handoff.get("plan_sha256")
            or report.get("plan_sha256") != inventory.get("plan_sha256")
            or report.get("model_calls") != 0 or report.get("document_downloads") != 0):
        _fail("mapping_not_complete_or_identity_changed")
    _sha(report["plan_sha256"])
    coverage = report.get("coverage")
    if (not isinstance(coverage, list) or not coverage
            or len(coverage) != report.get("queries_planned")
            or len(coverage) != report.get("queries_complete")
            or any(not isinstance(q, dict) or q.get("coverage_complete") is not True
                   or q.get("errors") for q in coverage)):
        _fail("mapping_coverage_invalid")
    source_manifest = metadata.get("manifest")
    if not isinstance(source_manifest, dict) or digest(source_manifest) != metadata.get("manifest_hash"):
        _fail("mapping_manifest_hash_mismatch")
    input_bindings = inventory.get("input_bindings", {})
    for path, sha in ((handoff_path, handoff_sha), (report_path, report_sha)):
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            relative = str(path)
        if input_bindings.get(relative) != sha:
            _fail("inventory_source_binding_mismatch")
    members = _unique(metadata.get("members"), "convocation_id")
    records = _unique(inventory.get("records"), "convocation_id")
    chosen = _unique(selection["candidates"], "convocation_id")
    if not chosen:
        _fail("empty_selection")
    allowed = handoff.get("candidate_ids_for_review")
    if (not isinstance(allowed, list) or any(type(v) is not int for v in allowed)
            or len(set(allowed)) != len(allowed) or not set(chosen) <= set(allowed)):
        _fail("selection_outside_handoff")
    observations = metadata.get("observations", [])
    if not isinstance(observations, list):
        _fail("invalid_observations")
    cache_refs = inventory.get("cache_refs")
    if not isinstance(cache_refs, list):
        _fail("invalid_cache_references")
    cache = {}
    items = []
    for conv, selected in chosen.items():
        if set(selected) != {"convocation_id", "procedure_id", "listing_content_hash", "title_sha256"}:
            _fail("invalid_selected_identity_fields")
        member, record = members.get(conv), records.get(conv)
        if not member or not record or member.get("observed_hash_count") != 1:
            _fail("missing_or_changed_selected_listing")
        row = member.get("latest_listing_json", {})
        if (type(selected.get("procedure_id")) is not int
                or selected["procedure_id"] != member.get("procedure_id")
                or selected["procedure_id"] != row.get("procedure_id")
                or selected["procedure_id"] != record.get("procedure_id")
                or row.get("convocation_id") != conv
                or selected["listing_content_hash"] != member.get("latest_content_hash")
                or selected["listing_content_hash"] != record.get("listing_content_hash")
                or digest(row) != _sha(selected["listing_content_hash"])
                or row.get("description") != record.get("title")
                or hashlib.sha256(row["description"].encode()).hexdigest() != _sha(selected["title_sha256"])
                or selected["title_sha256"] != record.get("title_sha256")):
            _fail("selected_listing_identity_or_hash_mismatch")
        evidence = record.get("evidence", {})
        ref_index = evidence.get("cache_ref")
        if type(ref_index) is not int or not 0 <= ref_index < len(cache_refs):
            _fail("invalid_selected_cache_reference")
        ref = cache_refs[ref_index]
        if ref_index not in cache:
            path = Path(ref["path"])
            if not path.is_absolute():
                path = root / path
            saved, _ = _read(path, ref["sha256"])
            captured = validate_capture(saved["capture"], now=datetime.now(timezone.utc))
            if (saved.get("version") != "prod2_keyword_sweep_v1"
                    or saved.get("page_hash") != captured.page_hash
                    or ref.get("page_hash") != captured.page_hash
                    or ref.get("query") != captured.query
                    or ref.get("page_number") != captured.page["number"]
                    or ref.get("observed_at") != captured.observed_at.isoformat()):
                _fail("selected_cache_capture_mismatch")
            cache[ref_index] = captured
        captured = cache[ref_index]
        row_index = evidence.get("row_index")
        if type(row_index) is not int or not 0 <= row_index < len(captured.rows):
            _fail("invalid_selected_cache_row")
        native_item = {"row": captured.rows[row_index], "query": captured.query,
                       "page_number": captured.page["number"], "observed_at": captured.observed_at.isoformat()}
        normalized = normalize_entry(native_item)
        associations = [v for v in observations if v.get("convocation_id") == conv]
        if (normalized["row"] != row or normalized["content_hash"] != selected["listing_content_hash"]
                or evidence.get("observed_query_hash") != captured.query_hash
                or not any(v.get("content_hash") == normalized["content_hash"]
                           and v.get("listing_json") == row
                           and v.get("observed_query_hash") == captured.query_hash
                           and v.get("observed_query") == captured.query
                           and v.get("query_hash") == evidence.get("root_query_hash")
                           and v.get("observed_at") == native_item["observed_at"]
                           and v.get("page_number") == native_item["page_number"] for v in associations)):
            _fail("selected_observation_provenance_mismatch")
        items.append({**native_item, "handoff_provenance": {"version": VERSION,
            "mapping_run_id": run_id, "selection_sha256": selection_sha,
            "listing_content_hash": selected["listing_content_hash"], "title_sha256": selected["title_sha256"],
            "first_classification": member["first_classification"],
            "cache_sha256": ref["sha256"], "page_hash": captured.page_hash,
            "root_query_hash": evidence["root_query_hash"], "associations": associations}})
    binding = {"version": VERSION, "scope_id": selection["scope_id"], "mapping_run_id": run_id,
        "selection_path": str(selection_path), "selection_sha256": selection_sha,
        "handoff_path": str(handoff_path), "handoff_sha256": handoff_sha,
        "report_sha256": report_sha, "inventory_path": str(inventory_path), "inventory_sha256": inventory_sha,
        "mapping_plan_sha256": report["plan_sha256"], "mapping_manifest_sha256": metadata["manifest_hash"],
        "candidate_ids": list(chosen), "items_sha256": digest(items),
        "runtime_bindings": runtime_bindings(root)}
    return {"binding": binding, "items": items,
        "years": sorted({v["query"]["year"] for v in items}),
        "keywords": list(dict.fromkeys(v["query"]["keyword"] for v in items)),
        "published_from": source_manifest["published_from"], "published_to": source_manifest["published_to"],
        "coverage": [{"coverage_complete": True, "coverage_kind": "sha_verified_handoff_selection",
            "selected_ids": list(chosen), "source_queries_repeated": 0,
            "source_query_counter_scope": "global_mapping_sweep_only",
            "native_ficha_reentry_may_search": True, "mapping_plan_sha256": report["plan_sha256"]}],
        "scope_proofs": []}
