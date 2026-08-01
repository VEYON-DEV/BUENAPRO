from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


CONTRACT_CODE_RE = re.compile(
    r"^(?P<tipo>[A-Z]+)-(?P<correlativo>\d+)-(?P<anio>\d{4})-(?P<sigla>[A-Z0-9].*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HistoricalOutcome:
    state: str
    supplier_ruc: str | None
    supplier_name: str | None
    price_total: Decimal | None


def parse_contract_code(code: str | None) -> dict[str, object | None]:
    full = (code or "").strip()
    match = CONTRACT_CODE_RE.fullmatch(full)
    if not match:
        return {
            "codigo_completo": full,
            "codigo_tipo": None,
            "codigo_correlativo": None,
            "codigo_anio": None,
            "codigo_sigla": None,
        }
    groups = match.groupdict()
    return {
        "codigo_completo": full,
        "codigo_tipo": groups["tipo"].upper(),
        "codigo_correlativo": int(groups["correlativo"]),
        "codigo_anio": int(groups["anio"]),
        "codigo_sigla": groups["sigla"].strip().upper(),
    }


def _money(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def classify_outcome(detail: dict[str, Any]) -> HistoricalOutcome:
    rows = [
        *(detail.get("uitContratoItemProjectionList") or []),
        *(detail.get("uitCotizacionProjectionList") or []),
    ]
    awarded = next(
        (
            row
            for row in rows
            if row.get("precioTotal") not in (None, "")
            or row.get("precioOferta") not in (None, "")
            or row.get("codRuc")
            or row.get("nomRazonSocial")
        ),
        None,
    )
    if awarded:
        raw_price = awarded.get("precioTotal")
        if raw_price in (None, ""):
            raw_price = awarded.get("precioOferta")
        return HistoricalOutcome(
            state="ADJUDICADO",
            supplier_ruc=str(awarded.get("codRuc") or "").strip() or None,
            supplier_name=str(awarded.get("nomRazonSocial") or "").strip() or None,
            price_total=_money(raw_price),
        )
    if any("DESIERTO" in str(row.get("nomEstadoCotiza") or "").upper() for row in rows):
        return HistoricalOutcome("DESIERTO", None, None, None)
    return HistoricalOutcome("SIN_RESULTADO", None, None, None)
