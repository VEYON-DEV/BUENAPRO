"""Deterministic review rules for a stored reading (no model, no network).

Gemini reads the document once; these rules look for the ways a reading contradicts itself
or the contract title and decide whether a person must check it. Every rule yields a stable
English ``code`` and a short Spanish ``message``. They run when a reading is derived
(``derive_summary_job``) and when summaries are recomputed locally (``prod6.recompute``), so a
change of rules never needs a new paid reading.

``quality`` keeps its meaning (``auto``/``reviewed``/``corrected``/``failed``): it is the outcome
of a human review or a failure, not a confidence label, and the database constrains its values.
The review signal is ``requires_human_review``, which the admin queue lists and which keeps a
reading out of LLM2 until someone clears it. Reasons live in ``summary_json['review_reasons']``.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from buenapro_worker.normalization.facets import (
    as_dict, as_list, derive_summary, parse_days,
)
from buenapro_worker.prod6.reading_schema import normalize_currency

RULES_VERSION = "reading_review_v1"
# Readings of this prompt generation carry a quote and a page on every requirement kind.
QUALITY_PROMPT_PREFIX = "prod6_"
EXPERIENCE_TYPES = {"economic_experience", "general_experience", "specific_experience"}
# Boilerplate every procedure repeats; a missing quote there says little about the reading.
BOILERPLATE_TYPES = {"legal_capacity", "ruc_status", "rnp", "not_disqualified", "cci"}
EVIDENCE_MIN_REQUIREMENTS = 2
EVIDENCE_MISSING_RATIO = 0.5
HUMAN_QUALITIES = {"reviewed", "corrected"}


def _fold(text: Any) -> str:
    value = text.strip() if isinstance(text, str) else ""
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(char))


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _positive(value: Any) -> float | None:
    if isinstance(value, str) and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", value):
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _reason(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _number(token: str) -> float | None:
    """A Spanish or English number: '.' or ',' followed by exactly three digits is a thousands mark."""
    token = token.strip()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):
        token = re.sub(r"[.,]", "", token)
    else:
        token = token.replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


# --- plazo ------------------------------------------------------------------------------


def _term_is_literal(days: float, text: str) -> bool:
    """The number of days appears in the text, as days or as months, weeks or years.

    Without any digit in the text there is nothing to compare with, so it is accepted.
    """
    figures = [value for token in re.findall(r"\d+(?:[.,]\d+)?", text)
               if (value := _number(token)) is not None]
    if not figures:
        return True
    for value in figures:
        if value == days or value * 7 == days or value * 30 == days or value * 365 == days:
            return True
        if value == int(value) and int(value) % 12 == 0 and days in (value / 12 * 360, value / 12 * 365):
            return True  # "12 meses" / "24 meses" counted as a year.
        if value == int(value) and days == round(value * 30.4):
            return True
    return False


def _term_reasons(raw: dict[str, Any]) -> list[dict[str, str]]:
    contract = as_dict(raw.get("contract"))
    delivery = as_dict(as_dict(raw.get("goods")).get("entrega"))
    reasons: list[dict[str, str]] = []
    days = _positive(contract.get("plazo_dias"))
    text = _text(contract.get("plazo_texto"))
    delivery_days = _positive(delivery.get("plazo_dias"))
    delivery_text = _text(delivery.get("plazo_texto"))
    if days is None and (parse_days(text) or delivery_days or parse_days(delivery_text)):
        reasons.append(_reason("term_missing", "El plazo del contrato está vacío pero el texto o la entrega indican uno."))
    if days is not None and delivery_days is not None and days != delivery_days:
        reasons.append(_reason("term_conflict", "El plazo del contrato y el de la entrega difieren."))
    for value, label in ((days, text), (delivery_days, delivery_text)):
        if value is not None and label and not _term_is_literal(value, label):
            reasons.append(_reason("term_not_literal", "El plazo en días no figura literalmente en el texto del plazo (posible suma o cálculo)."))
            break
    return reasons


# --- pago -------------------------------------------------------------------------------

_COUNT_WORDS = {
    "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19,
    "veinte": 20, "veinticuatro": 24,
}
_PAYMENT_NOUN = r"(?:armadas?|pagos?|cuotas?)"
_PAYMENT_COUNT_PATTERNS = (
    re.compile(rf"\((\d{{1,3}})\)\s*{_PAYMENT_NOUN}\b"),
    re.compile(rf"\b(\d{{1,3}})\s+{_PAYMENT_NOUN}\b"),
    re.compile(rf"\b({'|'.join(_COUNT_WORDS)})\s+{_PAYMENT_NOUN}\b"),
)
_MONTHLY_PAYMENT = re.compile(
    r"\b(?:pagos?|armadas?|cuotas?)\s+(?:\w+\s+){0,2}mensual(?:es)?\b|"
    r"\bmensual(?:es)?\s+(?:y\s+\w+\s+)?(?:pagos?|armadas?|cuotas?)\b|"
    r"\bcada\s+mes\b|\bpor\s+mes\b|\bmes\s+vencido\b")
_SINGLE_PAYMENT = re.compile(r"\bpago\s+unico\b|\bunico\s+pago\b|\bun\s+solo\s+pago\b|\ben\s+una\s+sola\s+armada\b")


def _declared_payments(payment: dict[str, Any]) -> int | None:
    declared = payment.get("numero_pagos_declarado")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    text = _fold(payment.get("detalle"))
    found: set[int] = set()
    for pattern in _PAYMENT_COUNT_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1)
            found.add(_COUNT_WORDS.get(token) or int(token))
    return found.pop() if len(found) == 1 else None


def _payment_reasons(raw: dict[str, Any]) -> list[dict[str, str]]:
    payment = as_dict(raw.get("payment")) or as_dict(as_dict(raw.get("execution")).get("payment"))
    if not payment:
        return []
    kind = _text(payment.get("tipo"))
    rows = [as_dict(item) for item in as_list(payment.get("armadas") or payment.get("installments"))]
    prose = _fold(" ".join([_text(payment.get("detalle"))] + [_text(row.get("condicion")) for row in rows]))
    reasons: list[dict[str, str]] = []

    declared = _declared_payments(payment)
    if declared is not None and (rows or kind == "armadas") and declared != len(rows):
        reasons.append(_reason(
            "payment_count_mismatch",
            f"El documento declara {declared} pagos y el detalle trae {len(rows)} filas."))
    percentages = [row.get("porcentaje") for row in rows]
    if rows and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in percentages):
        total = sum(percentages)
        if not 99 <= total <= 101:
            reasons.append(_reason(
                "payment_percent_sum",
                f"Los porcentajes de los pagos suman {total:g}% y no 100%."))

    incoherent = None
    if kind == "pago_unico" and len(rows) > 1:
        incoherent = "El pago figura como único pero el detalle trae varios pagos."
    elif kind == "armadas" and len(rows) < 2:
        incoherent = "El pago figura en armadas pero el detalle no trae al menos dos pagos."
    elif kind and kind != "mensual" and _MONTHLY_PAYMENT.search(prose):
        incoherent = "El pago figura como no mensual pero el texto describe pagos mensuales."
    elif kind == "mensual" and _SINGLE_PAYMENT.search(prose):
        incoherent = "El pago figura como mensual pero el texto describe un pago único."
    if incoherent:
        reasons.append(_reason("payment_type_incoherent", incoherent))
    return reasons


# --- moneda de la experiencia -----------------------------------------------------------

_CURRENCY_MARKERS = (
    ("USD", re.compile(r"us\s*\$|\busd\b|\$\s*\d|\bdolar(?:es)?\b")),
    ("EUR", re.compile(r"€|\beur\b|\beuros?\b")),
    ("PEN", re.compile(r"\bs\s*/\s*\.?|\bsoles?\b|\bpen\b")),
)


def _currencies_in(text: str) -> set[str]:
    folded = _fold(text)
    return {currency for currency, marker in _CURRENCY_MARKERS if marker.search(folded)}


def _normalize_currency(value: Any) -> str | None:
    """PEN/USD/EUR when the value names exactly one currency."""
    declared = normalize_currency(value)
    if declared:
        return declared
    found = _currencies_in(_text(value))
    return next(iter(found)) if len(found) == 1 else None


def _currency_reasons(raw: dict[str, Any]) -> list[dict[str, str]]:
    contract_currency = _normalize_currency(as_dict(raw.get("contract")).get("moneda")) or "PEN"
    for item in as_list(as_dict(raw.get("requirements")).get("provider")):
        item = as_dict(item)
        if item.get("tipo") not in EXPERIENCE_TYPES:
            continue
        declared = _normalize_currency(item.get("moneda"))
        currencies = {declared} if declared else _currencies_in(
            f"{_text(item.get('descripcion'))} {_text(item.get('evidencia'))}")
        if currencies and contract_currency not in currencies:
            return [_reason(
                "experience_currency_mismatch",
                f"La experiencia exigida está en {'/'.join(sorted(currencies))} y el contrato en {contract_currency}.")]
    return []


# --- evidencia --------------------------------------------------------------------------


def _evidence_reasons(raw: dict[str, Any], prompt_version: str | None) -> list[dict[str, str]]:
    requirements = as_dict(raw.get("requirements"))
    items = [as_dict(item) for item in as_list(requirements.get("provider"))
             if as_dict(item).get("tipo") not in BOILERPLATE_TYPES]
    if isinstance(prompt_version, str) and prompt_version.startswith(QUALITY_PROMPT_PREFIX):
        for key in ("key_personnel", "equipment", "insurance"):
            items += [as_dict(item) for item in as_list(requirements.get(key))]
    if len(items) < EVIDENCE_MIN_REQUIREMENTS:
        return []
    missing = sum(1 for item in items if not _text(item.get("evidencia")))
    if missing / len(items) > EVIDENCE_MISSING_RATIO:
        return [_reason("low_evidence",
                        f"{missing} de {len(items)} requisitos no traen cita textual del documento.")]
    return []


# --- cifras del título contra las especificaciones --------------------------------------

_UNIT_PATTERN = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(ghz|mhz|gb|tb|mb|hp|kva|kw|kv|nucleos?|cores?)\b(?!\s*/)")
_STORAGE_TO_GB = {"mb": (1 / 1000, 1 / 1024), "gb": (1,), "tb": (1000, 1024)}


def _spec_numbers(text: str) -> dict[str, set[float]]:
    """Numbers with a technical unit, grouped by unit (storage in GB, frequency in GHz)."""
    found: dict[str, set[float]] = {}
    for token, unit in _UNIT_PATTERN.findall(_fold(text)):
        value = _number(token)
        if value is None:
            continue
        if unit in _STORAGE_TO_GB:
            group, values = "GB", [value * factor for factor in _STORAGE_TO_GB[unit]]
        elif unit in {"ghz", "mhz"}:
            group, values = "GHz", [value if unit == "ghz" else value / 1000]
        elif unit.startswith(("nucleo", "core")):
            group, values = "núcleos", [value]
        else:
            group, values = unit.upper() if unit != "kva" else "kVA", [value]
        found.setdefault(group, set()).update(round(item, 3) for item in values)
    return found


def _shown(values: set[float]) -> str:
    return ", ".join(f"{value:g}" for value in sorted(values))


def _goods_specs_text(raw: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in as_list(as_dict(raw.get("goods")).get("items")):
        item = as_dict(item)
        parts.append(_text(item.get("nombre")))
        parts += [_text(spec) for spec in as_list(item.get("especificaciones"))]
    return " . ".join(part for part in parts if part)


_NUMBER_WORD = (r"(?:un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|trece|catorce|"
                r"quince|dieciseis|diecisiete|dieciocho|diecinueve|veinte|veinti\w+|treinta|cuarenta|"
                r"cincuenta|sesenta|setenta|ochenta|noventa|cien|ciento|doscientos|trescientos|"
                r"cuatrocientos|quinientos|mil)")
_TITLE_QUANTITY = re.compile(rf"\b{_NUMBER_WORD}(?:\s+y\s+{_NUMBER_WORD})?\s*\((\d{{1,4}})\)")
_MODEL_TOKEN = re.compile(r"\b(?=[A-Za-z0-9-]{4,}\b)(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]+\b")
_UNIT_TOKEN = re.compile(r"\d+(?:ghz|mhz|gb|tb|mb|hp|kva|kw|kv|hz|rpm|psi|bar|mm|cm|kg|ma|va|v|a|w|m)")


def _title_reasons(raw: dict[str, Any], title: str | None) -> list[dict[str, str]]:
    title = re.sub(r"\s+", " ", title).strip() if isinstance(title, str) else ""
    if not title:
        return []
    reasons: list[dict[str, str]] = []
    goods = as_dict(raw.get("goods"))
    items = [as_dict(item) for item in as_list(goods.get("items"))]

    specs_text = _goods_specs_text(raw)
    if specs_text:
        wanted, read = _spec_numbers(title), _spec_numbers(specs_text)
        conflicts = [f"{group} título {_shown(wanted[group])} / lectura {_shown(read[group])}"
                     for group in sorted(set(wanted) & set(read)) if not wanted[group] & read[group]]
        if conflicts:
            reasons.append(_reason(
                "title_spec_mismatch",
                "Cifras del título distintas de las especificaciones leídas: " + "; ".join(conflicts) + "."))

    quantities = {int(value) for value in _TITLE_QUANTITY.findall(_fold(title))}
    amounts = [item.get("cantidad") for item in items
               if isinstance(item.get("cantidad"), (int, float)) and not isinstance(item.get("cantidad"), bool)]
    if quantities and amounts:
        read_quantities = {float(value) for value in amounts} | {float(sum(amounts)), float(len(items))}
        if not {float(value) for value in quantities} & read_quantities:
            reasons.append(_reason(
                "title_quantity_mismatch",
                f"La cantidad del título ({_shown({float(v) for v in quantities})}) no coincide con las cantidades leídas ({_shown(read_quantities)})."))

    # Only a purchased good has a model the document must repeat; service titles carry project codes.
    corpus = re.sub(r"[^a-z0-9]", "", _fold(" ".join(_walk_strings(raw)))) if "goods" in raw else ""
    if corpus:
        missing = sorted({token for token in _MODEL_TOKEN.findall(title)
                          if not _UNIT_TOKEN.fullmatch(_fold(token).replace("-", ""))
                          and re.sub(r"[^a-z0-9]", "", _fold(token)) not in corpus})
        if missing:
            reasons.append(_reason(
                "title_model_missing",
                "El modelo o código del título no aparece en la lectura: " + ", ".join(missing[:3]) + "."))
    return reasons


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


# --- conjunto ---------------------------------------------------------------------------


def review_reading(raw: dict[str, Any], *, contract_title: str | None = None,
                   prompt_version: str | None = None) -> list[dict[str, str]]:
    """Every reason this reading needs a person; empty when no rule objects."""
    raw = as_dict(raw)
    reasons: list[dict[str, str]] = []
    rules = (
        lambda: _term_reasons(raw),
        lambda: _payment_reasons(raw),
        lambda: _currency_reasons(raw),
        lambda: _evidence_reasons(raw, prompt_version),
        lambda: _title_reasons(raw, contract_title),
    )
    for rule in rules:
        try:
            reasons += rule()
        except Exception:  # A rule that cannot read this shape means nobody has verified it.
            reasons.append(_reason("review_rule_failed", "Una regla de revisión no pudo evaluar esta lectura."))
    return reasons


def build_reading_summary(raw: dict[str, Any], *, contract_title: str | None = None,
                          prompt_version: str | None = None, previous_summary: dict[str, Any] | None = None,
                          previous_flag: bool = False, quality: str = "auto") -> tuple[dict[str, Any], bool]:
    """The summary to store and the final ``requires_human_review``.

    A flag raised before the rules existed (empty goods, fallback model, unsupported commercial
    labels) is kept as ``review_inherited`` and cleared only by a person. Rules add reasons and
    can also be withdrawn by recomputing. A reading a person already reviewed or corrected keeps
    the flag the person left.
    """
    previous = previous_summary if isinstance(previous_summary, dict) else {}
    summary = derive_summary(raw)
    reasons = review_reading(raw, contract_title=contract_title, prompt_version=prompt_version)
    inherited = (bool(previous.get("review_inherited")) if "review_rules_version" in previous
                 else bool(previous_flag))
    summary["review_reasons"] = reasons
    summary["review_rules_version"] = RULES_VERSION
    summary["review_inherited"] = inherited
    flag = bool(reasons) or inherited
    if quality in HUMAN_QUALITIES:
        flag = bool(previous_flag)
    return summary, flag
