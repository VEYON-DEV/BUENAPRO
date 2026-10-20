"""Explicit, lossless local partitioning of a structured capacity JEV payload.

There is no dispatch, retry, cache, filesystem, credential or database access.
The caller must opt into this policy, freeze every part and reserve each paid
request separately. An aggregate is derived evidence, never a provider receipt.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Callable

from .jev_recommendations import (
    JEV_MODEL, MAX_QUESTION_TOKENS, MAX_REQUEST_TOKENS, _dimension,
    parse_jev_answers, token_budget,
)

PARTITION_POLICY = "jev_capacity_partition_v1"
MAX_PARTS = 12
_TARGET = re.compile(r"state\.requirements\[(\d+)\]")
_CONTEXT_ERROR = "JEV default context limit exceeded; no truncation or automatic paid split permitted"


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_source(payload: dict) -> dict[str, int | None]:
    """Accept precisely the existing builder's closed question vocabulary."""
    if (not isinstance(payload, dict) or payload.get("model") != JEV_MODEL
            or not isinstance(payload.get("state"), dict)
            or not isinstance(payload.get("questions"), dict)):
        raise ValueError("Expected an unchanged structured capacity JEV payload")
    state = payload["state"]
    requirements, profile = state.get("requirements"), state.get("profile")
    if (not isinstance(requirements, list) or not requirements
            or not isinstance(profile, dict)
            or not isinstance(profile.get("contracts"), list)
            or not isinstance(profile.get("documents"), list)):
        raise ValueError("Complete requirement and profile cards are required")
    _hash(payload)  # Reject non-JSON/NaN identities before any projection.
    refs = [item.get("ref") for item in profile["contracts"] if isinstance(item, dict)]
    if (len(refs) != len(profile["contracts"]) or len(set(refs)) != len(refs)
            or any(not isinstance(ref, str) or not re.fullmatch(r"C[1-9]\d*", ref) for ref in refs)):
        raise ValueError("Profile contract references must be complete and unique")
    indices = [r.get("source_index") for r in requirements if isinstance(r, dict)]
    if (len(indices) != len(requirements) or any(type(i) is not int or i <= 0 for i in indices)
            or len(set(indices)) != len(indices)):
        raise ValueError("Requirement source indices must be complete and unique")
    owners: dict[str, int | None] = {"rubro_scope": None, "rubro_score": None}
    for index, requirement in enumerate(requirements):
        stem = f"r{requirement['source_index']:04d}"
        owners[stem + "_status"] = index
        dimension = _dimension(requirement)
        if dimension in {"experiencia", "perfiles"}:
            owners[stem + "_score"] = index
        if dimension == "economica":
            for ref in refs:
                owners[stem + f"_{ref}_scope"] = index
    if list(payload["questions"]) != list(owners):
        raise ValueError("Full source question coverage or order differs from its requirements")
    for qid, original_index in owners.items():
        question = payload["questions"][qid]
        if (not isinstance(question, dict) or question.get("type") not in {"choice", "score"}
                or not isinstance(question.get("instructions"), str)):
            raise ValueError("Invalid closed JEV primitive")
        targets = _TARGET.findall(question["instructions"])
        if targets != ([] if original_index is None else [str(original_index)]):
            raise ValueError("Question target differs from its exact original requirement")
    return owners


def _project(payload: dict, qids: list[str], owners: dict) -> tuple[dict, list[int], dict]:
    original_indices = sorted({owners[qid] for qid in qids if owners[qid] is not None})
    local = {original: i for i, original in enumerate(original_indices)}
    part = deepcopy(payload)
    part["state"]["requirements"] = [deepcopy(payload["state"]["requirements"][i])
                                      for i in original_indices]
    part["questions"] = {}
    mapping = {}
    for qid in qids:
        question = deepcopy(payload["questions"][qid])
        original = owners[qid]
        local_index = local[original] if original is not None else None
        if original is not None:
            question["instructions"] = _TARGET.sub(
                lambda match: f"state.requirements[{local_index}]", question["instructions"])
        part["questions"][qid] = question
        mapping[qid] = {"original_requirement_index": original,
                        "local_requirement_index": local_index}
    return part, original_indices, mapping


def _budget(payload: dict, token_counter: Callable[[str], int] | None) -> dict | None:
    try:
        return token_budget(payload, token_counter=token_counter)
    except ValueError as error:
        if str(error) != _CONTEXT_ERROR:
            raise
        return None


def partition_jev_payload(payload: dict, *, policy_version: str,
                          token_counter: Callable[[str], int] | None = None,
                          max_parts: int = MAX_PARTS) -> dict:
    """Greedily pack exact requirement groups under existing 64k/32k gates.

    Only a group that cannot fit by itself may be split by question. Such parts
    repeat its identical requirement, never an answer or a profile-card subset.
    An indivisible oversized requirement/question fails instead of truncating.
    """
    if policy_version != PARTITION_POLICY:
        raise ValueError("Explicit capacity partition policy is required")
    if type(max_parts) is not int or not 1 <= max_parts <= MAX_PARTS:
        raise ValueError("Partition count must remain within the finite twelve-part ceiling")
    owners = _validate_source(payload)
    groups = [[qid for qid, index in owners.items() if index is None]]
    groups += [[qid for qid, owner in owners.items() if owner == index]
               for index in range(len(payload["state"]["requirements"]))]
    groups = [group for group in groups if group]
    parts: list[dict] = []
    current: list[str] = []

    def fits(qids: list[str]) -> bool:
        return _budget(_project(payload, qids, owners)[0], token_counter) is not None

    def finish(qids: list[str]) -> None:
        part, original_indices, mapping = _project(payload, qids, owners)
        budget = _budget(part, token_counter)
        if not qids or budget is None:
            raise ValueError("Indivisible requirement or complete profile exceeds the JEV context guard")
        if len(parts) >= max_parts:
            raise ValueError("Finite JEV partition ceiling exceeded; no automatic expansion permitted")
        parts.append({"part_id": f"part-{len(parts) + 1:03d}", "payload": part,
                      "payload_sha256": _hash(part), "requirement_indices": original_indices,
                      "question_mapping": mapping, "token_budget": budget})

    for group in groups:
        if fits(current + group):
            current += group
            continue
        if current:
            finish(current)
            current = []
        if fits(group):
            current = group
            continue
        # Economic contract comparisons may exceed the overall request guard;
        # preserve all questions and all profile cards across bounded parts.
        for qid in group:
            if not fits([qid]):
                raise ValueError("Indivisible requirement or complete profile exceeds the JEV context guard")
            if current and not fits(current + [qid]):
                finish(current)
                current = []
            current.append(qid)
    if current:
        finish(current)
    plan = {"policy_version": PARTITION_POLICY, "model": payload["model"],
            "full_payload_sha256": _hash(payload), "profile_sha256": _hash(payload["state"]["profile"]),
            "limits": {"max_parts": max_parts, "max_request_tokens": MAX_REQUEST_TOKENS,
                       "max_question_tokens": MAX_QUESTION_TOKENS, "long_context": False},
            "question_ids": list(payload["questions"]),
            "requirement_source_indices": [r["source_index"] for r in payload["state"]["requirements"]],
            "parts": parts, "truncated": False, "profile_cards_complete_in_every_part": True}
    plan["plan_sha256"] = _hash(plan)
    return plan


def aggregate_jev_parts(payload: dict, plan: dict, responses: list[dict], *,
                        token_counter: Callable[[str], int] | None = None) -> dict:
    """Reassemble known, exact, unique answers without inventing a paid receipt.

    Each input envelope is {part_id, payload_sha256, response}. The returned
    response passes parse_jev_answers(full_payload); its separate proof explicitly
    records derivation and the hashes of every original part-response envelope.
    This function cannot dispatch or retry a missing/uncertain part.
    """
    owners = _validate_source(payload)
    if (not isinstance(plan, dict) or plan.get("policy_version") != PARTITION_POLICY
            or plan.get("plan_sha256") != _hash({k: v for k, v in plan.items() if k != "plan_sha256"})
            or plan.get("model") != payload["model"] or plan.get("full_payload_sha256") != _hash(payload)
            or plan.get("profile_sha256") != _hash(payload["state"]["profile"])
            or plan.get("question_ids") != list(payload["questions"])
            or plan.get("requirement_source_indices") != [r["source_index"] for r in payload["state"]["requirements"]]
            or plan.get("truncated") is not False or plan.get("profile_cards_complete_in_every_part") is not True):
        raise ValueError("Frozen partition plan differs from the exact source")
    parts = plan.get("parts")
    limits = plan.get("limits", {})
    if (not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS
            or type(limits.get("max_parts")) is not int or not len(parts) <= limits["max_parts"] <= MAX_PARTS
            or limits.get("max_request_tokens") != MAX_REQUEST_TOKENS
            or limits.get("max_question_tokens") != MAX_QUESTION_TOKENS or limits.get("long_context") is not False
            or not isinstance(responses, list) or len(responses) != len(parts)):
        raise ValueError("Exact finite partition response coverage is required")
    expected_ids = [f"part-{i + 1:03d}" for i in range(len(parts))]
    if [p.get("part_id") for p in parts if isinstance(p, dict)] != expected_ids:
        raise ValueError("Frozen partition identity or order changed")
    by_id = {}
    for envelope in responses:
        if (not isinstance(envelope, dict) or set(envelope) != {"part_id", "payload_sha256", "response"}
                or envelope["part_id"] not in expected_ids or envelope["part_id"] in by_id):
            raise ValueError("Unknown or repeated partition response")
        by_id[envelope["part_id"]] = envelope
    answers, all_qids, hashes = {}, [], []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for part in parts:
        if (not isinstance(part.get("payload"), dict)
                or not isinstance(part["payload"].get("questions"), dict)):
            raise ValueError("Partition payload must remain a structured object")
        qids = list(part["payload"]["questions"])
        if not qids or any(qid not in owners or qid in all_qids for qid in qids):
            raise ValueError("Partition questions overlap or leave the closed source vocabulary")
        projected, indices, mapping = _project(payload, qids, owners)
        if (part.get("payload") != projected or part.get("payload_sha256") != _hash(projected)
                or part.get("requirement_indices") != indices or part.get("question_mapping") != mapping
                or not isinstance(part.get("token_budget"), dict)
                or part.get("token_budget") != _budget(projected, token_counter)):
            raise ValueError("Partition payload, complete profile, requirement target or budget changed")
        envelope = by_id[part["part_id"]]
        response = envelope["response"]
        if (envelope["payload_sha256"] != part["payload_sha256"] or not isinstance(response, dict)
                or response.get("usage_known", True) is not True
                or response.get("provider_usage_known", True) is not True):
            raise ValueError("Partition response identity or known usage differs")
        parsed = parse_jev_answers(projected, response)
        answers.update(parsed)
        all_qids += qids
        for key in usage:
            usage[key] += response["usage"][key]
        hashes.append({"part_id": part["part_id"], "receipt_sha256": _hash(envelope),
                       "payload_sha256": part["payload_sha256"], "usage": deepcopy(response["usage"])})
    if all_qids != list(payload["questions"]):
        raise ValueError("Full question coverage/order changed across partitions")
    aggregate = {"model": payload["model"], "answers": answers, "usage": usage}
    parse_jev_answers(payload, aggregate)
    return {"response": aggregate, "proof": {"policy_version": PARTITION_POLICY,
            "aggregated": True, "not_single_provider_response": True,
            "plan_sha256": plan["plan_sha256"], "full_payload_sha256": plan["full_payload_sha256"],
            "part_receipt_hashes": hashes, "question_count": len(answers),
            "profile_cards_complete_in_every_part": True, "usage_known": True}}
