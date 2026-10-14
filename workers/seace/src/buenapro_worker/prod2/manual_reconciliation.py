"""Explicit terminal reconciliation of proven historical failures, never a retry.

No source/provider requests are made. Planning is offline; applying owns only
one exact evaluation row. A closed failure is not an extraction and does not
authorize another model request. Existing paid bodies must be recovered first.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re

from buenapro_worker.extraction.gemini import MODEL_PRICES_USD_PER_MILLION, estimate_cost
from buenapro_worker.normalization.facets import canonical_hash
from .batch_runner import read_private
from .luna_fallback import validated_failure_receipt
from .luna_policy import LUNA_REQUEST_POLICY, LUNA_SELECTED_TRANSPORT_POLICY
from .pdf_preparation import SELECTED_DOCUMENTS_POLICY
from .repository import _id, _json, _sha, _usage, _uuid, hash_request
from .selected_extractor import SELECTED_TRANSPORT_POLICY

POLICY = "prod2_manual_terminal_failure_v1"
EXTRACTION_POLICY = "prod2_document_extraction_v1"
_ATTEMPT = re.compile(r"(gemini|openai)-selected-[0-9a-f]{32}\Z")


class ManualReconciliationError(ValueError):
    """A historical claim is not proved terminal; preserve inputs and stop."""


def _folder(value):
    folder = Path(value).absolute()
    if folder.is_symlink() or not folder.is_dir():
        raise ManualReconciliationError("Reconciliation requires its private case directory")
    return folder


def _read(folder, name, evidence):
    path = folder / name
    if Path(name).name != name or path.is_symlink() or not path.is_file():
        raise ManualReconciliationError("Owned reconciliation receipt is missing or linked")
    content = path.read_bytes()
    if len(content) > 32 * 1024**2:
        raise ManualReconciliationError("Reconciliation receipt exceeds its byte bound")
    value = read_private(path)
    if not isinstance(value, dict):
        raise ManualReconciliationError("Reconciliation receipt must be a JSON object")
    evidence.append({"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
    return value


def _absent(folder, names):
    if any((folder / name).exists() or (folder / name).is_symlink() for name in names):
        raise ManualReconciliationError("A response or completed bundle is available; reconcile it before terminal failure")


def _cleanup(value):
    if (type(value.get("remote_cleanup_pending")) is not int or value["remote_cleanup_pending"] != 0
            or value.get("remote_cleanup_unknown") is not False or value.get("remote_files_pending") != []):
        raise ManualReconciliationError("Historical remote cleanup is pending or unknown")


def _known_usage(value):
    usage = value.get("usage")
    inp, out, cost = (value.get(key) for key in ("input_tokens", "output_tokens", "cost_usd"))
    if (value.get("provider_usage_known") is not True or value.get("model") not in MODEL_PRICES_USD_PER_MILLION
            or value.get("cost_is_estimate") is not True or value.get("output_tokens_include_thinking") is not True
            or type(inp) is not int or inp < 0 or type(out) is not int or out < 0
            or type(cost) not in (float, int) or not math.isfinite(cost) or cost < 0
            or not isinstance(usage, dict) or usage.get("prompt_token_count") != inp
            or type(usage.get("candidates_token_count")) is not int or usage["candidates_token_count"] < 0
            or (usage.get("thoughts_token_count") is not None and
                (type(usage["thoughts_token_count"]) is not int or usage["thoughts_token_count"] < 0))
            or out != usage["candidates_token_count"] + (usage.get("thoughts_token_count") or 0)):
        raise ManualReconciliationError("Historical response has unknown or contradictory usage")
    _usage(inp, out, cost)
    if not math.isclose(cost, estimate_cost(value["model"], inp, out), rel_tol=1e-9, abs_tol=1e-12):
        raise ManualReconciliationError("Historical cost differs from the exact model token usage")


def _binding(inputs):
    context = inputs.get("document_context")
    manifest = inputs.get("manifest")
    if (not isinstance(context, dict) or context.get("document_scope") != SELECTED_DOCUMENTS_POLICY
            or not isinstance(context.get("selection_context"), dict)
            or not isinstance(context.get("document_set_identity"), dict)
            or not isinstance(context.get("document_set"), list) or not context["document_set"]
            or not isinstance(manifest, list) or not manifest
            or context["document_set_identity"].get("document_scope") != SELECTED_DOCUMENTS_POLICY
            or context["document_set_identity"].get("document_set") != context["document_set"]
            or context["document_set_identity"].get("selection_context") != context["selection_context"]
            or canonical_hash(context["document_set_identity"]) != context.get("document_set_hash")):
        raise ManualReconciliationError("Request has no exact selected document identity")
    for key in ("analysis_sha", "original_sha", "prompt_hash"):
        _sha(inputs.get(key))
    _sha(context["document_set_hash"])
    return {"prompt_hash": inputs["prompt_hash"], "analysis_sha256": inputs["analysis_sha"],
        "document_set_hash": context["document_set_hash"], "document_scope": SELECTED_DOCUMENTS_POLICY,
        "selection_context_hash": canonical_hash(context["selection_context"]),
        "document_set_identity_hash": canonical_hash(context["document_set_identity"]),
        "llm1_input_hash": canonical_hash(context.get("llm1_input")),
        "source_document_manifest_hash": canonical_hash(manifest),
        "transport_policy": inputs.get("selected_transport_policy")}


def _match(value, expected):
    for key in ("prompt_hash", "analysis_sha256", "document_set_hash", "document_scope", "transport_policy"):
        if value.get(key) != expected[key]:
            raise ManualReconciliationError("Historical attempt differs from its exact request binding")
    for key in ("selection_context", "document_set_identity", "llm1_input"):
        if canonical_hash(value.get(key)) != expected[key + "_hash"]:
            raise ManualReconciliationError("Historical selection or input proof differs from its request")


def plan_terminal_failure(folder, convocation_id, profile_id, request_inputs, *,
                          expected_evaluation_id, attempt_id, parent_failure=None):
    """Build metadata proof for one STOP parse failure or unissued Luna attempt.

    The caller constructs ``request_inputs`` from verified pinned PDF/input
    bytes with ``analysis._document_input``. This function binds that exact hash
    to immutable receipts, but performs no PostgreSQL or provider operation.
    """
    folder = _folder(folder)
    convocation_id, profile_id = _id(convocation_id), _uuid(profile_id)
    expected_evaluation_id = _id(expected_evaluation_id)
    if not isinstance(request_inputs, dict) or not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
        raise ManualReconciliationError("Exact request inputs and explicit attempt identity are required")
    request_hash = canonical_hash(request_inputs)
    binding = _binding(request_inputs)
    model = request_inputs.get("model")
    evidence, absent = [], ["gemini_extraction-" + request_hash + ".response.private.json"]
    claim = _read(folder, "gemini_extraction-" + request_hash + ".claim.private.json", evidence)
    evaluation_key = hash_request({"convocation": convocation_id, "kind": "gemini_extraction",
        "request_hash": request_hash, "model": model, "policy_version": EXTRACTION_POLICY, "profile_id": profile_id})
    if (claim.get("status") != "claimed" or claim.get("id") != expected_evaluation_id
            or claim.get("evaluation_key") != evaluation_key):
        raise ManualReconciliationError("Claim ID or evaluation identity differs from the requested owned reservation")
    _uuid(claim.get("claim_token"))
    outcome = _read(folder, attempt_id + ".outcome.private.json", evidence)
    _match(outcome, binding)
    if outcome.get("attempt_id") != attempt_id or outcome.get("model") != model:
        raise ManualReconciliationError("Historical attempt model or ID differs")
    if outcome.get("request_hash") is not None and outcome["request_hash"] != request_hash:
        raise ManualReconciliationError("Historical provider request hash differs")
    if not isinstance(outcome.get("ended_at"), str) or not outcome["ended_at"]:
        raise ManualReconciliationError("Historical attempt has not ended")
    _cleanup(outcome)
    parent = None
    if attempt_id.startswith("gemini-"):
        if (binding["transport_policy"] != SELECTED_TRANSPORT_POLICY or model not in MODEL_PRICES_USD_PER_MILLION
                or outcome.get("status") != "response_parse_failure"
                or outcome.get("error_type") not in {"JSONDecodeError", "ValidationError"}
                or outcome.get("model_call_started") is not True
                or outcome.get("finish_reason") != "STOP" or outcome.get("finish_reasons") != ["STOP"]
                or parent_failure is not None):
            raise ManualReconciliationError("Only the exact known Gemini STOP parse failure may close here")
        started = _read(folder, attempt_id + ".started.private.json", evidence)
        response = _read(folder, attempt_id + ".response.private.json", evidence)
        cleanup = _read(folder, attempt_id + ".cleanup.private.json", evidence)
        _cleanup(cleanup)
        if cleanup.get("attempt_id") != attempt_id:
            raise ManualReconciliationError("Cleanup belongs to another attempt")
        if started.get("status") != "generation_reserved" or response.get("status") != "response_received":
            raise ManualReconciliationError("Historical generation or response is unproved")
        for value in (started, response):
            _match(value, binding)
            if value.get("attempt_id") != attempt_id or value.get("model") != model:
                raise ManualReconciliationError("Historical receipts identify different attempts")
            if any(value.get(key) != outcome.get(key) for key in ("max_output_tokens", "timeout_ms")):
                raise ManualReconciliationError("Historical generation limits differ")
        if (type(outcome.get("max_output_tokens")) is not int or outcome["max_output_tokens"] not in {24576, 65536}
                or type(outcome.get("timeout_ms")) is not int or outcome["timeout_ms"] not in {180000, 300000}):
            raise ManualReconciliationError("Historical generation bounds are unsupported")
        if any(response.get(key) != outcome.get(key) for key in
               ("usage", "input_tokens", "output_tokens", "cost_usd", "finish_reason", "finish_reasons")):
            raise ManualReconciliationError("Received usage/finish differs from the final audit")
        _known_usage(response)
        _known_usage(outcome)
        if any(response.get(key) is not None for key in
               ("response_text", "raw_response", "raw_json", "raw_extraction_json", "candidates")):
            raise ManualReconciliationError("Generated body exists; recover its paid reading before terminal failure")
        if response.get("response_text_status") not in {None, "unavailable"}:
            raise ManualReconciliationError("Generated body capture is contradictory")
        usage = {key: deepcopy(outcome[key]) for key in
                 ("usage", "input_tokens", "output_tokens", "cost_usd", "provider_usage_known",
                  "cost_is_estimate", "output_tokens_include_thinking")}
        provider, reason = "gemini", "STOP_parse_failure_body_unavailable"
        usage["usage_source"] = "provider_response"
    else:
        absent += [attempt_id + ".started.private.json", attempt_id + ".response.private.json"]
        if (binding["transport_policy"] != LUNA_SELECTED_TRANSPORT_POLICY or model != LUNA_REQUEST_POLICY["model"]
                or request_inputs.get("provider_request") != LUNA_REQUEST_POLICY
                or outcome.get("provider_request") != LUNA_REQUEST_POLICY or outcome.get("provider") != "openai"
                or outcome.get("reasoning_effort") != LUNA_REQUEST_POLICY["reasoning_effort"]
                or outcome.get("status") != "transport_failed" or outcome.get("error_type") != "SelectedTransportError"
                or outcome.get("model_call_started") is not False or outcome.get("provider_usage_known") is not False
                or type(outcome.get("cost_usd")) not in (float, int) or outcome["cost_usd"] != 0
                or outcome.get("input_tokens") is not None or outcome.get("output_tokens") is not None
                or outcome.get("usage") is not None or outcome.get("request_hash") != request_hash
                or type(outcome.get("max_output_tokens")) is not int
                or outcome["max_output_tokens"] != LUNA_REQUEST_POLICY["max_output_tokens"]
                or type(outcome.get("timeout_ms")) is not int or outcome["timeout_ms"] != 300000
                or not isinstance(parent_failure, dict)):
            raise ManualReconciliationError("Only a proved unissued Luna transport attempt may close here")
        parent = deepcopy(parent_failure)
        parent_hash = _sha(parent.get("request_hash"))
        parent_stem = "gemini_extraction-" + parent_hash
        actual_parent = _read(folder, parent_stem + ".failure.private.json", evidence)
        parent_claim = _read(folder, parent_stem + ".claim.private.json", evidence)
        if (actual_parent != parent or not validated_failure_receipt(parent_claim, parent)
                or parent.get("convocation_id") != convocation_id or parent.get("profile_id") != profile_id
                or parent.get("analysis_sha256") != binding["analysis_sha256"]
                or parent.get("document_set_hash") != binding["document_set_hash"]):
            raise ManualReconciliationError("Unissued Luna attempt lacks its exact reconciled Gemini parent")
        for suffix, hash_key in (("started", "started_receipt_hash"), ("response", "response_receipt_hash"),
                                 ("outcome", "attempt_receipt_hash"),
                                 (parent["remote_cleanup_receipt_source"], "remote_cleanup_receipt_hash")):
            parent_record = _read(folder, parent["attempt_id"] + "." + suffix + ".private.json", evidence)
            if canonical_hash(parent_record) != parent[hash_key]:
                raise ManualReconciliationError("Parent failure differs from its immutable source receipts")
        usage = {"usage": None, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0,
                 "provider_usage_known": False, "usage_source": "pre_generation_no_request",
                 "cost_is_estimate": False, "output_tokens_include_thinking": False}
        provider, reason = "openai", "pre_generation_transport_failed"
    _absent(folder, absent)
    plan = {"policy_version": POLICY, "kind": "gemini_extraction", "provider": provider,
        "status": "failed", "reconciled": False, "failure_reason": reason,
        "convocation_id": convocation_id, "profile_id": profile_id, "evaluation_id": expected_evaluation_id,
        "evaluation_key": evaluation_key, "claim_token": claim["claim_token"], "request_hash": request_hash,
        "model": model, "source_attempt_id": attempt_id, "binding": binding,
        "model_call_started": outcome["model_call_started"], "finish_reason": outcome.get("finish_reason"),
        "error_type": outcome["error_type"], "ended_at": outcome["ended_at"], **usage,
        "case_folder": str(folder), "evidence": evidence, "absent_artifacts": absent,
        "parent_failure": parent, "does_not_authorize_retry": True}
    plan["plan_sha256"] = canonical_hash(plan)
    return plan


def _checked_plan(plan):
    if not isinstance(plan, dict):
        raise ManualReconciliationError("Explicit reconciliation plan is required")
    base = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (plan.get("policy_version") != POLICY or plan.get("plan_sha256") != canonical_hash(base)
            or plan.get("status") != "failed" or plan.get("reconciled") is not False
            or plan.get("kind") != "gemini_extraction" or plan.get("does_not_authorize_retry") is not True):
        raise ManualReconciliationError("Reconciliation plan is changed or invalid")
    folder, evidence = _folder(plan["case_folder"]), []
    if not isinstance(plan.get("evidence"), list) or not plan["evidence"]:
        raise ManualReconciliationError("Plan has no immutable receipt proof")
    for proof in plan["evidence"]:
        _read(folder, proof["name"], evidence)
        if evidence[-1] != proof:
            raise ManualReconciliationError("Owned receipt changed after planning")
    _absent(folder, plan["absent_artifacts"])
    _id(plan["convocation_id"])
    _id(plan["evaluation_id"])
    _uuid(plan["profile_id"])
    _uuid(plan["claim_token"])
    _sha(plan["request_hash"])
    _sha(plan["evaluation_key"])
    _usage(plan["input_tokens"], plan["output_tokens"], plan["cost_usd"])
    # Re-read the actual owner and provider fields, rather than trusting the
    # plan checksum as permission to replace a paid audit with edited usage.
    names = {value["name"] for value in evidence}
    claim_name = "gemini_extraction-" + plan["request_hash"] + ".claim.private.json"
    outcome_name = plan["source_attempt_id"] + ".outcome.private.json"
    if not {claim_name, outcome_name}.issubset(names):
        raise ManualReconciliationError("Plan omits its exact owner or provider receipt")
    claim = _read(folder, claim_name, [])
    outcome = _read(folder, outcome_name, [])
    expected_key = hash_request({"convocation": plan["convocation_id"], "kind": "gemini_extraction",
        "request_hash": plan["request_hash"], "model": plan["model"],
        "policy_version": EXTRACTION_POLICY, "profile_id": plan["profile_id"]})
    if (claim.get("status") != "claimed" or claim.get("id") != plan["evaluation_id"]
            or claim.get("claim_token") != plan["claim_token"]
            or claim.get("evaluation_key") != expected_key or plan["evaluation_key"] != expected_key
            or outcome.get("attempt_id") != plan["source_attempt_id"] or outcome.get("model") != plan["model"]
            or outcome.get("ended_at") != plan["ended_at"] or outcome.get("error_type") != plan["error_type"]
            or outcome.get("model_call_started") is not plan["model_call_started"]):
        raise ManualReconciliationError("Plan no longer represents its exact owned failed attempt")
    _match(outcome, plan["binding"])
    _cleanup(outcome)
    completed_name = "gemini_extraction-" + plan["request_hash"] + ".response.private.json"
    if completed_name not in plan["absent_artifacts"]:
        raise ManualReconciliationError("Plan omits the completed bundle absence check")
    if plan["provider"] == "gemini":
        required = {plan["source_attempt_id"] + "." + suffix + ".private.json"
                    for suffix in ("started", "response", "cleanup")}
        if (not required.issubset(names) or plan["failure_reason"] != "STOP_parse_failure_body_unavailable"
                or plan["model_call_started"] is not True or plan["finish_reason"] != "STOP"
                or plan["error_type"] not in {"JSONDecodeError", "ValidationError"}
                or outcome.get("status") != "response_parse_failure" or outcome.get("finish_reasons") != ["STOP"]
                or plan["parent_failure"] is not None):
            raise ManualReconciliationError("Plan does not prove its known STOP parse failure")
        response = _read(folder, plan["source_attempt_id"] + ".response.private.json", [])
        _known_usage(response)
        _known_usage(outcome)
        if (any(plan.get(key) != outcome.get(key) or response.get(key) != outcome.get(key) for key in
                ("usage", "input_tokens", "output_tokens", "cost_usd", "provider_usage_known",
                 "cost_is_estimate", "output_tokens_include_thinking"))
                or plan["usage_source"] != "provider_response"):
            raise ManualReconciliationError("Plan changes the exact historical received usage")
    elif plan["provider"] == "openai":
        if (plan["failure_reason"] != "pre_generation_transport_failed" or plan["model_call_started"] is not False
                or outcome.get("status") != "transport_failed" or outcome.get("request_hash") != plan["request_hash"]
                or outcome.get("cost_usd") != 0 or outcome.get("provider_usage_known") is not False
                or plan["model"] != LUNA_REQUEST_POLICY["model"]
                or plan["input_tokens"] != 0 or plan["output_tokens"] != 0 or plan["cost_usd"] != 0
                or plan["usage"] is not None or plan["provider_usage_known"] is not False
                or plan["usage_source"] != "pre_generation_no_request"
                or not isinstance(plan["parent_failure"], dict)
                or any(plan["source_attempt_id"] + "." + suffix + ".private.json" not in plan["absent_artifacts"]
                       for suffix in ("started", "response"))):
            raise ManualReconciliationError("Plan does not prove an unissued transport request")
        parent = plan["parent_failure"]
        parent_stem = "gemini_extraction-" + parent["request_hash"]
        if not {parent_stem + suffix for suffix in (".claim.private.json", ".failure.private.json")}.issubset(names):
            raise ManualReconciliationError("Plan omits its known parent failure")
        parent_claim = _read(folder, parent_stem + ".claim.private.json", [])
        if (_read(folder, parent_stem + ".failure.private.json", []) != parent
                or not validated_failure_receipt(parent_claim, parent)):
            raise ManualReconciliationError("Plan changes its reconciled parent failure")
    else:
        raise ManualReconciliationError("Unknown provider in terminal failure plan")
    return {**deepcopy(plan), "reconciled": True}


def _same_usage(row, failure):
    return (row.get("input_tokens") == failure["input_tokens"]
        and row.get("output_tokens") == failure["output_tokens"]
        and row.get("cost_usd") is not None
        and math.isclose(float(row["cost_usd"]), failure["cost_usd"], rel_tol=1e-6, abs_tol=5e-9))


def apply_closed_failure(conn, plan):
    """Close only the exact owned row as FAILED, with truthful paid/no-call usage.

    Other unresolved rows are reported, never changed. The caller must inspect
    that result before independently deciding whether another read is allowed.
    """
    failure = _checked_plan(plan)
    with conn.transaction():
        row = conn.execute("SELECT * FROM prod2_evaluations WHERE id=%s FOR UPDATE",
                           (failure["evaluation_id"],)).fetchone()
        if (not row or row.get("id") != failure["evaluation_id"]
                or row.get("id_convocatoria") != failure["convocation_id"]
                or str(row.get("profile_id")) != failure["profile_id"]
                or row.get("kind") != failure["kind"] or row.get("model") != failure["model"]
                or row.get("policy_version") != EXTRACTION_POLICY
                or row.get("request_hash") != failure["request_hash"]
                or row.get("evaluation_key") != failure["evaluation_key"]
                or str(row.get("claim_token")) != failure["claim_token"]
                or row.get("status") not in {"running", "uncertain", "failed"}):
            raise ManualReconciliationError("Live evaluation is not this exact owned reservation")
        parent = failure["parent_failure"]
        if parent is not None:
            parent_row = conn.execute("SELECT * FROM prod2_evaluations WHERE id=%s FOR UPDATE",
                                     (parent["evaluation_id"],)).fetchone()
            if (not parent_row or parent_row.get("status") != "failed" or parent_row.get("result_json") != parent
                    or parent_row.get("id_convocatoria") != failure["convocation_id"]
                    or str(parent_row.get("profile_id")) != failure["profile_id"]
                    or parent_row.get("id") != parent["evaluation_id"]
                    or parent_row.get("model") != parent["model"] or parent_row.get("kind") != parent["kind"]
                    or parent_row.get("policy_version") != EXTRACTION_POLICY
                    or parent_row.get("request_hash") != parent["request_hash"]
                    or parent_row.get("evaluation_key") != parent["evaluation_key"]
                    or str(parent_row.get("claim_token")) != parent["claim_token"]
                    or not _same_usage(parent_row, parent)):
                raise ManualReconciliationError("Parent failure is not durably reconciled in PostgreSQL")
        reused = row["status"] == "failed"
        if reused:
            if row.get("result_json") != failure or not _same_usage(row, failure):
                raise ManualReconciliationError("Existing failed row differs from the exact closing receipt")
        else:
            if (row.get("result_json") is not None or any(row.get(key) is not None and row[key] != failure[key]
                    for key in ("input_tokens", "output_tokens"))
                    or row.get("cost_usd") is not None and not math.isclose(
                        float(row["cost_usd"]), failure["cost_usd"], rel_tol=1e-6, abs_tol=5e-9)):
                raise ManualReconciliationError("Live reservation contains contradictory received output or usage")
            updated = conn.execute("""UPDATE prod2_evaluations SET status='failed',result_json=%s::jsonb,
                input_tokens=%s,output_tokens=%s,cost_usd=%s,completed_at=now(),updated_at=now()
                WHERE id=%s AND claim_token=%s AND status IN ('running','uncertain') RETURNING id""",
                (_json(failure), failure["input_tokens"], failure["output_tokens"], failure["cost_usd"],
                 failure["evaluation_id"], failure["claim_token"])).fetchone()
            if not updated or updated.get("id") != failure["evaluation_id"]:
                raise ManualReconciliationError("Reservation ownership changed during terminal reconciliation")
        unresolved = conn.execute("""SELECT id FROM prod2_evaluations WHERE id_convocatoria=%s
            AND profile_id=%s AND kind='gemini_extraction' AND id<>%s
            AND status IN ('pending','running','uncertain') ORDER BY id FOR UPDATE""",
            (failure["convocation_id"], failure["profile_id"], failure["evaluation_id"])).fetchall()
    return {"failure": failure, "evaluation_id": failure["evaluation_id"], "status": "failed",
            "reused": reused, "other_unresolved_evaluation_ids": [value["id"] for value in unresolved],
            "does_not_authorize_retry": True}


def validated_manual_failure_receipt(claim, failure):
    """Local terminal-failure proof for cleanup, never a payment/retry permit.

    Reads only pinned local receipts through the offline checker. The cleanup
    caller must independently establish a completed PostgreSQL extraction and
    no unresolved payments; this helper does not assert either condition.
    """
    try:
        if (not isinstance(claim, dict) or not isinstance(failure, dict)
                or claim.get("status") != "claimed" or failure.get("reconciled") is not True
                or failure.get("status") != "failed" or failure.get("policy_version") != POLICY
                or claim.get("id") != failure.get("evaluation_id")
                or _uuid(claim.get("claim_token")) != _uuid(failure.get("claim_token"))
                or claim.get("evaluation_key") != failure.get("evaluation_key")):
            return False
        planned = deepcopy(failure)
        planned["reconciled"] = False
        return _checked_plan(planned) == failure
    except (KeyError, TypeError, ValueError, OSError):
        return False
