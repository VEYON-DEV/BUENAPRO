"""Deterministic UI categories projected from evidence-validated LLM1 v1 labels."""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
from typing import Any

from pydantic import ValidationError

from buenapro_worker.extraction.commercial_classification import (
    AREA_IDS, SPECIALTY_IDS, TAXONOMY_VERSION, WORK_TYPE_IDS,
    normalize_commercial_classification,
)

_CATEGORY_IDS = ("licencias_suscripciones", "desarrollo_software", "ia_automatizacion",
                 "industrial", "cloud_infraestructura", "soporte_mantenimiento")


@lru_cache(maxsize=1)
def _categories() -> tuple[dict, ...]:
    try:
        catalog = json.loads(files("buenapro_worker.extraction").joinpath(
            "commercial_categories.v1.json").read_text(encoding="utf-8"))
        if (not isinstance(catalog, dict) or catalog.get("version") != "commercial_categories_v1"
                or catalog.get("classification_taxonomy_version") != TAXONOMY_VERSION):
            return ()
        rows, seen = catalog.get("categories"), set()
        if not isinstance(rows, list):
            return ()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or row["id"] not in _CATEGORY_IDS or row["id"] in seen):
                return ()
            seen.add(row["id"])
            rules = row.get("match_any")
            if not isinstance(rules, dict) or set(rules) != {"areas", "work_types", "specialties"}:
                return ()
            for axis, allowed in (("areas", AREA_IDS), ("work_types", WORK_TYPE_IDS),
                                  ("specialties", SPECIALTY_IDS)):
                if (not isinstance(rules[axis], list)
                        or any(value not in allowed for value in rules[axis])):
                    return ()
        return tuple(rows) if tuple(row["id"] for row in rows) == _CATEGORY_IDS else ()
    except (OSError, TypeError, ValueError):
        return ()


def derive_commercial_categories(classification: Any) -> list[str]:
    """No guessing, model calls, scores, or changes to paid taxonomy/prompt IDs."""
    try:
        block = normalize_commercial_classification(classification)
    except ValidationError:
        return []
    if block["primary_area"] == "sin_clasificar":
        return []
    selected = {"areas": {block["primary_area"], *block["secondary_areas"]},
                "work_types": set(block["work_types"]), "specialties": set(block["specialties"])}
    return [row["id"] for row in _categories()
            if any(selected[axis].intersection(row["match_any"][axis]) for axis in selected)]
