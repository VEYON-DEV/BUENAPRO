"""Only already-read, usable source snapshots may be submitted to LLM2."""
from __future__ import annotations

from buenapro_worker.matching.substantive_policy import split_match_requirements


def match_readiness(source: dict | None, facets: list[dict], *, provisional_source_review: bool = False) -> tuple[bool, str]:
    if not source or source.get("extraction_id") is None:
        return False, "pending_reading"
    if source.get("is_current") is not True:
        return False, "stale_reading"
    if source.get("quality") not in {"auto", "reviewed", "corrected"}:
        return False, "failed_or_unknown_reading"
    if source.get("requires_human_review") is not False and not (provisional_source_review and source.get("requires_human_review") is True):
        return False, "reading_requires_review"
    if not isinstance(source.get("raw_extraction_json"), dict) or not source["raw_extraction_json"]:
        return False, "empty_reading"
    if not isinstance(source.get("summary_json"), dict) or not source["summary_json"]:
        return False, "empty_summary"
    if not facets or any(facet.get("extraction_id") != source["extraction_id"] or facet.get("is_current") is not True for facet in facets):
        return False, "missing_or_unbound_requirements"
    substantive, _ = split_match_requirements(facets)
    if not any(requirement["obligatorio"] for requirement in substantive):
        return False, "no_required_substantive_requirements"
    return True, ("provisional_existing_reading_source_review_preserved" if source["requires_human_review"] else "ready_from_existing_reading")


def require_match_ready(source: dict | None, facets: list[dict], *, provisional_source_review: bool = False) -> None:
    ready, reason = match_readiness(source, facets, provisional_source_review=provisional_source_review)
    if not ready:
        raise ValueError(f"LLM2 source excluded before dispatch: {reason}")
