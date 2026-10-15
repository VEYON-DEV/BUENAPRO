"""Strict Google responseJsonSchema for a frozen shared recommendation batch.

Pure construction only: no DB, filesystem, network or provider imports. The
schema covers every case and requirement at its exact original array position.
"""
from __future__ import annotations

import re


def _document_refs(profile: object) -> list[str]:
    if not isinstance(profile, dict):
        raise ValueError("Recommendation profile must be an object")
    columns, documents = profile.get("document_columns"), profile.get("documents")
    if (not isinstance(columns, list) or not all(isinstance(column, str) for column in columns)
            or len(columns) != len(set(columns)) or "ref" not in columns
            or not isinstance(documents, list)):
        raise ValueError("Invalid columnar documentary profile")
    index = columns.index("ref")
    refs = []
    for row in documents:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("Document row does not match document_columns")
        alias = row[index]
        if not isinstance(alias, str) or not re.fullmatch(r"D[1-9][0-9]*", alias):
            raise ValueError("Invalid documentary alias")
        if alias in refs:
            raise ValueError("Duplicate documentary alias")
        refs.append(alias)
    return refs


def _strict_object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _exact_array(schemas: list[dict]) -> dict:
    # Exact length also prohibits any trailing tuple item. Omit boolean `items`
    # and `unevaluatedItems`, which are outside Google's documented subset.
    result = {"type": "array", "minItems": len(schemas), "maxItems": len(schemas)}
    if schemas:
        result["prefixItems"] = schemas
    return result


def schema_from_contexts(contexts: dict[str, dict]) -> dict:
    """Return the JSON schema value for generationConfig.responseJsonSchema.

    Input is the mapping passed to build_recommendation_batch: each value has
    profile.document_columns/documents plus evaluation.requirements. Shared
    profile identity is checked before a schema can silently bind other sources.
    """
    if not isinstance(contexts, dict) or not 1 <= len(contexts) <= 5:
        raise ValueError("Recommendation schema requires one to five cases")
    case_schemas = []
    first_profile = None
    for case_key, context in contexts.items():
        if (not isinstance(case_key, str) or not re.fullmatch(r"seace_prod[26]-[1-9][0-9]*", case_key)
                or not isinstance(context, dict)):
            raise ValueError("Invalid stable source case_key")
        profile = context.get("profile")
        refs = _document_refs(profile)
        if first_profile is None:
            first_profile = profile
        elif profile != first_profile:
            raise ValueError("Shared recommendation batch profiles differ")
        evaluation = context.get("evaluation")
        requirements = evaluation.get("requirements") if isinstance(evaluation, dict) else None
        if not isinstance(requirements, list) or not requirements:
            raise ValueError("Each recommendation case requires explicit requirements")
        positions = []
        seen = set()
        for requirement in requirements:
            source_index = requirement.get("source_index") if isinstance(requirement, dict) else None
            if type(source_index) is not int or source_index < 1 or source_index in seen:
                raise ValueError("Invalid or duplicate requirement source_index")
            seen.add(source_index)
            reference_items = {"type": "string", "enum": list(refs)} if refs else {"type": "string"}
            positions.append(_strict_object({
                "source_index": {"type": "integer", "enum": [source_index]},
                "evidencia": {"type": "string"},
                "brecha": {"type": "string"},
                "accion": {"type": "string"},
                "evidence_refs": {"type": "array", "items": reference_items,
                                  "minItems": 0, "maxItems": len(refs)},
            }))
        case_schemas.append(_strict_object({
            "case_key": {"type": "string", "enum": [case_key]},
            "resumen": {"type": "string"},
            "requisitos": _exact_array(positions),
            "acciones_recomendadas": {"type": "array", "items": {"type": "string"}},
        }))
    return _strict_object({"evaluations": _exact_array(case_schemas)})
