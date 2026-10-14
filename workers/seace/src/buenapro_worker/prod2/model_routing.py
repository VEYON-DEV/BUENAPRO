"""Choose LLM1 by the complete prepared PDF's pages, before transport splitting.

The versioned policy belongs to a run's immutable manifest. Runs that predate
the policy keep their original configured model and receipt identity.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MODEL_ROUTING_POLICY = "prod2_llm1_page_routing_v1"
_SMALL_MODEL = "gemini-3.1-flash-lite"
_LARGE_MODEL = "gemini-2.5-flash-lite"
_POLICY_FIELDS = frozenset({"policy_version", "page_threshold", "small_model", "large_model"})
_MODELS = frozenset({_SMALL_MODEL, _LARGE_MODEL})


class ModelRoutingError(ValueError):
    """A frozen page-routing policy or its complete page count is invalid."""


def default_model_routing() -> dict[str, Any]:
    """Return a fresh policy for newly planned runs; never mutate old manifests."""
    return {
        "policy_version": MODEL_ROUTING_POLICY,
        "page_threshold": 20,
        "small_model": _SMALL_MODEL,
        "large_model": _LARGE_MODEL,
    }


def _bounded_pages(value: Any, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ModelRoutingError(f"{name} must be an integer between 1 and 1000")
    return value


def resolve_llm1_model(
    manifest: Mapping[str, Any], page_count: int, base_model: str,
) -> tuple[str, dict[str, Any] | None]:
    """Return the exact model and optional routing proof for this document set.

    ``page_count`` is the total after preparation and deduplication, not the number
    of pages in a transport part. Legacy manifests deliberately bypass the page
    policy so their historical model and receipts remain unchanged.
    """
    if not isinstance(manifest, Mapping):
        raise ModelRoutingError("Run manifest must be a mapping")
    if not isinstance(base_model, str) or not base_model.strip():
        raise ModelRoutingError("Base model must be a nonempty model identifier")
    if "llm1_model_routing" not in manifest:
        return base_model, None

    policy = manifest["llm1_model_routing"]
    if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
        raise ModelRoutingError("LLM1 model routing policy fields are invalid")
    if policy["policy_version"] != MODEL_ROUTING_POLICY:
        raise ModelRoutingError("LLM1 model routing policy version is unsupported")
    if policy["small_model"] != _SMALL_MODEL or policy["large_model"] != _LARGE_MODEL:
        raise ModelRoutingError("LLM1 model routing model identifiers are unsupported")
    if base_model not in _MODELS:
        raise ModelRoutingError("Base model is unsupported by the page-routing policy")
    threshold = _bounded_pages(policy["page_threshold"], "Page threshold")
    total_pages = _bounded_pages(page_count, "Complete PDF page count")
    model = policy["large_model"] if total_pages > threshold else policy["small_model"]
    return model, {
        "policy_version": MODEL_ROUTING_POLICY,
        "page_threshold": threshold,
        "page_count": total_pages,
        "model": model,
    }
