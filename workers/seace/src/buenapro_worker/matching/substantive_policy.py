"""Submission paperwork is a checklist, not a missing business capability."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from buenapro_worker.matching.compact_context import normalized

MATCH_POLICY_VERSION = "match_analysis_v5_concrete_evidence"


class ModelOutputMismatch(ValueError):
    """The model's judgements cannot be bound to the requirements it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_administrative_requirement(row: dict) -> bool:
    kind = str(row.get("tipo") or row.get("facet") or row.get("categoria") or "")
    label = normalized(row.get("requisito") or row.get("label"))
    if re.fullmatch(r"persona natural(?: y/o| o| y)? juridica[ .]*|persona natural y/o juridica[ .]*", label):
        return True
    if re.search(r"^carta de presentacion\b|\bpacto de integridad\b|\bno incurrir en nepotismo\b", label):
        return True
    if kind in {"rnp", "ruc_status", "not_disqualified", "cci"}:
        return True
    if kind == "legal_capacity" and re.fullmatch(r"(?:empresa|persona juridica) dedicada al rubro", label):
        return True
    if re.search(r"\b(?:rnp|ruc|cci)\b|registro (?:nacional de proveedores|unico de contribuyente)|impedimento para contratar|no (?:tener|contar con) impedimento|declaracion jurada|declaraciones juradas", label):
        return True
    if re.search(r"^(?:formato|anexo|cotizacion|propuesta economica|oferta economica)\b", label):
        return True
    return bool(re.search(r"copia simple.*(?:constancia|documento de identidad)|documentos? (?:que )?acredit(?:en|e).*|documentacion que acredite", label))


def split_match_requirements(facets: list[dict]) -> tuple[list[dict], list[dict]]:
    substantive, checklist = [], []
    for index, row in enumerate(facets, 1):
        if row.get("facet") == "penalty_condition":
            continue
        if is_administrative_requirement(row):
            checklist.append({"tipo": row["facet"], "requisito": row["label"], "estado": "preparar", "score_impact": False})
        else:
            substantive.append({"source_index": index, "tipo": row["facet"], "requisito": row["label"],
                                "obligatorio": row["required"], "detalle": row["details_json"]})
    return substantive, checklist


SOURCE_CATEGORIES = {
    "economic_experience": "experiencia_economica", "general_experience": "experiencia",
    "specific_experience": "experiencia", "key_personnel": "personal_clave", "education": "formacion",
    "training": "formacion", "equipment": "equipamiento", "insurance": "seguro", "license": "licencia",
    "company_certification": "certificacion", "professional_registration": "certificacion",
    "proposal_document": "documentacion", "legal_capacity": "identidad", "other": "otro",
}


def bind_substantive_requirements(results: list[dict], expected: list[dict], compact_profile: dict) -> list[dict]:
    """Bind every judgement to a source requirement and existing evidence IDs."""
    if [r.get("source_index") for r in results] != [r["source_index"] for r in expected]:
        raise ModelOutputMismatch("requirement_coverage_changed", "Substantive requirement coverage/order changed")
    ids = {str(s["document_id"]) for e in compact_profile.get("experience_json", [])
           for s in e.get("documentary_summary", {}).get("sources", [])}
    for result, source in zip(results, expected, strict=True):
        refs = result.get("evidence_refs") or []
        if not set(refs) <= ids:
            raise ModelOutputMismatch("unknown_evidence_reference", "Unknown company evidence reference")
        result["source_facet"] = source["tipo"]
        result["categoria"] = SOURCE_CATEGORIES.get(source["tipo"], "otro")
        result["critico"] = bool(source["obligatorio"] and result.get("critico"))
        absent = normalized(str(result.get("gap") or "") + " " + str(result.get("evidence_explanation") or ""))
        if result.get("estado") == "no_cumple" and re.search(r"(?:sin|ausencia|falta|no existe|no hay|no presentan|no se presentan|no acredita|no (?:se )?(?:ha )?documentad|no (?:se )?(?:encontro|encontraron|aporto|aportaron)).*(?:evidencia|document|respaldo|acredita|experiencia|certific)|(?:evidencia|document|respaldo).*(?:ausente|no (?:adjuntad|presentad))", absent):
            result["estado"] = "requiere_revision"
        if result["categoria"] in {"experiencia", "experiencia_economica"} and result.get("estado") in {"cumple", "no_cumple"} and not refs:
            result.update(estado="requiere_revision", gap="Falta una fuente pertinente atribuida para concluir sobre esta experiencia.",
                          accion="Contrastar experiencia y respaldo con las condiciones exigidas.")
    return results


def _documentary_date(value: object) -> datetime | None:
    """Accept only a real ISO or day/month/year date quoted in a source fact."""
    text = str(value or "")
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    latin = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    parts = iso.groups() if iso else (latin[3], latin[2], latin[1]) if latin else None
    if not parts:
        return None
    try:
        return datetime(*(int(part) for part in parts), tzinfo=timezone.utc)
    except ValueError:
        return None


def _documentary_amount(value: object) -> Decimal | None:
    """Mirror the web's exact, nonnegative decimal format without floats/FX arithmetic."""
    if type(value) not in {str, int, float}:
        return None
    text = str(value).strip()
    return Decimal(text) if re.fullmatch(r"\d+(?:\.\d{1,6})?", text) else None


def _period_start(source: dict, at: datetime) -> datetime | None:
    detail = source.get("detalle") or source.get("details_json") or {}
    text = normalized(f"{source.get('requisito') or ''} {detail.get('descripcion') or ''}")
    numeric = re.search(r"\b(\d{1,2})\)?\s*anos\b", text)
    words = re.search(r"\b(uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|quince|veinte)\s+anos\b", text)
    years_by_word = {"uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
                     "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "quince": 15, "veinte": 20}
    years = int(numeric[1]) if numeric else years_by_word[words[1]] if words else None
    if years is None:
        return None
    try:
        return at.replace(year=at.year - years)
    except ValueError:
        # Match Date.setUTCFullYear: a leap-day cutoff becomes March 1.
        return at.replace(year=at.year - years, month=3, day=1)


def guard_economic_comparisons(
    results: list[dict], expected: list[dict], compact_profile: dict, at: datetime | None = None,
) -> list[dict]:
    """A scalar LLM score cannot attest an unsupported currency/scope comparison.

    Validate documentary amount references and arithmetic, preserving uncertain
    comparisons for review. Semantic admission still requires compatible scope;
    this gate never grants legal accreditation or invents an exchange rate.
    """
    at = at or datetime.now(timezone.utc)
    at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
    sources = {s["document_id"]: (s, e) for e in compact_profile.get("experience_json", [])
               for s in e.get("documentary_summary", {}).get("sources", [])
               if isinstance(s.get("document_id"), str)}
    issues = []
    for result, source in zip(results, expected, strict=True):
        detail = source.get("detalle") or source.get("details_json") or {}
        required = _documentary_amount(next((detail[key] for key in ("monto", "amount", "required_amount")
                                            if detail.get(key) is not None), None))
        if required is None or required <= 0 or source.get("tipo") not in {"economic_experience", "specific_experience", "general_experience", "experience"}:
            continue
        if result.get("estado") != "cumple":
            continue
        reason = None
        reasons = []
        proof = result.get("economic_comparison") or []
        if not proof:
            reason = "missing_computable_evidence"
            reasons.append("Falta comparación económica estructurada contra partidas documentadas.")
        else:
            seen, contracts = set(), set()
            total = Decimal("0")
            currency = str(detail.get("moneda") or detail.get("currency") or "PEN").upper()
            start = _period_start(source, at)
            for item in proof:
                doc = str(item.get("document_id") or "")
                binding = sources.get(doc)
                if not binding:
                    reasons.append("La comparación usa una fuente documental inexistente.")
                    continue
                if doc in seen:
                    reasons.append("Una misma fuente se repite en el monto computado.")
                    continue
                seen.add(doc)
                documentary_source, experience = binding
                contract = normalized(experience.get("numero_contrato"))
                if contract and contract in contracts:
                    reasons.append("Se repite el mismo contrato en documentos diferentes.")
                    continue
                if contract:
                    contracts.add(contract)
                extracted = documentary_source.get("extraction") or {}
                amounts = extracted.get("amounts") or []; index = item.get("amount_index")
                amount = amounts[index] if type(index) is int and 0 <= index < len(amounts) else {}
                facts = extracted.get("facts") or []
                texts = [extracted.get("summary"), (extracted.get("classification") or {}).get("quote")]
                texts.extend(value for fact in facts for value in (fact.get("value"), fact.get("quote")))
                quote = normalized(item.get("compatible_scope_quote"))
                selected = _documentary_amount(item.get("eligible_amount_decimal"))
                original = _documentary_amount(amount.get("amount_decimal"))
                item_reasons = []
                if doc not in (result.get("evidence_refs") or []):
                    item_reasons.append("La partida no está atribuida en las referencias documentales del requisito.")
                if selected is None or original is None or selected <= 0 or selected != original:
                    item_reasons.append("El monto computable no coincide con una partida monetaria documentada.")
                if amount.get("currency") != currency or item.get("currency") != currency:
                    item_reasons.append("No se permite convertir o mezclar otra moneda con el mínimo exigido.")
                if item.get("full_amount_comparable") is not True:
                    item_reasons.append("No se acreditó que toda la partida seleccionada corresponda al objeto compatible.")
                if len(quote) < 12 or not any(quote in normalized(text) for text in texts):
                    item_reasons.append("La compatibilidad no cita un alcance literal de la fuente seleccionada.")
                dates = [_documentary_date(value) for fact in facts
                         if re.search(r"fecha|date|vigencia|period|aceptacion|recepcion|conformidad|culminacion", normalized(fact.get("field")))
                         for value in (fact.get("value"), fact.get("quote"))]
                within_period = any(date is not None and date <= at and (start is None or date >= start) for date in dates)
                if item.get("period_supported") is not True or not within_period:
                    item_reasons.append("La vigencia o el período computable no tiene una fecha documental compatible.")
                reasons.extend(item_reasons)
                if not item_reasons:
                    total += selected
            if reasons:
                reason = "unsupported_scope_currency_period_or_duplicate_amount"
            elif total < required:
                reason = "compatible_amount_below_required"
                reasons.append("Las partidas documentadas no cubren el mínimo económico exigido.")
        if reason:
            reasons = list(dict.fromkeys(reasons))
            result.update(estado="requiere_revision", gap="El monto compatible todavía no está validado por partida, moneda y período.",
                          accion="Verificar las partidas compatibles y sus respaldos antes de computarlas.",
                          economic_validation_reasons=reasons)
            issues.append({"source_index": source["source_index"], "code": reason, "reasons": reasons})
    return issues
