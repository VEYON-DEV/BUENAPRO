"""Exact identity for the explicitly configured PROD2 Luna backup.

The existing gemini_extraction ledger kind is retained for compatibility;
provider, transport, model and request parameters are independently bound.
"""
from __future__ import annotations

import copy
import json

LUNA_SELECTED_TRANSPORT_POLICY = "prod2_selected_luna_responses_v1"
LUNA_REQUEST_POLICY = {"provider": "openai", "model": "gpt-6-luna",
    "reasoning_effort": "low", "max_output_tokens": 65536,
    "response_format": "json_object", "store": False, "service_tier": "default"}


def provider_instruction(prompt, schema):
    value = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema
    return prompt + "\nDevuelve un único objeto JSON válido conforme a este esquema. " \
        "No añadas Markdown ni texto fuera del JSON.\n" + json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def provider_model(settings):
    provider = getattr(settings, "prod2_llm1_provider", "gemini")
    if provider not in {"gemini", "openai"}:
        raise ValueError("Unsupported PROD2 LLM1 provider")
    return LUNA_REQUEST_POLICY["model"] if provider == "openai" else settings.gemini_model


def luna_settings(settings):
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update={"prod2_llm1_provider": "openai"})
    result = copy.copy(settings)
    result.prod2_llm1_provider = "openai"
    return result


def validate_luna_bundle(bundle):
    """Verify the complete paid identity, not merely a model-name allowlist."""
    raw = bundle.get("raw_extraction_json")
    from buenapro_worker.normalization.facets import canonical_hash
    if (bundle.get("model") != LUNA_REQUEST_POLICY["model"] or not isinstance(raw, dict)
            or bundle.get("doc_class") not in {"bases_good", "bases_service"}
            or canonical_hash(bundle.get("provider_request")) != canonical_hash(LUNA_REQUEST_POLICY)
            or canonical_hash(raw.get("provider_request")) != canonical_hash(LUNA_REQUEST_POLICY)
            or bundle.get("selected_transport_policy") != LUNA_SELECTED_TRANSPORT_POLICY
            or raw.get("selected_transport_policy") != LUNA_SELECTED_TRANSPORT_POLICY):
        raise ValueError("Luna provider provenance differs from the configured policy")
    from types import SimpleNamespace
    from .analysis import _document_input_from_verified_sha
    context = {key: bundle[key] for key in ("document_scope", "document_set",
        "document_set_hash", "document_set_identity", "selection_context")}
    if "llm1_input" in bundle:
        context["llm1_input"] = bundle["llm1_input"]
    inputs = _document_input_from_verified_sha(SimpleNamespace(prod2_llm1_provider="openai"),
        bundle["analysis_sha256"], "good" if bundle["doc_class"] == "bases_good" else "service",
        bundle["original_sha256"], raw["source_document_manifest"], context)
    if (bundle.get("cache_key") != "prod2:llm1:" + canonical_hash(inputs)
            or bundle.get("prompt_hash") != inputs["prompt_hash"]
            or bundle.get("prompt_version") != inputs["prompt_version"]):
        raise ValueError("Luna receipt does not bind its exact request")
