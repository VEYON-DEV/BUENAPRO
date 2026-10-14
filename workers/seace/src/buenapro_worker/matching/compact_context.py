"""Local projection for LLM2. No inference, OCR or changes to stored evidence."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from buenapro_worker.matching.profile_evidence import evidence_only_profile

COMPACT_PROFILE_VERSION = "compact_match_context_v1"


def _record(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _pick(value: dict, keys: tuple[str, ...]) -> dict:
    return {key: value[key] for key in keys if key in value}


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    return re.sub(r"\s+", " ", "".join(c for c in text if not unicodedata.combining(c)).lower().replace("_", " ").replace(".", " ")).strip()


EXPERIENCE_KEYS = ("numero_contrato", "objeto", "tipo_objeto", "rubro", "especialidad", "entidad", "fecha_inicio", "fecha_fin", "fecha_conformidad", "anio", "monto", "moneda", "conformidad", "modalidad_participacion", "porcentaje_participacion")
AMOUNT_KEYS = ("amount_decimal", "literal", "currency", "label", "tax_basis", "source_type", "quote", "page")
FACT_KEYS = ("field", "value", "quote", "page")
AREA_KEYS = ("monto", "moneda", "rubro", "especialidad", "tipo_objeto", "descripcion", "documents", "status", "no_global_sum", "source_record_index")


def build_compact_match_profile(value: dict) -> dict:
    profile = evidence_only_profile(value)
    backed_ids: set[str] = set()
    experiences = []
    for value in _list(profile.get("experience_json")):
        experience = _record(value)
        summary = _record(experience.get("documentary_summary"))
        sources = []
        for value in _list(summary.get("sources")):
            source = _record(value)
            extraction = _record(source.get("extraction"))
            if isinstance(source.get("document_id"), str):
                backed_ids.add(source["document_id"])
            amounts = [_pick(_record(item), AMOUNT_KEYS) for item in _list(extraction.get("amounts"))]
            unique = []
            for amount in amounts:
                if amount not in unique:
                    unique.append(amount)
            principal = extraction.get("principal_amount_index")
            principal_amount = amounts[principal] if isinstance(principal, int) and 0 <= principal < len(amounts) else None
            projected = _pick(extraction, ("summary", "conformity", "warnings", "unknowns", "document_types"))
            projected.update(
                classification=_pick(_record(extraction.get("classification")), ("rubro", "specialty", "inferred", "quote", "page")),
                amounts=unique,
                principal_amount_index=unique.index(principal_amount) if principal_amount in unique else None,
                # Preserve all facts: acceptance, partial execution and duration
                # are often labelled without the words "fecha"/"conformidad".
                facts=[_pick(_record(fact), FACT_KEYS) for fact in _list(extraction.get("facts"))],
            )
            sources.append({**_pick(source, ("document_id", "sha256", "status")), "extraction": projected})
        item = _pick(experience, EXPERIENCE_KEYS)
        if sources:
            # Long legacy declarations and generated descriptions duplicate the
            # extracted facts and must not masquerade as documentary evidence.
            item["documentary_summary"] = {
                **_pick(summary, ("status", "no_legal_accreditation", "amount_review_required", "missing_document_ids")),
                "sources": sources,
            }
        else:
            item.update(_pick(experience, ("actividades", "alcance_participacion", "descripcion")))
        experiences.append(item)
    areas = []
    for value in _list(_record(profile.get("econ_experience_json")).get("areas")):
        area = _record(value)
        index = area.get("source_record_index")
        indexed = _record(experiences[index]) if isinstance(index, int) and 0 <= index < len(experiences) else {}
        ids = [str(_record(doc).get("id") or doc) for doc in _list(area.get("documents"))]
        if _list(_record(indexed.get("documentary_summary")).get("sources")) or (ids and all(doc in backed_ids for doc in ids)):
            continue
        areas.append(_pick(area, AREA_KEYS))
    return {
        **_pick(profile, ("razon_social", "ruc", "team_json", "equipment_json", "certifications_json", "hireable_roles_json")),
        "identity_json": _pick(_record(profile.get("identity_json")), ("rnp", "cci", "website", "commercial_name", "ruc_source", "services_declared_by_user", "capabilities_source")),
        "experience_json": experiences,
        "econ_experience_json": {"areas": areas, "no_global_sum": True, "currency_conversion": False},
    }


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
