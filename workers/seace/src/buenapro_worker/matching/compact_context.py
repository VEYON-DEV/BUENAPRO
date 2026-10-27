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


# LLM2 sees the experiences closest to the contract, each one complete. A profile with up to this
# many is sent whole; DIN had 43 (~35 000 tokens per evaluation), VEYON 2.
MATCH_EXPERIENCE_LIMIT = 8
# Words every procurement text shares; they say nothing about which experience fits.
_GENERIC_TERMS = frozenset("""
    para como entre sobre desde hasta segun donde cuando todo toda todos todas este esta estos estas
    servicio servicios adquisicion contratacion suministro bienes compra contrato contratos entidad
    empresa proveedor requerimiento requerimientos tecnico tecnica tecnicos tecnicas general generales
    experiencia minima minimo acreditar acredita documento documentos copia copias similares similar
""".split())


def _terms(*values: Any) -> set[str]:
    return {word for value in values for word in re.findall(r"[a-z0-9]+", normalized(value))
            if len(word) >= 4 and word not in _GENERIC_TERMS}


def _experience_terms(experience: dict) -> tuple[set[str], set[str]]:
    main = _terms(*(experience.get(key) for key in ("objeto", "especialidad", "rubro", "tipo_objeto")))
    documents = set()
    for source in _list(_record(experience.get("documentary_summary")).get("sources")):
        extraction = _record(_record(source).get("extraction"))
        documents |= _terms(extraction.get("summary"), _record(extraction.get("classification")).get("specialty"))
    return main, documents - main


def select_relevant_experience(compact: dict, oportunidad: dict, requisitos: list[dict],
                               limit: int = MATCH_EXPERIENCE_LIMIT) -> dict:
    """Keep the ``limit`` experiences that share most words with the contract, complete and in order.

    A word in the experience's object, specialty or sector counts twice; one in its documents once;
    a tie goes to the most recent year. Economic areas follow their experience (an index pointing to
    an experience that was left out becomes None), and a note tells the model that more exist.
    """
    experiences = _list(compact.get("experience_json"))
    if len(experiences) <= limit:
        return compact
    resumen = _record(oportunidad.get("resumen"))
    wanted = _terms(oportunidad.get("descripcion"), resumen.get("descripcion_corta"), resumen.get("objeto"),
                    *(requisito.get("requisito") for requisito in requisitos))

    def rank(index: int) -> tuple:
        main, documents = _experience_terms(_record(experiences[index]))
        year = str(_record(experiences[index]).get("anio") or "")
        return (-(2 * len(wanted & main) + len(wanted & documents)), -int(year) if year.isdigit() else 0, index)

    kept = sorted(sorted(range(len(experiences)), key=rank)[:limit])
    position = {old: new for new, old in enumerate(kept)}
    econ = _record(compact.get("econ_experience_json"))
    areas = []
    for value in _list(econ.get("areas")):
        area = dict(_record(value))
        if isinstance(area.get("source_record_index"), int):
            area["source_record_index"] = position.get(area["source_record_index"])
        areas.append(area)
    return {
        **compact,
        "experience_json": [experiences[index] for index in kept],
        "experience_note": (f"Se incluyen las {len(kept)} experiencias más relacionadas con esta contratación "
                            f"de {len(experiences)} registradas por la empresa."),
        "econ_experience_json": {**econ, "areas": areas},
    }


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
