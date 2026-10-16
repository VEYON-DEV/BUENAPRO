"""Exact, offline source-citation reclassification for the opt-in DIN cohort.

Requirement citations belong to the frozen extraction, never to the company's
D/P evidence aliases. Only an unknown alias identical to the citation of that
same requirement may be removed. All model text and valid aliases stay intact.
This module performs no I/O and cannot change scores or requirement states.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


DECODER_VERSION = "exact_requirement_source_citation_v1"
PROOF_FILENAME = "source-citation-decoder-proof.private.json"


def _sha(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def normalize_requirement_source_refs(value: dict, payload: dict) -> tuple[dict, list[dict]]:
    """Return an independent derived recommendation and exact removal notes.

    No fuzzy matching, trimming, page-to-D conversion, missing-row repair or
    cross-requirement attribution is allowed. The strict recommendation schema
    must still validate the returned object before it can be published.
    """
    requirements = payload["state"]["requirements"]
    expected = [row["source_index"] for row in requirements]
    if (any(type(index) is not int for index in expected)
            or len(expected) != len(set(expected))):
        raise ValueError("Invalid frozen requirement identities")
    rows = value.get("requisitos") if isinstance(value, dict) else None
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or any(type(row.get("source_index")) is not int for row in rows)
            or [row["source_index"] for row in rows] != expected):
        raise ValueError("Recommendation requirement coverage/order changed")
    profile = payload["state"]["profile"]
    valid = {row["ref"] for row in profile["documents"]}
    valid.update(row["ref"] for row in profile.get("professionals", []))
    derived = deepcopy(value)
    notes = []
    for row, requirement in zip(derived["requisitos"], requirements, strict=True):
        refs = row.get("evidence_refs")
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("Recommendation evidence_refs must be a list of strings")
        unknown = [ref for ref in refs if ref not in valid]
        if not unknown:
            continue
        detail = requirement.get("detalle")
        citation = detail.get("evidencia") if isinstance(detail, dict) else None
        if (not isinstance(citation, str) or not citation
                or any(ref != citation for ref in unknown)):
            raise ValueError("Recommendation cites an unknown documentary reference")
        # Preserve order and duplicates of genuine documentary/professional refs.
        row["evidence_refs"] = [ref for ref in refs if ref in valid]
        notes.append({"source_index": row["source_index"],
                      "source_requirement_citation": citation,
                      "removed_from_company_refs": unknown,
                      "retained_company_refs": list(row["evidence_refs"]),
                      "evidence_text_unchanged": True,
                      "source_citation_is_not_company_evidence": True})
    return derived, notes


def normalize_batch_source_refs(value: dict, payloads: dict[str, dict]) -> tuple[dict, list[dict]]:
    """Require exact batch identities before the same per-requirement rule."""
    rows = value.get("evaluations") if isinstance(value, dict) and set(value) == {"evaluations"} else None
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or [row.get("case_key") for row in rows] != list(payloads)):
        raise ValueError("Recommendation batch case coverage/order changed")
    derived = {"evaluations": []}
    notes = []
    for row in rows:
        key = row["case_key"]
        result, case_notes = normalize_requirement_source_refs(
            {name: field for name, field in row.items() if name != "case_key"}, payloads[key])
        derived["evaluations"].append({"case_key": key, **result})
        notes.extend({"case_key": key, **note} for note in case_notes)
    return derived, notes


def source_ref_decoder_proof(*, request_sha256: str, response_sha256: str,
                             jev_requests: dict[str, str], recommendations: dict[str, dict],
                             notes: list[dict]) -> dict | None:
    """Bind a derived decode to untouched paid inputs, outputs and source IDs."""
    if not notes:
        return None
    return {"decoder_version": DECODER_VERSION, "request_sha256": request_sha256,
            "response_sha256": response_sha256, "jev_requests": deepcopy(jev_requests),
            "normalizations": deepcopy(notes),
            "normalized_case_sha256": {key: _sha(value) for key, value in recommendations.items()},
            "normalized_recommendations_sha256": _sha(recommendations),
            "raw_response_preserved": True, "score_or_status_changed": False,
            "new_inference_calls": 0}


def verify_source_ref_case_proof(proof: dict, *, case_key: str, request_sha256: str,
                                 response_sha256: str, jev_request_sha256: str,
                                 recommendation: dict, notes: list[dict]) -> None:
    """A cached corrected case requires the immutable whole-batch decoder proof."""
    if (proof.get("decoder_version") != DECODER_VERSION
            or proof.get("request_sha256") != request_sha256
            or proof.get("response_sha256") != response_sha256
            or proof.get("jev_requests", {}).get(case_key) != jev_request_sha256
            or proof.get("normalized_case_sha256", {}).get(case_key) != _sha(recommendation)
            or [note for note in proof.get("normalizations", []) if note.get("case_key") == case_key]
                != [{"case_key": case_key, **note} for note in notes]
            or proof.get("raw_response_preserved") is not True
            or proof.get("score_or_status_changed") is not False
            or proof.get("new_inference_calls") != 0):
        raise ValueError("Source-citation decoder proof does not bind this exact cached case")
