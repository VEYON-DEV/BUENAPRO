"""Adopt an exact PostgreSQL-completed read before rebuilding disposable PDFs.

Only SELECTs and local original-byte verification occur here. Durable paid
metadata is the authority for the old derivative; that derivative is never
recreated. No receipt, raw reading, review flag, model or request key is rewritten.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from buenapro_worker.normalization.facets import canonical_hash
from .analysis import (
    _document_input_from_verified_sha, extraction_request_hash_from_verified_sha,
)
from .extraction_policy import (
    schema_for_scope, schema_version_for_scope, validate_scope_payload, get_scope_policy,
)
from .llm1_completion import _selected_context
from .model_routing import resolve_llm1_model
from .pdf_preparation import selected_preparation_identity, _source
from .repository import _id, _uuid, _sha


class CompletedReadReuseError(ValueError):
    """A prior paid identity exists but cannot safely establish reusable evidence."""


_CONTEXT_FIELDS = ("document_scope", "document_set", "document_set_hash",
                   "document_set_identity", "selection_context")


def _no_hit(conn, conv, profile):
    """A new derivative must never evade an unresolved previous payment."""
    unresolved = conn.execute("""SELECT id,status FROM prod2_evaluations
        WHERE id_convocatoria=%s AND profile_id=%s AND kind='gemini_extraction'
          AND status IN ('pending','running','uncertain') LIMIT 1""", (conv, profile)).fetchone()
    if unresolved:
        raise CompletedReadReuseError("Prior LLM1 reservation requires reconciliation; automatic repayment blocked")
    return None


def _verify_originals(folder, identity, files):
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir() or not isinstance(files, dict):
        raise CompletedReadReuseError("Selected original files require a private case folder")
    folder = folder.resolve()
    unique = {}
    for row in identity["document_set"]:
        candidate = row["candidate_id"]
        if candidate not in files:
            raise CompletedReadReuseError("Selected original verification is incomplete")
        content = _source(Path(files[candidate]), folder, row["original_sha256"],
                          identity["max_source_bytes"])
        unique.setdefault(row["original_sha256"], len(content))
        if sum(unique.values()) > identity["max_source_bytes"]:
            raise CompletedReadReuseError("Selected originals exceed aggregate source policy")


def _model_settings(settings, model):
    if settings.gemini_model == model:
        return settings
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update={"gemini_model": model})
    result = copy.copy(settings)
    result.gemini_model = model
    return result


def _durable_bundle(row, conv, profile, identity, anchor, anchor_sha):
    bundle = row.get("result_json")
    if (row.get("id_convocatoria") != conv or row.get("kind") != "gemini_extraction"
            or row.get("status") != "done" or str(row.get("profile_id")) != profile
            or row.get("policy_version") != "prod2_document_extraction_v1"
            or type(row.get("evaluation_id")) is not int or row["evaluation_id"] <= 0
            or type(row.get("extraction_id")) is not int or row["extraction_id"] <= 0
            or row.get("is_current") is not True or row.get("is_listed") is not True
            or row.get("quality") in (None, "failed") or not isinstance(bundle, dict)
            or bundle.get("raw_extraction_json") != row.get("raw_extraction_json")
            or bundle.get("model") != row.get("reserved_model")
            or bundle.get("model") != row.get("extraction_model")
            or row.get("document_uuid") != anchor or row.get("sha256_original") != anchor_sha
            or bundle.get("document_uuid") != anchor or bundle.get("document_sha256") != anchor_sha
            or bundle.get("original_sha256") != anchor_sha
            or bundle.get("document_set_identity") != identity
            or bundle.get("document_set_hash") != canonical_hash(identity)
            or type(bundle.get("requires_human_review")) is not bool):
        raise CompletedReadReuseError("Prior completed read differs from current durable evidence")
    request_hash = _sha(row.get("request_hash"))
    if bundle.get("cache_key") != "prod2:llm1:" + request_hash:
        raise CompletedReadReuseError("Prior receipt does not bind its completed request")
    for key in ("model", "prompt_version", "schema_version"):
        if not isinstance(bundle.get(key), str) or not bundle[key]:
            raise CompletedReadReuseError("Prior completed read lacks exact model/schema/prompt versions")
    if bundle.get("doc_class") not in {"bases_service", "bases_good"}:
        raise CompletedReadReuseError("Prior completed selected read lacks its document class")
    _sha(bundle.get("analysis_sha256"))
    _sha(bundle.get("prompt_hash"))
    for key in ("analysis_bytes", "transport_parts"):
        if key in bundle and (type(bundle[key]) is not int or bundle[key] <= 0):
            raise CompletedReadReuseError("Prior transport metadata is invalid")
    context = _selected_context(bundle, anchor, anchor_sha)
    raw = bundle["raw_extraction_json"]
    if ("source_document_manifest" in bundle
            and bundle["source_document_manifest"] != context["source_document_manifest"]):
        raise CompletedReadReuseError("Prior source page manifest is incomplete")
    validate_scope_payload(raw, identity["selection_context"])
    schema_for_scope(bundle.get("doc_class"), identity["selection_context"]).model_validate(raw)
    if get_scope_policy(identity["selection_context"]):
        if raw.get("extraction_coverage") != {
            "policy_version": get_scope_policy(identity["selection_context"]),
            "not_evaluated": ["payment", "requirements.proposal_documents"],
        }:
            raise CompletedReadReuseError("Prior scope coverage is incomplete")
    return bundle, context, request_hash


def _verify_parent_facts(conn, conv, identity):
    inventory = {row["document_uuid"]: row
                 for row in identity["selection_context"]["official_inventory"]}
    for code in dict.fromkeys(row["document_uuid"] for row in identity["document_set"]):
        parent = inventory[code]
        sha = _sha(parent.get("original_sha256"))
        row = conn.execute("""SELECT d.is_listed,d.source_url,dl.sha256_original,dl.bytes
            FROM prod2_documents d LEFT JOIN prod2_document_downloads dl
              ON dl.id_convocatoria=d.id_convocatoria AND dl.codigo_alfresco=d.codigo_alfresco
              AND dl.sha256_original=%s
            WHERE d.id_convocatoria=%s AND d.codigo_alfresco=%s""", (sha, conv, code)).fetchone()
        if (not row or row.get("is_listed") is not True
                or row.get("source_url") != parent["source_url"]
                or row.get("sha256_original") != sha or type(row.get("bytes")) is not int
                or row["bytes"] <= 0):
            raise CompletedReadReuseError("Prior originals lack current listed durable download facts")


def reuse_completed_read(conn, settings, folder, conv, profile, object_type,
                        selected, files, context, manifest):
    """Return an exact completed read or None for a genuinely different identity.

    Must run after current originals/selector are known, before conversion. A
    malformed/incomplete matching paid identity raises, never authorizes another
    payment. A known model/prompt/schema change is a new reading identity. The
    caller retains ownership of persistence, budgets and temporary cleanup.
    """
    try:
        conv, profile = _id(conv), _uuid(profile)
        if object_type not in {"good", "service"} or not isinstance(manifest, dict):
            raise CompletedReadReuseError("Current selected reading inputs are invalid")
        source_mib = manifest["max_source_mib"]
        if type(source_mib) is not int or source_mib <= 0:
            raise CompletedReadReuseError("Current source byte limit is invalid")
        identity = selected_preparation_identity(selected, context,
            max_source_bytes=source_mib * 1024**2,
            word_conversion_policy=manifest.get("word_conversion_policy"))
        _verify_originals(folder, identity, files)
        digest = canonical_hash(identity)
        rows = conn.execute("""SELECT ev.id AS evaluation_id,ev.id_convocatoria,ev.kind,ev.status,
            ev.model AS reserved_model,ev.policy_version,ev.profile_id,ev.request_hash,ev.result_json,
            ex.id AS extraction_id,ex.is_current,ex.quality,ex.model AS extraction_model,
            ex.raw_extraction_json,ex.sha256_original,ex.codigo_alfresco::text AS document_uuid,d.is_listed
            FROM prod2_evaluations ev
            LEFT JOIN prod2_document_extractions ex ON ex.evaluation_id=ev.id
              AND ex.id_convocatoria=ev.id_convocatoria
            LEFT JOIN prod2_documents d ON d.id_convocatoria=ex.id_convocatoria
              AND d.codigo_alfresco=ex.codigo_alfresco
            WHERE ev.id_convocatoria=%s AND ev.profile_id=%s AND ev.kind='gemini_extraction'
              AND ev.status='done' AND (ev.result_json->>'document_set_hash'=%s
                OR ev.result_json->'document_set_identity'=%s::jsonb
                OR (ev.result_json->'document_set'=%s::jsonb
                    AND ev.result_json->'selection_context'=%s::jsonb))
            ORDER BY ev.completed_at DESC,ev.id DESC""", (conv, profile, digest,
                json.dumps(identity, ensure_ascii=False, allow_nan=False),
                json.dumps(identity["document_set"], ensure_ascii=False, allow_nan=False),
                json.dumps(identity["selection_context"], ensure_ascii=False, allow_nan=False))).fetchall()
        if not rows:
            return _no_hit(conn, conv, profile)
        matching = []
        for row in rows:
            old = row.get("result_json", {}).get("document_set_identity")
            if isinstance(old, dict) and old != identity:
                normalized = selected_preparation_identity(old["document_set"], old["selection_context"],
                    max_source_bytes=old["max_source_bytes"], max_analysis_bytes=old["max_analysis_bytes"],
                    optimize_lossless=old["optimize_lossless"], word_conversion_policy=old.get("word_conversion_policy"))
                if old != normalized or row["result_json"].get("document_set_hash") != canonical_hash(old):
                    raise CompletedReadReuseError("Prior preparation policy is incomplete or contradictory")
                continue  # A verified, genuinely different conversion/resource policy.
            matching.append(row)
        if not matching:
            return _no_hit(conn, conv, profile)
        rows = matching
        rows = [row for row in rows if row.get("is_current") is True]
        if not rows:
            raise CompletedReadReuseError("Prior paid preparation has no current extraction; automatic repayment blocked")
        anchor = identity["document_set"][0]
        code = anchor["document_uuid"]
        sha = anchor.get("archive_sha256", anchor["original_sha256"])
        reusable = []
        for row in rows:
            bundle, old_context, request_hash = _durable_bundle(row, conv, profile, identity, code, sha)
            model, routing = resolve_llm1_model(manifest, old_context["analysis_page_count"], settings.gemini_model)
            current_settings = _model_settings(settings, model)
            if bundle.get("model") == "gpt-6-luna":
                from .luna_fallback import LUNA_FALLBACK_KEY, validate_luna_fallback_config
                from .luna_policy import luna_settings, validate_luna_bundle
                if LUNA_FALLBACK_KEY not in manifest:
                    continue
                validate_luna_fallback_config(manifest[LUNA_FALLBACK_KEY])
                validate_luna_bundle(bundle)
                current_settings = luna_settings(current_settings)
                model = "gpt-6-luna"
                routing = None
            document_context = {key: bundle[key] for key in _CONTEXT_FIELDS}
            if "llm1_input" in bundle:
                document_context["llm1_input"] = bundle["llm1_input"]
            source_manifest = old_context["source_document_manifest"]
            inputs = _document_input_from_verified_sha(current_settings, _sha(bundle["analysis_sha256"]),
                object_type, sha, source_manifest, document_context)
            if (bundle.get("model") != model or bundle.get("doc_class") != inputs["doc_class"]
                    or bundle.get("prompt_version") != inputs["prompt_version"]
                    or bundle.get("prompt_hash") != inputs["prompt_hash"]
                    or bundle.get("schema_version") != schema_version_for_scope(inputs["doc_class"], context)):
                continue  # Explicitly different current reading; not a weakened cache hit.
            expected = extraction_request_hash_from_verified_sha(current_settings, bundle["analysis_sha256"],
                object_type=object_type, original_sha256=sha, manifest=source_manifest,
                document_context=document_context)
            if expected != request_hash:
                raise CompletedReadReuseError("Prior request identity cannot be verified; automatic repayment blocked")
            reusable.append({"evaluation_id": row["evaluation_id"], "bundle": copy.deepcopy(bundle),
                "request_hash": request_hash, "model": model, "routing": routing,
                "page_count": old_context["analysis_page_count"], "document_set_hash": digest,
                "source_document_manifest": copy.deepcopy(source_manifest),
                # Historical paid bundles do not include these transport facts.
                # Page ranges cannot recover a combined PDF's size or splitting.
                "analysis_bytes": bundle.get("analysis_bytes"),
                "transport_parts": bundle.get("transport_parts")})
        if not reusable:
            return _no_hit(conn, conv, profile)
        if any(value["request_hash"] != reusable[0]["request_hash"]
               or value["bundle"] != reusable[0]["bundle"] for value in reusable[1:]):
            raise CompletedReadReuseError("Prior completed readings are ambiguous")
        _verify_parent_facts(conn, conv, identity)
        return reusable[0]
    except CompletedReadReuseError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise CompletedReadReuseError("Completed reading evidence is incomplete or changed") from error
