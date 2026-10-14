"""Optional cross-batch preservation of prior PROD2 readings, without repayment.

This is preservation, not a cache hit for the new reading policy. It reads the
canonical extraction and its historical paid reservation without changing either
one. Incomplete, failed and uncertain earlier readings remain review work rather
than becoming implicit authorization for another provider request.
"""
from __future__ import annotations

from datetime import datetime
import math

from buenapro_worker.normalization.facets import canonical_hash
from .extraction_policy import validate_scope_payload
from .input_policy import schema_for_input
from .llm1_completion import _document_set_context, _source_reference
from .repository import _id, _sha, _text, _uuid

POLICY = "prod2_preserve_prior_readings_v1"

_PRIOR_READINGS_SQL = """SELECT
    ev.id AS evaluation_id,ev.id_convocatoria,ev.profile_id,ev.kind,ev.status,
    ev.request_hash,ev.model AS reserved_model,ev.policy_version,ev.completed_at,ev.result_json,
    ex.id AS extraction_id,ex.id_convocatoria AS extraction_convocation_id,
    ex.evaluation_id AS extraction_evaluation_id,ex.opportunity_id::text AS opportunity_id,
    ex.codigo_alfresco::text AS document_uuid,ex.sha256_original,ex.is_current,ex.quality,
    ex.model AS extraction_model,ex.prompt_version AS extraction_prompt_version,
    ex.schema_version AS extraction_schema_version,ex.requires_human_review,
    ex.raw_extraction_json,ex.facet_count,
    d.is_listed,d.source_url,
    dl.sha256_original AS download_sha256,dl.bytes AS download_bytes,
    disc.opportunity_id::text AS canonical_opportunity_id,
    op.id::text AS canonical_opportunity_exists
    FROM prod2_evaluations ev
    LEFT JOIN prod2_document_extractions ex
      ON ex.evaluation_id=ev.id AND ex.id_convocatoria=ev.id_convocatoria
    LEFT JOIN prod2_documents d
      ON d.id_convocatoria=ex.id_convocatoria AND d.codigo_alfresco=ex.codigo_alfresco
    LEFT JOIN prod2_document_downloads dl
      ON dl.id_convocatoria=ex.id_convocatoria AND dl.codigo_alfresco=ex.codigo_alfresco
        AND dl.sha256_original=ex.sha256_original
    LEFT JOIN prod2_discoveries disc ON disc.id_convocatoria=ev.id_convocatoria
    LEFT JOIN opportunities op ON op.id=disc.opportunity_id
    WHERE ev.id_convocatoria=%s AND ev.profile_id=%s AND ev.kind='gemini_extraction'
    ORDER BY ex.is_current DESC NULLS LAST,ev.completed_at DESC NULLS LAST,ev.id DESC"""


def validate_preservation_policy(policy):
    """An absent policy preserves old behavior; only the exact version opts in."""
    if policy is None:
        return None
    if not isinstance(policy, str) or policy != POLICY:
        raise ValueError("Unsupported prior reading preservation policy")
    return policy


def _valid_current(row, conv, profile):
    """Validate the historical contract itself, never against new input policy."""
    try:
        if (row.get("id_convocatoria") != conv or str(row.get("profile_id")) != profile
                or row.get("kind") != "gemini_extraction" or row.get("status") != "done"
                or row.get("policy_version") != "prod2_document_extraction_v1"
                or row.get("extraction_convocation_id") != conv
                or row.get("is_current") is not True or row.get("is_listed") is not True
                or row.get("quality") not in {"auto", "reviewed", "corrected"}
                or type(row.get("requires_human_review")) is not bool
                or type(row.get("facet_count")) is not int or row["facet_count"] < 0):
            return None
        evaluation, extraction = _id(row["evaluation_id"]), _id(row["extraction_id"])
        if _id(row["extraction_evaluation_id"]) != evaluation:
            return None
        opportunity = _uuid(row["opportunity_id"])
        if (opportunity != _uuid(row["canonical_opportunity_id"])
                or opportunity != _uuid(row["canonical_opportunity_exists"])):
            return None
        completed = row["completed_at"]
        if not isinstance(completed, datetime) or completed.tzinfo is None or completed.utcoffset() is None:
            return None
        code, sha = _uuid(row["document_uuid"]), _sha(row["sha256_original"])
        request = _sha(row["request_hash"])
        if (row.get("download_sha256") != sha or type(row.get("download_bytes")) is not int
                or row["download_bytes"] <= 0):
            return None
        source = _source_reference(row["source_url"], code)
        bundle, raw = row.get("result_json"), row.get("raw_extraction_json")
        if not isinstance(bundle, dict) or not isinstance(raw, dict) or not raw:
            return None
        if (bundle.get("raw_extraction_json") != raw or bundle.get("document_uuid") != code
                or bundle.get("document_sha256") != sha or bundle.get("original_sha256") != sha
                or bundle.get("cache_key") != "prod2:llm1:" + request
                or type(bundle.get("requires_human_review")) is not bool
                or bundle.get("requires_human_review") != row["requires_human_review"]):
            return None
        for bundle_key, row_key in (("model", "extraction_model"),
                ("prompt_version", "extraction_prompt_version"),
                ("schema_version", "extraction_schema_version")):
            value = _text(bundle[bundle_key], limit=200)
            if value != row.get(row_key):
                return None
        if bundle["model"] != row.get("reserved_model"):
            return None
        _sha(bundle["analysis_sha256"])
        _sha(bundle["prompt_hash"])
        for key in ("input_tokens", "output_tokens"):
            if type(bundle.get(key)) is not int or bundle[key] < 0:
                return None
        cost = bundle.get("cost_usd")
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            return None
        selection = bundle.get("selection_context")
        validate_scope_payload(raw, selection)
        schema_for_input(bundle.get("doc_class"), selection).model_validate(raw)
        _document_set_context(bundle, code, sha)
        return {"evaluation_id": evaluation, "extraction_id": extraction,
            "request_hash": request, "document_uuid": code, "document_sha256": sha,
            "source_url": source, "opportunity_id": opportunity,
            "model": bundle["model"], "prompt_version": bundle["prompt_version"],
            "schema_version": bundle["schema_version"],
            "requires_human_review": row["requires_human_review"],
            "facet_count": row["facet_count"], "paid_result_sha256": canonical_hash(bundle),
            "raw_extraction_sha256": canonical_hash(raw)}
    except (KeyError, TypeError, ValueError):
        return None


def _reservations(rows):
    """Compact evidence only; no response, personal data or credentials."""
    return [{"evaluation_id": row.get("evaluation_id"), "status": row.get("status"),
        "extraction_id": row.get("extraction_id"), "is_current": row.get("is_current")}
        for row in rows]


def preserve_prior_reading(conn, *, convocation_id, profile_id, policy=None):
    """Return a preservation outcome before any document or provider activity.

    None means no opt-in or no historical LLM1 reservation for this profile.
    Existing review work returns a terminal needs_review preservation outcome.
    A valid current canonical extraction outranks older failed attempts, while
    explicitly retaining its original policy and human-review state.
    """
    if validate_preservation_policy(policy) is None:
        return None
    conv, profile = _id(convocation_id), _uuid(profile_id)
    rows = conn.execute(_PRIOR_READINGS_SQL, (conv, profile)).fetchall()
    if not rows:
        return None
    valid = [proof for row in rows if (proof := _valid_current(row, conv, profile)) is not None]
    evidence = {"policy_version": POLICY, "convocation_id": conv, "profile_id": profile,
        "prior_reservations": _reservations(rows), "new_reading_policy_equivalence_claimed": False,
        "provider_calls": 0, "source_calls": 0, "business_writes": 0}
    # The database has a unique current extraction per convocatoria. Multiple
    # valid identities would contradict that canonical contract: fail closed.
    if len(valid) == 1:
        proof = valid[0]
        evidence.update(reason="valid_current_canonical_reading_preserved", canonical=proof)
        return {"status": "llm1_complete", "llm1_prior_preserved": True,
            "llm1_cost_usd": 0, "llm1_evaluation_id": proof["evaluation_id"],
            "llm1_request_hash": proof["request_hash"], "extraction_id": proof["extraction_id"],
            "llm1_model": proof["model"], "requires_human_review": proof["requires_human_review"],
            "facet_count": proof["facet_count"], "prior_reading_preserved": evidence}
    evidence["reason"] = "ambiguous_current_canonical_reading" if valid else "prior_reading_requires_review"
    return {"status": "needs_review", "llm1_prior_preserved": True,
        "llm1_cost_usd": 0, "requires_human_review": True,
        "prior_reading_preserved": evidence}
