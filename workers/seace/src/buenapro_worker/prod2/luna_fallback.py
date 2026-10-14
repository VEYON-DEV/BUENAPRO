"""Close only the current, owned Gemini MAX_TOKENS attempt before Luna.

This helper never invokes a provider or authorizes replaying an old failure. The
caller must pass the exception and outcome obtained from its current extractor;
directory scanning cannot establish a current attempt. ``gemini_extraction`` is
the legacy compatibility name of the LLM1 ledger, not a provider assertion.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re

from buenapro_worker.extraction.gemini import (
    MODEL_PRICES_USD_PER_MILLION, TruncatedResponseError, estimate_cost,
)
from buenapro_worker.normalization.facets import canonical_hash
from .batch_runner import private_write, read_private
from .luna_policy import LUNA_REQUEST_POLICY, LUNA_SELECTED_TRANSPORT_POLICY
from .pdf_preparation import SELECTED_DOCUMENTS_POLICY
from .repository import _id, _json, _sha, _usage, _uuid, hash_request
from .selected_extractor import SELECTED_TRANSPORT_POLICY

LUNA_FALLBACK_KEY = "llm1_luna_fallback"
LUNA_FALLBACK_POLICY = "prod2_luna_max_tokens_fallback_v1"
LUNA_MODEL = LUNA_REQUEST_POLICY["model"]
LUNA_REASONING_EFFORT = LUNA_REQUEST_POLICY["reasoning_effort"]
LUNA_TRANSPORT_POLICY = LUNA_SELECTED_TRANSPORT_POLICY
EXTRACTION_POLICY = "prod2_document_extraction_v1"
_ATTEMPT = re.compile(r"gemini-selected-[0-9a-f]{32}")


class LunaFallbackError(ValueError):
    """Current payment evidence is not exact; retain inputs and stop."""


def default_luna_fallback_config() -> dict:
    return {"policy_version": LUNA_FALLBACK_POLICY, "provider": "openai",
        "model": LUNA_MODEL, "reasoning_effort": LUNA_REASONING_EFFORT,
        "max_output_tokens": LUNA_REQUEST_POLICY["max_output_tokens"],
        "timeout_ms": 300000, "max_attempts": 1,
        "trigger": "gemini_max_tokens_known_usage"}


def validate_luna_fallback_config(value) -> dict:
    """Validate a present frozen policy; callers preserve absent legacy fields."""
    expected = default_luna_fallback_config()
    if (not isinstance(value, dict) or value != expected
            or any(type(value.get(key)) is not int for key in
                ("max_output_tokens", "timeout_ms", "max_attempts"))):
        raise LunaFallbackError("Unsupported frozen Luna fallback configuration")
    return deepcopy(value)


def _known_usage(value):
    usage = value.get("usage")
    input_tokens, output_tokens, cost = (value.get(key) for key in
        ("input_tokens", "output_tokens", "cost_usd"))
    if (value.get("provider_usage_known") is not True
            or value.get("model") not in MODEL_PRICES_USD_PER_MILLION
            or value.get("cost_is_estimate") is not True
            or value.get("output_tokens_include_thinking") is not True
            or type(input_tokens) is not int or input_tokens < 0
            or type(output_tokens) is not int or output_tokens < 0
            or type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0
            or not isinstance(usage, dict) or usage.get("prompt_token_count") != input_tokens
            or type(usage.get("candidates_token_count")) is not int
            or usage["candidates_token_count"] < 0
            or (usage.get("thoughts_token_count") is not None and (
                type(usage["thoughts_token_count"]) is not int or usage["thoughts_token_count"] < 0))
            or output_tokens != usage["candidates_token_count"] + (usage.get("thoughts_token_count") or 0)):
        raise LunaFallbackError("MAX_TOKENS requires exact known provider usage")
    expected = estimate_cost(value["model"], input_tokens, output_tokens)
    if not math.isclose(cost, expected, rel_tol=1e-9, abs_tol=1e-12):
        raise LunaFallbackError("MAX_TOKENS cost differs from its configured Gemini model")
    _usage(input_tokens, output_tokens, cost)


def _read_owned(folder, name):
    path = folder / name
    if path.is_symlink() or not path.is_file():
        raise LunaFallbackError("Current MAX_TOKENS audit is unavailable or linked")
    return read_private(path)


def validated_failure_receipt(claim, failure) -> bool:
    """Local cleanup proof for a terminal failed claim; never a payment permit.

    PostgreSQL readback and the original attempt/remote-cleanup audit remain
    required by the outer storage verifier. A failure is not an extraction.
    """
    try:
        if "generation_limits_audit_policy" in failure or "legacy_generation_limits_policy" in failure:
            if (failure.get("generation_limits_audit_policy") != "limits_not_recorded"
                    or failure.get("legacy_generation_limits_policy") != "legacy_selected_defaults_absent"
                    or "max_output_tokens" in failure or "timeout_ms" in failure):
                return False
        if (not isinstance(claim, dict) or not isinstance(failure, dict)
                or claim.get("status") != "claimed"
                or failure.get("policy_version") != LUNA_FALLBACK_POLICY
                or failure.get("status") != "failed" or failure.get("provider") != "gemini"
                or failure.get("reconciled") is not True
                or failure.get("kind") != "gemini_extraction"
                or failure.get("failure_reason") != "MAX_TOKENS"
                or failure.get("finish_reason") != "MAX_TOKENS"
                or failure.get("finish_reasons") != ["MAX_TOKENS"]
                or failure.get("model_call_started") is not True
                or type(failure.get("remote_cleanup_pending")) is not int
                or failure["remote_cleanup_pending"] != 0
                or failure.get("remote_cleanup_unknown") is not False
                or failure.get("remote_files_pending") != []
                or failure.get("remote_cleanup_receipt_source") not in {"cleanup", "outcome"}
                or not _ATTEMPT.fullmatch(failure.get("source_attempt_id", ""))
                or failure.get("attempt_id") != failure.get("source_attempt_id")
                or failure.get("document_scope") != SELECTED_DOCUMENTS_POLICY
                or failure.get("transport_policy") != SELECTED_TRANSPORT_POLICY
                or _id(failure.get("evaluation_id")) != _id(claim.get("id"))
                or _uuid(failure.get("claim_token")) != _uuid(claim.get("claim_token"))
                or _sha(failure.get("evaluation_key")) != _sha(claim.get("evaluation_key"))):
            return False
        for key in ("request_hash", "prompt_hash", "analysis_sha256", "document_set_hash",
                    "source_document_manifest_hash", "attempt_receipt_hash",
                    "started_receipt_hash", "response_receipt_hash", "remote_cleanup_receipt_hash"):
            _sha(failure.get(key))
        _id(failure.get("convocation_id"))
        _uuid(failure.get("profile_id"))
        expected = hash_request({"convocation": failure["convocation_id"], "kind": "gemini_extraction",
            "request_hash": failure["request_hash"], "model": failure["model"],
            "policy_version": EXTRACTION_POLICY, "profile_id": failure["profile_id"]})
        if expected != failure["evaluation_key"]:
            return False
        _known_usage(failure)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def close_current_gemini_max_tokens(conn, folder, *, convocation_id, profile_id,
        request_hash, evaluation_key, model, prompt_hash, analysis_sha256,
        document_set_hash, document_set_identity, selection_context, error, attempt,
        request_inputs, llm1_input=None, legacy_generation_limits_policy=None) -> dict:
    """Record the current owned failure and its charge, without issuing Luna.

    ``paid_llm1`` may have just marked this same claim uncertain while propagating
    the exception. Only its exact received MAX_TOKENS response can close that
    claim. Any other pending/running/uncertain LLM1 claim blocks the fallback.
    """
    # Explicit reconciliation only. Old missing audit facts stay absent; this
    # does not infer provider settings or permit another generation/fallback.
    if (legacy_generation_limits_policy is not None
            and legacy_generation_limits_policy != "legacy_selected_defaults_absent"):
        raise LunaFallbackError("Unsupported legacy generation-limit audit policy")
    legacy_bounds = legacy_generation_limits_policy is not None
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise LunaFallbackError("Current fallback requires a private case directory")
    convocation_id, profile_id = _id(convocation_id), _uuid(profile_id)
    for value in (request_hash, evaluation_key, prompt_hash, analysis_sha256, document_set_hash):
        _sha(value)
    context = request_inputs.get("document_context") if isinstance(request_inputs, dict) else None
    if (type(error) is not TruncatedResponseError or not isinstance(attempt, dict)
            or not isinstance(context, dict)
            or canonical_hash(request_inputs) != request_hash
            or request_inputs.get("model") != model
            or request_inputs.get("prompt_hash") != prompt_hash
            or request_inputs.get("analysis_sha") != analysis_sha256
            or request_inputs.get("selected_transport_policy") != SELECTED_TRANSPORT_POLICY
            or context.get("document_scope") != SELECTED_DOCUMENTS_POLICY
            or context.get("document_set_identity") != document_set_identity
            or context.get("document_set_hash") != document_set_hash
            or canonical_hash(document_set_identity) != document_set_hash
            or context.get("selection_context") != selection_context
            or context.get("llm1_input") != llm1_input
            or attempt.get("status") != "response_parse_failure"
            or attempt.get("error_type") != "TruncatedResponseError"
            or attempt.get("model_call_started") is not True
            or attempt.get("finish_reason") != "MAX_TOKENS"
            or attempt.get("finish_reasons") != ["MAX_TOKENS"]):
        raise LunaFallbackError("Fallback does not bind the current exact MAX_TOKENS request")
    attempt_id = attempt.get("attempt_id", "")
    if not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
        raise LunaFallbackError("Current MAX_TOKENS attempt identity is invalid")
    _sha(request_inputs.get("original_sha"))
    if (not isinstance(request_inputs.get("manifest"), list) or not request_inputs["manifest"]
            or (legacy_bounds and any(key in attempt for key in ("max_output_tokens", "timeout_ms")))
            or (not legacy_bounds and (type(attempt.get("max_output_tokens")) is not int
                or attempt["max_output_tokens"] not in {24576, 65536}
                or type(attempt.get("timeout_ms")) is not int
                or attempt["timeout_ms"] not in {180000, 300000}))):
        raise LunaFallbackError("Current MAX_TOKENS source or generation bounds are incomplete")
    expected = {"attempt_id": attempt_id, "model": model, "prompt_hash": prompt_hash,
        "analysis_sha256": analysis_sha256, "document_set_hash": document_set_hash,
        "document_set_identity": document_set_identity, "selection_context": selection_context,
        "document_scope": SELECTED_DOCUMENTS_POLICY, "transport_policy": SELECTED_TRANSPORT_POLICY}
    if llm1_input is not None:
        expected["llm1_input"] = llm1_input
    started = _read_owned(folder, attempt_id + ".started.private.json")
    response = _read_owned(folder, attempt_id + ".response.private.json")
    outcome = _read_owned(folder, attempt_id + ".outcome.private.json")
    if legacy_bounds and any(key in receipt for receipt in (started, response, outcome)
                             for key in ("max_output_tokens", "timeout_ms")):
        raise LunaFallbackError("Legacy generation bounds must be absent from all three receipts")
    if (outcome != attempt or started.get("status") != "generation_reserved"
            or response.get("status") != "response_received"
            or any(any(receipt.get(key) != value for key, value in expected.items())
                for receipt in (started, response, outcome))
            or any(receipt.get("llm1_input") != llm1_input for receipt in (started, response, outcome))
            or any(response.get(key) != outcome.get(key) for key in
                ("usage", "input_tokens", "output_tokens", "cost_usd", "finish_reason", "finish_reasons"))
            or any(started.get(key) != outcome.get(key) or response.get(key) != outcome.get(key)
                for key in ("max_output_tokens", "timeout_ms"))):
        raise LunaFallbackError("Current MAX_TOKENS audit receipts differ")
    _known_usage(response)
    _known_usage(outcome)
    cleanup_name = attempt_id + ".cleanup.private.json"
    cleanup_exists = (folder / cleanup_name).exists()
    cleanup = _read_owned(folder, cleanup_name) if cleanup_exists else outcome
    if (cleanup.get("attempt_id") != attempt_id
            or type(cleanup.get("remote_cleanup_pending")) is not int
            or cleanup["remote_cleanup_pending"] != 0
            or cleanup.get("remote_cleanup_unknown") is not False
            or cleanup.get("remote_files_pending") != []):
        raise LunaFallbackError("Current Gemini remote cleanup is pending or unknown")
    stem = folder / ("gemini_extraction-" + request_hash)
    claim = _read_owned(folder, stem.with_suffix(".claim.private.json").name)
    if stem.with_suffix(".response.private.json").exists():
        raise LunaFallbackError("A completed extraction response cannot become a failed payment")
    failure = {"policy_version": LUNA_FALLBACK_POLICY, "kind": "gemini_extraction",
        "provider": "gemini", "status": "failed", "reconciled": True, "failure_reason": "MAX_TOKENS",
        "convocation_id": convocation_id, "profile_id": profile_id,
        "evaluation_id": claim.get("id"), "claim_token": claim.get("claim_token"),
        "evaluation_key": evaluation_key, "request_hash": request_hash,
        "model": model, "prompt_hash": prompt_hash, "analysis_sha256": analysis_sha256,
        "document_set_hash": document_set_hash, "document_scope": SELECTED_DOCUMENTS_POLICY,
        "transport_policy": SELECTED_TRANSPORT_POLICY, "source_attempt_id": attempt_id, "attempt_id": attempt_id,
        "source_document_manifest_hash": canonical_hash(request_inputs["manifest"]),
        "attempt_receipt_hash": canonical_hash(outcome), "started_receipt_hash": canonical_hash(started),
        "response_receipt_hash": canonical_hash(response),
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False, "remote_files_pending": [],
        "remote_cleanup_receipt_hash": canonical_hash(cleanup),
        "remote_cleanup_receipt_source": "cleanup" if cleanup_exists else "outcome",
        **{key: outcome[key] for key in ("provider_usage_known", "model_call_started", "usage",
            "input_tokens", "output_tokens", "cost_usd", "cost_is_estimate",
            "output_tokens_include_thinking", "finish_reason", "finish_reasons", "ended_at")}}
    if legacy_bounds:
        failure.update(generation_limits_audit_policy="limits_not_recorded",
                       legacy_generation_limits_policy=legacy_generation_limits_policy)
    if not validated_failure_receipt(claim, failure):
        raise LunaFallbackError("Current failed payment does not bind its owned claim")
    path = stem.with_suffix(".failure.private.json")
    if path.exists() and _read_owned(folder, path.name) != failure:
        raise LunaFallbackError("Current failure receipt conflicts with prior evidence")
    with conn.transaction():
        row = conn.execute("SELECT * FROM prod2_evaluations WHERE id=%s FOR UPDATE",
                           (failure["evaluation_id"],)).fetchone()
        if (not row or row.get("id_convocatoria") != convocation_id
                or str(row.get("profile_id")) != profile_id or row.get("kind") != "gemini_extraction"
                or row.get("model") != model or row.get("policy_version") != EXTRACTION_POLICY
                or row.get("request_hash") != request_hash or row.get("evaluation_key") != evaluation_key
                or str(row.get("claim_token")) != failure["claim_token"]
                or row.get("status") not in {"running", "uncertain", "failed"}):
            raise LunaFallbackError("Current MAX_TOKENS has no exact owned live reservation")
        unresolved = conn.execute("""SELECT id FROM prod2_evaluations
            WHERE id_convocatoria=%s AND profile_id=%s AND kind='gemini_extraction'
              AND id<>%s AND status IN ('pending','running','uncertain') LIMIT 1 FOR UPDATE""",
            (convocation_id, profile_id, failure["evaluation_id"])).fetchone()
        if unresolved:
            raise LunaFallbackError("Another unresolved LLM1 payment blocks the current fallback")
        if row["status"] == "failed":
            if (row.get("result_json") != failure
                    or any(row.get(key) != failure[key] for key in ("input_tokens", "output_tokens"))
                    or row.get("cost_usd") is None
                    or not math.isclose(float(row["cost_usd"]), failure["cost_usd"], rel_tol=1e-6, abs_tol=5e-9)):
                raise LunaFallbackError("Failed reservation differs from the current exact audit")
        else:
            updated = conn.execute("""UPDATE prod2_evaluations SET status='failed',result_json=%s::jsonb,
                input_tokens=%s,output_tokens=%s,cost_usd=%s,completed_at=now(),updated_at=now()
                WHERE id=%s AND claim_token=%s AND status IN ('running','uncertain') RETURNING id""",
                (_json(failure), failure["input_tokens"], failure["output_tokens"], failure["cost_usd"],
                 failure["evaluation_id"], failure["claim_token"])).fetchone()
            if not updated:
                raise LunaFallbackError("Current failed payment ownership changed before reconciliation")
    # Publish only after PostgreSQL confirms the terminal failure. A crash here
    # retains local input until the exact failed row/receipt is reconciled.
    if not path.exists():
        private_write(path, failure)
    return failure
