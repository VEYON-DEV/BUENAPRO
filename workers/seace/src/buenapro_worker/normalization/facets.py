from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _first_text(*candidates: Any) -> str:
    for candidate in candidates:
        text = _text(candidate)
        if text:
            return text
    return ""


def _humanize(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _truncate(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


_DAY_UNITS = r"d[ií]as?(?:\s+(?:calendario|h[aá]biles|naturales))?"
_MONTH_UNITS = r"mes(?:es)?|months?"
_WEEK_UNITS = r"semanas?|weeks?"


def parse_days(value: Any) -> int | None:
    """Extrae un plazo en dias desde numeros o texto libre tipo 'treinta (30) dias calendario'."""
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    text = _text(value).lower()
    if not text:
        return None

    day_match = re.search(rf"(\d+(?:[.,]\d+)?)\s*(?:\)|\s)*\s*(?:{_DAY_UNITS})", text)
    if day_match:
        return int(float(day_match.group(1).replace(",", ".")))

    month_match = re.search(rf"(\d+(?:[.,]\d+)?)\s*(?:\)|\s)*\s*(?:{_MONTH_UNITS})", text)
    if month_match:
        return int(float(month_match.group(1).replace(",", ".")) * 30)

    week_match = re.search(rf"(\d+(?:[.,]\d+)?)\s*(?:\)|\s)*\s*(?:{_WEEK_UNITS})", text)
    if week_match:
        return int(float(week_match.group(1).replace(",", ".")) * 7)

    return None


def derive_summary(raw: dict[str, Any]) -> dict[str, Any]:
    summary_raw = raw.get("summary")
    summary = as_dict(summary_raw)
    summary_text = _text(summary_raw)
    contract = as_dict(raw.get("contract"))
    execution = as_dict(raw.get("execution"))
    payment = as_dict(raw.get("payment")) or as_dict(execution.get("payment"))
    penalties = raw.get("penalties")

    deliverables = _deliverables(execution, payment)

    return {
        "descripcion_corta": _descripcion_corta(summary, summary_text, contract),
        "modalidad": _modalidad(contract, execution),
        "valor_estimado": _valor_estimado(summary, contract),
        "plazo_ejecucion_dias": _plazo_dias(summary, execution, contract),
        "tipo_pago": _tipo_pago(summary, payment, contract, summary_text),
        "entregables_count": len(deliverables),
        "penalidad_tope_pct": _penalty_cap(penalties),
        "roles_requeridos": _roles(raw, summary),
        "requirement_facets": sorted({facet["facet"] for facet in derive_facets(raw)}),
        "documentos_clave": _documents(raw),
        "observaciones_clave": as_list(summary.get("observaciones_clave") or summary.get("key_observations")),
    }


def _modalidad(contract: dict[str, Any], execution: dict[str, Any]) -> str:
    text = _text(contract.get("modalidad") or execution.get("modalidad")).lower()
    if not text:
        return "no_indicada"
    has_virtual = any(value in text for value in ("virtual", "remot", "distancia"))
    has_presential = any(value in text for value in ("presencial", "in situ", "campo"))
    if "mixt" in text or (has_virtual and has_presential):
        return "mixta"
    if has_virtual:
        return "virtual"
    if has_presential:
        return "presencial"
    return "no_indicada"


def _descripcion_corta(summary: dict[str, Any], summary_text: str, contract: dict[str, Any]) -> str | None:
    text = _first_text(
        summary.get("descripcion_corta"),
        summary.get("description"),
        summary_text,
        contract.get("objective"),
        contract.get("purpose"),
        contract.get("description"),
        contract.get("type"),
    )
    return _truncate(text) if text else None


def _valor_estimado(summary: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    candidate = as_dict(summary.get("valor_estimado"))
    monto = candidate.get("monto") if candidate else None
    if monto is None:
        for key in ("valor_estimado_monto", "valor_estimado", "estimated_value", "amount", "monto", "budget", "referential_value"):
            value = contract.get(key)
            if isinstance(value, (int, float)) and value > 0:
                monto = float(value)
                break
            parsed = _parse_money(_text(value))
            if parsed is not None:
                monto = parsed
                break
    return {
        "monto": monto,
        "moneda": candidate.get("moneda") or "PEN",
        "no_informado": monto is None,
    }


def _parse_money(text: str) -> float | None:
    if not text:
        return None
    match = re.search(r"S/\.?\s*([\d.,]+)", text)
    if not match:
        return None
    digits = match.group(1).replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return value if value > 0 else None


_PLAZO_TEXT_KEYS = (
    "plazo",
    "plazo_ejecucion",
    "term",
    "duration",
    "contract_duration",
    "period",
    "execution_period",
    "execution_term",
)


def _plazo_dias(summary: dict[str, Any], execution: dict[str, Any], contract: dict[str, Any]) -> int | None:
    direct = summary.get("plazo_ejecucion_dias") or execution.get("plazo_dias")
    if isinstance(direct, (int, float)) and direct > 0:
        return int(direct)

    for source in (execution, contract):
        for key in ("execution_period_days", "plazo_dias", "duration_days"):
            value = source.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        for key in ("execution_period_months", "duration_months"):
            value = source.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value * 30)
        for key in _PLAZO_TEXT_KEYS:
            days = parse_days(source.get(key))
            if days:
                return days
    return None


_TIPO_PAGO_VALUES = {"pago_unico", "armadas", "mensual", "por_entregable", "no_determinado"}


def _tipo_pago(
    summary: dict[str, Any],
    payment: dict[str, Any],
    contract: dict[str, Any],
    summary_text: str = "",
) -> str:
    declared = _text(summary.get("tipo_pago") or payment.get("tipo"))
    if declared in _TIPO_PAGO_VALUES:
        return declared

    per_deliverable = as_list(payment.get("payment_percentage_per_deliverable"))
    installments = as_list(payment.get("installments"))
    corpus = " ".join(
        filter(
            None,
            [
                _text(payment.get("frequency")),
                _text(payment.get("modality")),
                _text(payment.get("method")),
                _text(payment.get("schedule")),
                _text(payment.get("conditions")),
                _text(payment.get("timing")),
                _text(contract.get("payment_terms")),
                _text(contract.get("payment_method")),
                " ".join(_text(as_dict(item).get("description")) for item in installments),
                summary_text,
            ],
        )
    ).lower()

    if re.search(r"mensual|monthly|cada mes|por mes", corpus):
        return "mensual"
    if len(installments) > 1:
        return "armadas"
    if len(per_deliverable) > 1:
        return "por_entregable"
    if len(installments) == 1 or len(per_deliverable) == 1:
        return "pago_unico"
    if re.search(r"armada", corpus):
        return "armadas"
    if re.search(r"pago [uú]nico|[uú]nico pago|un solo pago|single payment", corpus):
        return "pago_unico"
    if re.search(r"entregable|deliverable|prestaci[oó]n parcial|pago parcial", corpus):
        return "por_entregable"
    if re.search(r"100\s*%", corpus):
        return "pago_unico"
    return "no_determinado"


_DELIVERABLE_KEYS = ("entregables", "deliverables", "productos", "products")


def _deliverables(execution: dict[str, Any], payment: dict[str, Any]) -> list[Any]:
    for key in _DELIVERABLE_KEYS:
        value = execution.get(key)
        if isinstance(value, list) and value:
            return value
    per_deliverable = payment.get("payment_percentage_per_deliverable")
    if isinstance(per_deliverable, list) and per_deliverable:
        return per_deliverable
    return []


def _iter_penalty_items(penalties: Any) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(penalties, list):
        for entry in penalties:
            if _is_no_aplica(entry):
                continue
            if isinstance(entry, dict) and str(entry.get("tipo")) in _PENALTY_LABELS:
                items.append((_penalty_label(str(entry["tipo"])), entry))
                continue
            label = _label(entry, "Penalidad")
            items.append((label, entry))
        return items
    if isinstance(penalties, dict):
        for key, value in penalties.items():
            if _is_no_aplica(value):
                continue
            if isinstance(value, list):
                for entry in value:
                    if _is_no_aplica(entry):
                        continue
                    items.append((_label(entry, _penalty_label(key)), entry))
            else:
                items.append((_penalty_label(key), value))
    return items


_PENALTY_LABELS = {
    "late_delivery": "Penalidad por mora",
    "late_execution": "Penalidad por mora",
    "delay": "Penalidad por mora",
    "mora": "Penalidad por mora",
    "other_penalties": "Otra penalidad",
    "other": "Otra penalidad",
    "otra": "Otra penalidad",
    "breach_of_contract": "Resolucion de contrato",
    "resolucion": "Resolucion de contrato",
}


def _penalty_label(key: str) -> str:
    return _PENALTY_LABELS.get(key, _humanize(key))


_CAP_KEY_PATTERN = re.compile(r"cap|tope|limite|l[ií]mite|max", re.IGNORECASE)
_PCT_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_CAP_CONTEXT_PATTERN = re.compile(r"tope|m[aá]xim|cap|no exceder|hasta el|limit", re.IGNORECASE)


def _penalty_cap(penalties: Any) -> float | None:
    caps: list[float] = []

    def scan(value: Any, key_hint: str = "") -> None:
        if isinstance(value, dict):
            for key, entry in value.items():
                scan(entry, key)
            return
        if isinstance(value, list):
            for entry in value:
                scan(entry, key_hint)
            return
        if isinstance(value, (int, float)):
            if _CAP_KEY_PATTERN.search(key_hint) and 0 < float(value) <= 100:
                caps.append(float(value))
            return
        text = _text(value)
        if not text:
            return
        match = _PCT_PATTERN.search(text)
        if not match:
            return
        if _CAP_KEY_PATTERN.search(key_hint) or _CAP_CONTEXT_PATTERN.search(text):
            try:
                pct = float(match.group(1).replace(",", "."))
            except ValueError:
                return
            if 0 < pct <= 100:
                caps.append(pct)

    scan(penalties)
    return max(caps) if caps else None


def derive_facets(raw: dict[str, Any]) -> list[dict[str, Any]]:
    requirements = as_dict(raw.get("requirements"))
    facets: list[dict[str, Any]] = []

    facets.extend(_provider_facets(requirements.get("provider")))
    facets.extend(_personnel_facets(requirements.get("key_personnel")))
    facets.extend(_named_facets("equipment", "Equipamiento", requirements.get("equipment")))
    facets.extend(_insurance_facets(requirements.get("insurance")))

    for item in as_list(requirements.get("proposal_documents")):
        facets.append(_facet("proposal_document", _label(item, "Documento de propuesta"), item))

    for item in as_list(requirements.get("training")):
        facets.append(_facet("training", _label(item, "Capacitacion"), item))

    for item in as_list(requirements.get("licenses")) + as_list(requirements.get("license")):
        facets.append(_facet("license", _label(item, "Licencia"), item))

    for item in as_list(requirements.get("economic_experience")):
        facets.append(_facet("economic_experience", _label(item, "Experiencia economica"), item))

    for item in as_list(requirements.get("specific_experience")):
        facets.append(_facet("specific_experience", _label(item, "Experiencia especifica"), item))

    for label, item in _iter_penalty_items(raw.get("penalties")):
        facets.append(_facet("penalty_condition", label, item, required=False))

    return facets


_PROVIDER_CATEGORY_FACETS: dict[str, tuple[str, str]] = {
    "legal_capacity": ("legal_capacity", "Capacidad legal"),
    "ruc": ("ruc_status", "RUC"),
    "rnp": ("rnp", "RNP"),
    "economic_experience": ("economic_experience", "Experiencia economica"),
    "general_experience": ("general_experience", "Experiencia general"),
    "specific_experience": ("specific_experience", "Experiencia especifica"),
    "experience": ("general_experience", "Experiencia"),
    "academic_training": ("education", "Formacion academica"),
    "education": ("education", "Formacion academica"),
    "specialized_training": ("training", "Capacitacion especializada"),
    "training": ("training", "Capacitacion"),
    "professional_registration": ("professional_registration", "Colegiatura y habilidad"),
    "licenses": ("license", "Licencia"),
    "license": ("license", "Licencia"),
    "equipment": ("equipment", "Equipamiento"),
    "insurance": ("insurance", "Seguro"),
    "certifications": ("company_certification", "Certificacion"),
    "technical_professional_capacity": ("general_experience", "Capacidad tecnica y profesional"),
    "strategic_infrastructure": ("equipment", "Infraestructura estrategica"),
    "proposal_documents": ("proposal_document", "Documento de propuesta"),
}


_FACET_TAXONOMY = {
    "legal_capacity",
    "ruc_status",
    "rnp",
    "not_disqualified",
    "cci",
    "business_line",
    "economic_experience",
    "general_experience",
    "specific_experience",
    "professional_registration",
    "education",
    "training",
    "license",
    "company_certification",
    "equipment",
    "insurance",
    "other",
}


def _provider_facets(provider: Any) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    if isinstance(provider, list):
        for item in provider:
            # Schema v2: items tipados {tipo, descripcion, obligatorio, monto}
            if isinstance(item, dict) and item.get("tipo") in _FACET_TAXONOMY:
                details = dict(item)
                if details.get("monto") is not None:
                    details.setdefault("amount", details["monto"])
                facets.append(
                    _facet(
                        str(item["tipo"]),
                        _truncate(_text(item.get("descripcion")) or _humanize(str(item["tipo"])), 90),
                        details,
                        required=bool(item.get("obligatorio", True)),
                    )
                )
                continue
            facets.append(_facet("legal_capacity", _label(item, "Requisito del proveedor"), item))
        return facets

    for key, value in as_dict(provider).items():
        if value in (None, "", [], {}):
            continue
        facet_name, base_label = _PROVIDER_CATEGORY_FACETS.get(key, ("other", _humanize(key)))
        if isinstance(value, list):
            for item in value:
                facets.append(_facet(facet_name, _label(item, base_label), item))
        else:
            facets.append(_facet(facet_name, base_label, value))
    return facets


def _personnel_facets(personnel: Any) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    if isinstance(personnel, list):
        for item in personnel:
            facets.append(_facet("key_personnel", _label(item, "Personal clave"), item))
        return facets

    entries = as_dict(personnel)
    if not entries:
        return facets
    if _looks_like_single_person(entries):
        facets.append(_facet("key_personnel", _label(entries, "Personal clave"), entries))
        return facets
    for key, value in entries.items():
        label = _label(value, _humanize(key))
        facets.append(_facet("key_personnel", label, value if isinstance(value, dict) else {"value": value}))
    return facets


_PERSON_FIELDS = {"role", "cargo", "profile", "academic_training", "experience", "specialized_training", "education"}


def _looks_like_single_person(entries: dict[str, Any]) -> bool:
    return bool(_PERSON_FIELDS.intersection(entries.keys()))


def _named_facets(facet_name: str, base_label: str, value: Any) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, entry in value.items():
            if entry in (None, "", [], {}):
                continue
            facets.append(_facet(facet_name, _label(entry, _humanize(key)), entry if isinstance(entry, dict) else {"value": entry}))
        return facets
    for item in as_list(value):
        facets.append(_facet(facet_name, _label(item, base_label), item))
    return facets


_INSURANCE_LABELS = {
    "sctr": "SCTR",
    "soat": "SOAT",
    "vehicle_all_risk": "Seguro vehicular todo riesgo",
    "liability": "Responsabilidad civil",
}


def _insurance_facets(value: Any) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, entry in value.items():
            if entry in (None, "", [], {}):
                continue
            label = _INSURANCE_LABELS.get(key, _humanize(key))
            details = entry if isinstance(entry, dict) else {"value": entry}
            required = not _is_no_aplica(entry)
            facets.append(_facet("insurance", label, details, required=required))
        return facets
    for item in as_list(value):
        facets.append(_facet("insurance", _label(item, "Seguro"), item, required=not _is_no_aplica(item)))
    return facets


def _is_no_aplica(value: Any) -> bool:
    return bool(re.search(r"no\s+(aplica|corresponde)", _text(value), re.IGNORECASE))


def facet_hash(facet: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "facet": facet["facet"],
            "label": facet["label"],
            "required": facet["required"],
            "details": facet["details"],
        }
    )


def _facet(name: str, label: str, details: Any, *, required: bool = True) -> dict[str, Any]:
    details_json = details if isinstance(details, dict) else {"value": details}
    evidence = as_list(details_json.get("evidence") or details_json.get("texto_original"))
    return {
        "facet": name,
        "label": _truncate(label, 90),
        "required": bool(details_json.get("required", required)),
        "details": details_json,
        "evidence": evidence,
    }


def _label(item: Any, fallback: str) -> str:
    if isinstance(item, dict):
        return str(
            item.get("label")
            or item.get("role")
            or item.get("rol")
            or item.get("name")
            or item.get("nombre")
            or item.get("tipo")
            or fallback
        )
    text = _text(item)
    if text:
        return _truncate(text, 90)
    return fallback


def _roles(raw: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    roles: list[str] = []

    declared = summary.get("roles_requeridos") or summary.get("required_roles")
    for item in as_list(declared):
        text = _text(item)
        if text:
            roles.append(text)

    personnel = as_dict(raw.get("requirements")).get("key_personnel")
    if isinstance(personnel, list):
        for item in personnel:
            if isinstance(item, dict):
                role = item.get("role") or item.get("rol") or item.get("cargo") or item.get("label") or item.get("name")
                if role:
                    roles.append(str(role))
    elif isinstance(personnel, dict):
        if _looks_like_single_person(personnel):
            role = personnel.get("role") or personnel.get("cargo") or personnel.get("label") or personnel.get("name")
            if role:
                roles.append(str(role))
        else:
            for key, value in personnel.items():
                role = value.get("role") if isinstance(value, dict) else None
                roles.append(str(role) if role else _humanize(key))

    seen: set[str] = set()
    unique: list[str] = []
    for role in roles:
        normalized = role.strip()
        if normalized and normalized.lower() not in seen:
            seen.add(normalized.lower())
            unique.append(normalized)
    return unique


def _documents(raw: dict[str, Any]) -> list[str]:
    requirements = as_dict(raw.get("requirements"))
    docs = as_list(requirements.get("proposal_documents"))
    if not docs:
        docs = as_list(as_dict(requirements.get("provider")).get("proposal_documents"))
    result = []
    for item in docs:
        if isinstance(item, dict):
            result.append(_truncate(str(item.get("label") or item.get("name") or item.get("tipo") or item), 120))
        else:
            text = _text(item)
            if text:
                result.append(_truncate(text, 120))
    return result
