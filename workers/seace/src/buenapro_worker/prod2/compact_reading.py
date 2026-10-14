"""Bind one new compact reading to known truncation and exact prepared bytes.

Pure local evidence/copying only. No provider, reservation or database mutation.
The old model call remains an independently charged, immutable failed attempt.
"""
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import UUID

from buenapro_worker.extraction.gemini import estimate_cost, MODEL_PRICES_USD_PER_MILLION
from buenapro_worker.normalization.facets import canonical_hash
from .batch_runner import read_private, private_write
from .model_routing import resolve_llm1_model
from .pdf_preparation import (
    SELECTED_DOCUMENTS_POLICY, PdfPreparationError, _candidates, _mkdir,
    _preparation_lock, _result, _source, _write,
)
from .reading_policy import COMPACT_UNIQUE_REQUIREMENTS_POLICY, reading_policy
from .selected_extractor import SELECTED_TRANSPORT_POLICY

COMPACT_SOURCE_POLICY = "prod2_compact_source_v1"
_FALLBACK_UNSET = object()
_SAME_MANIFEST = (
    "run_id", "profile_id", "gemini_model", "commercial_profiles", "published_from", "published_to",
    "cache_folder", "max_source_mib", "max_expanded_mib", "max_archive_members", "selector_high", "selector_low",
    "llm1_model_routing", "llm1_ficha_context_policy", "resolved_years", "years", "keywords", "objects",
    "selector_policy_version", "document_previews",
)


def safe_path(path):
    value = Path(path).absolute()
    if ".." in value.parts or any(p.is_symlink() for p in (value, *value.parents)):
        raise PdfPreparationError("Compact source paths must be literal and cannot contain symlinks")
    return value.resolve()


def file_sha(path):
    path = safe_path(path)
    if not path.is_file():
        raise PdfPreparationError("Compact source evidence is missing")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _read(path):
    path = safe_path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 8 * 1024 * 1024:
        raise PdfPreparationError("Compact source JSON is missing or exceeds its bound")
    value = read_private(path)
    if not isinstance(value, dict):
        raise PdfPreparationError("Compact source requires structured evidence")
    return value


def _known_max(value):
    if (value.get("provider_usage_known") is not True or value.get("finish_reason") != "MAX_TOKENS"
            or value.get("model") not in MODEL_PRICES_USD_PER_MILLION
            or type(value.get("input_tokens")) is not int or value["input_tokens"] < 0
            or type(value.get("output_tokens")) is not int or value["output_tokens"] < 0
            or isinstance(value.get("cost_usd"), bool) or not isinstance(value.get("cost_usd"), (int, float))
            or not math.isfinite(value["cost_usd"]) or value["cost_usd"] < 0):
        raise PdfPreparationError("Compact source requires a known-usage MAX_TOKENS response")
    try:
        expected = estimate_cost(value["model"], value["input_tokens"], value["output_tokens"])
    except (KeyError, ValueError):
        raise PdfPreparationError("Compact source model has no known usage tariff") from None
    if not math.isclose(value["cost_usd"], expected, rel_tol=1e-12, abs_tol=1e-12):
        raise PdfPreparationError("Compact source token usage and known cost differ")


def _generation_limits(manifest, *receipts):
    """Old absent audit fields stay absent; never manufacture provider metadata."""
    fields = {"max_output_tokens", "timeout_ms"}
    presence = [fields & set(receipt) for receipt in receipts]
    if all(not present for present in presence):
        if "llm1_generation_limits" in manifest:
            raise PdfPreparationError("Declared source generation limits are missing from its receipts")
        return "legacy_selected_defaults_absent", None, None
    if any(present != fields for present in presence):
        raise PdfPreparationError("Compact source generation limit audit is incomplete")
    expected = {"max_output_tokens": 24576, "timeout_ms": 180000}
    policy = "legacy_selected_defaults_recorded"
    if "llm1_generation_limits" in manifest:
        value = manifest["llm1_generation_limits"]
        if (not isinstance(value, dict)
                or value != {"policy_version": "prod2_explicit_extended_output_v1", "max_output_tokens": 65536, "timeout_ms": 300000}
                or any(type(value[field]) is not int for field in fields)):
            raise PdfPreparationError("Compact source generation limit policy is invalid")
        expected = {"max_output_tokens": 65536, "timeout_ms": 300000}
        policy = value["policy_version"]
    if any(type(receipt[field]) is not int or receipt[field] != expected[field]
           for receipt in receipts for field in fields):
        raise PdfPreparationError("Compact source generation limits must be exact audited integers")
    return policy, expected["max_output_tokens"], expected["timeout_ms"]


def compare_compact_prepared(previous, current):
    """Only the recognized reading policy may change; full bytes/ranges are exact."""
    old_identity, new_identity = (deepcopy(p["document_set_identity"]) for p in (previous, current))
    old_context, new_context = (identity.get("selection_context") for identity in (old_identity, new_identity))
    if reading_policy(old_context) is not None or reading_policy(new_context) != COMPACT_UNIQUE_REQUIREMENTS_POLICY:
        raise PdfPreparationError("Compact reading cannot recursively replace another compact reading")
    new_context.pop("llm1_reading_policy")
    if new_identity != old_identity:
        raise PdfPreparationError("Compact reading changed the source, selection or preparation policy")
    for prepared in (previous, current):
        identity = prepared["document_set_identity"]
        if (canonical_hash(identity) != prepared["document_set_hash"]
                or prepared["selection_hash"] != prepared["document_set_hash"]
                or identity["selection_context"] != prepared["selection_context"]
                or identity["document_set"] != prepared["document_set"]):
            raise PdfPreparationError("Compact preparation identity is inconsistent")
    exact = ("document_scope", "document_set", "source_document_manifest", "analysis_sha256", "analysis_bytes", "page_count")
    if any(previous.get(key) != current.get(key) for key in exact):
        raise PdfPreparationError("Compact reading must keep the complete prepared PDF and attribution")
    chunks = [[{k: v for k, v in part.items() if k not in {"pdf_path", "file_name"}} for part in p["chunks"]]
              for p in (previous, current)]
    if chunks[0] != chunks[1]:
        raise PdfPreparationError("Compact reading changed PDF transport bytes or page ranges")
    return True


def _source_binding(root, conv, original, original_manifest, item, fallback_model):
    manifest_path = root / "manifest.private.json"
    manifest = _read(manifest_path)
    if root == original:
        if manifest != original_manifest:
            raise PdfPreparationError("Compact original source manifest changed")
    elif (manifest.get("recovery_policy") != "prod2_accepted_selected_recovery_v1"
            or manifest.get("original_manifest_hash") != canonical_hash(original_manifest)
            or safe_path(manifest.get("original_folder", "")) != original
            or any(manifest.get(key) != original_manifest.get(key) for key in _SAME_MANIFEST)
            or manifest.get("no_discovery") is not True or manifest.get("no_redis") is not True):
        raise PdfPreparationError("Compact source does not bind the original accepted recovery context")
    if root != original and conv not in manifest.get("recovery_ids", []):
        return None
    owner = root / str(conv)
    if not owner.is_dir():
        return None
    outcome_path = owner / "outcome.private.json"
    outcome = _read(outcome_path)
    if (outcome.get("convocation_id") != conv or outcome.get("accepted") is not True
            or outcome.get("status") != "needs_review" or outcome.get("error_type") != "TruncatedResponseError"):
        return None
    matches = []
    for path in sorted(owner.glob("gemini-selected-*.outcome.private.json")):
        value = _read(path)
        if value.get("finish_reason") == "MAX_TOKENS":
            _known_max(value)
            matches.append((path, value))
    if len(matches) > 1:
        raise PdfPreparationError("Compact source has multiple truncation attempts; choose an unambiguous source folder")
    if not matches:
        return None
    attempt_path, attempt = matches[0]
    attempt_id = attempt.get("attempt_id", "")
    if (not isinstance(attempt_id, str) or not re.fullmatch(r"gemini-selected-[0-9a-f]{32}", attempt_id)
            or attempt_path.name != attempt_id + ".outcome.private.json"
            or attempt.get("status") != "response_parse_failure" or attempt.get("model_call_started") is not True
            or attempt.get("transport_policy") != SELECTED_TRANSPORT_POLICY
            or reading_policy(attempt.get("selection_context")) is not None):
        raise PdfPreparationError("Compact source is not an original paid truncation")
    started_path, response_path = (owner / (attempt_id + "." + suffix + ".private.json") for suffix in ("started", "response"))
    started, response = _read(started_path), _read(response_path)
    _known_max(response)
    limits_policy, original_maximum, original_timeout = _generation_limits(manifest, started, response, attempt)
    same = ("attempt_id", "model", "prompt_hash", "document_set_hash", "document_set_identity", "selection_context",
            "analysis_sha256", "page_count", "max_output_tokens", "timeout_ms")
    if (started.get("status") != "generation_reserved" or response.get("status") != "response_received"
            or any(started.get(key) != attempt.get(key) or response.get(key) != attempt.get(key) for key in same)
            or any(response.get(key) != attempt.get(key) for key in ("input_tokens", "output_tokens", "cost_usd"))):
        raise PdfPreparationError("Compact source start/response/failed receipts differ")
    prepared_folder = safe_path(Path(manifest["cache_folder"]) / ("selected-analysis-" + attempt["document_set_hash"]))
    pinned_path = prepared_folder / "manifest.private.json"
    pinned = _read(pinned_path)
    prepared = _result(prepared_folder, pinned, pinned["max_analysis_bytes"])
    if any(prepared.get(key) != attempt.get(key) for key in (
            "analysis_sha256", "document_set_hash", "document_set_identity", "selection_context", "page_count")):
        raise PdfPreparationError("Compact source preparation differs from the paid input")
    model, _ = resolve_llm1_model(manifest, prepared["page_count"], manifest["gemini_model"])
    if model != attempt.get("model"):
        raise PdfPreparationError("Compact source changed the frozen model")
    from .analysis import _document_input
    first = prepared["document_set"][0]
    document_context = {k: prepared[k] for k in ("document_scope", "document_set", "document_set_hash", "document_set_identity", "selection_context")}
    prompt = _document_input(SimpleNamespace(gemini_model=model, gemini_fallback_model=fallback_model), prepared["pdf_path"].read_bytes(),
        item["query"]["object_type"], first.get("archive_sha256", first["original_sha256"]),
        prepared["source_document_manifest"], document_context)
    claims = sorted(owner.glob("gemini_extraction-*.claim.private.json"))
    if len(claims) != 1:
        raise PdfPreparationError("Compact source needs one exact owner claim")
    claim_path, claim = claims[0], _read(claims[0])
    match = re.fullmatch(r"gemini_extraction-([0-9a-f]{64})\.claim\.private\.json", claim_path.name)
    if not match or claim.get("status") != "claimed" or type(claim.get("id")) is not int or claim["id"] <= 0:
        raise PdfPreparationError("Compact source claim ownership is invalid")
    try:
        if str(UUID(claim["claim_token"])) != claim["claim_token"]:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise PdfPreparationError("Compact source claim token is invalid") from None
    request_hash = match[1]
    expected_key = canonical_hash({"convocation": conv, "kind": "gemini_extraction", "request_hash": request_hash,
        "model": model, "policy_version": "prod2_document_extraction_v1", "profile_id": manifest["profile_id"]})
    if (canonical_hash(prompt) != request_hash or claim.get("evaluation_key") != expected_key
            or prompt["prompt_hash"] != attempt.get("prompt_hash")
            or (owner / ("gemini_extraction-" + request_hash + ".response.private.json")).exists()):
        raise PdfPreparationError("Compact source claim/prompt is inconsistent or already has a valid response")
    evidence = {"source_manifest": manifest_path, "source_outcome": outcome_path, "owner_claim": claim_path,
                "owner_started": started_path, "owner_response_audit": response_path, "owner_attempt": attempt_path,
                "prepared_manifest": pinned_path}
    return {"policy": COMPACT_SOURCE_POLICY, "convocation_id": conv, "source_folder": str(root),
        "original_folder": str(original), "original_manifest_hash": canonical_hash(original_manifest),
        "evidence": {key: {"path": str(safe_path(path)), "sha256": file_sha(path)} for key, path in evidence.items()},
        "prepared_folder": str(prepared_folder), "evaluation_id": claim["id"], "evaluation_key": expected_key,
        "request_hash": request_hash, "model": model, "prompt_hash": prompt["prompt_hash"],
        "fallback_model": fallback_model,
        "document_set_hash": prepared["document_set_hash"], "analysis_sha256": prepared["analysis_sha256"],
        "page_count": prepared["page_count"], "original_max_output_tokens": original_maximum,
        "original_timeout_ms": original_timeout, "original_generation_limits_policy": limits_policy,
        "original_input_tokens": attempt["input_tokens"], "original_output_tokens": attempt["output_tokens"],
        "original_cost_usd": attempt["cost_usd"]}


def compact_source_bindings(original, original_manifest, roots, ids, *, fallback_model=_FALLBACK_UNSET):
    if fallback_model is _FALLBACK_UNSET:
        from buenapro_worker.settings import Settings
        fallback_model = Settings.model_fields["gemini_fallback_model"].default
    if not isinstance(fallback_model, str) or not fallback_model.startswith("gemini-"):
        raise PdfPreparationError("Compact source request requires the exact configured fallback model")
    original, roots = safe_path(original), [safe_path(root) for root in roots]
    if not 1 <= len(roots) <= 8 or len(roots) != len(set(roots)):
        raise PdfPreparationError("Compact source roots must be one to eight distinct explicit folders")
    payloads = {str(member["candidate_id"]): member["payload"] for member in original_manifest.get("pending_snapshot", [])}
    bindings = {}
    for conv in ids:
        if str(conv) not in payloads:
            raise PdfPreparationError("Compact reading must stay inside the original frozen pending IDs")
        original_outcome = _read(original / str(conv) / "outcome.private.json")
        if original_outcome.get("accepted") is not True or original_outcome.get("status") == "llm1_complete":
            raise PdfPreparationError("Compact reading cannot replace an originally rejected or completed candidate")
        values = [_source_binding(root, conv, original, original_manifest, payloads[str(conv)], fallback_model) for root in roots]
        values = [value for value in values if value is not None]
        if len(values) != 1:
            raise PdfPreparationError("Compact reading requires exactly one explicit known truncation source per ID")
        bindings[str(conv)] = values[0]
    return bindings


def load_compact_source(binding):
    if not isinstance(binding, dict) or binding.get("policy") != COMPACT_SOURCE_POLICY:
        raise PdfPreparationError("Compact source binding policy is invalid")
    for value in binding["evidence"].values():
        if file_sha(value["path"]) != value["sha256"]:
            raise PdfPreparationError("Frozen compact source evidence changed")
    old_response = safe_path(Path(binding["source_folder"]) / str(binding["convocation_id"]) /
                             ("gemini_extraction-" + binding["request_hash"] + ".response.private.json"))
    if old_response.exists():
        raise PdfPreparationError("Frozen compact source was completed while awaiting a new reading")
    folder = safe_path(binding["prepared_folder"])
    pinned = _read(binding["evidence"]["prepared_manifest"]["path"])
    prepared = _result(folder, pinned, pinned["max_analysis_bytes"])
    if any(prepared.get(key) != binding.get(key) for key in ("document_set_hash", "analysis_sha256", "page_count")):
        raise PdfPreparationError("Frozen compact PDF or selection changed")
    return prepared


def prepare_compact_document_set(folder, candidates, files, selection_context, binding, *, cache_folder, max_source_bytes, word_conversion_policy=None):
    """Rebind exact old PDF bytes to a new reading identity; no reconversion/OCR."""
    previous = load_compact_source(binding)
    folder = safe_path(folder)
    rows = _candidates(candidates)
    new_context = deepcopy(selection_context)
    if reading_policy(new_context) != COMPACT_UNIQUE_REQUIREMENTS_POLICY:
        raise PdfPreparationError("Compact preparation requires its explicit reading policy")
    stripped = deepcopy(new_context)
    stripped.pop("llm1_reading_policy")
    identity = previous["document_set_identity"]
    if (rows != previous["document_set"] or stripped != previous["selection_context"]
            or max_source_bytes != identity["max_source_bytes"]
            or word_conversion_policy != identity.get("word_conversion_policy")):
        raise PdfPreparationError("Compact reading changed originals, document selection or conversion policy")
    unique = {}
    for row in rows:
        if row["candidate_id"] not in files:
            raise PdfPreparationError("Compact selected original is unavailable")
        content = _source(Path(files[row["candidate_id"]]), folder, row["original_sha256"], max_source_bytes)
        unique[row["original_sha256"]] = len(content)
    if sum(unique.values()) > max_source_bytes:
        raise PdfPreparationError("Compact originals exceed source byte policy")
    pinned = _read(binding["evidence"]["prepared_manifest"]["path"])
    new_identity = deepcopy(identity)
    new_identity["selection_context"] = new_context
    digest = canonical_hash(new_identity)
    new = {**pinned, "selection_context": new_context, "document_set_identity": new_identity,
           "selection_hash": digest, "document_set_hash": digest}
    cache = safe_path(cache_folder)
    _mkdir(cache)
    private = cache / ("selected-analysis-" + digest)
    _mkdir(private)
    with _preparation_lock(private):
        marker = private / "manifest.private.json"
        if marker.exists():
            if _read(marker) != new:
                raise PdfPreparationError("Pinned compact preparation changed")
        else:
            for name in ["analysis.pdf", *(part["file_name"] for part in pinned["chunks"])]:
                _write(private / name, (previous["pdf_path"].parent / name).read_bytes())
            private_write(marker, new)
        result = _result(private, new, new["max_analysis_bytes"])
        compare_compact_prepared(previous, result)
        load_compact_source(binding)  # Source evidence is checked again before it can be paid.
    return result
