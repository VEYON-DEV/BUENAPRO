"""Response schemas of the PROD6 quality reading (prompt ``prod6_*_reading_v1``).

They extend the frozen TDR/EETT schemas with optional fields only: a requirement carries
its literal quote, page and currency; the payment its declared number of payments; the
contract its form of execution. Every old reading still validates and every consumer of
the base fields keeps working. The new fields are lenient on purpose: a paid answer must
never be blocked by a malformed page number or currency, so an unusable value becomes
``None`` and the review rules treat a missing quote as missing evidence.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import Field, field_validator

from buenapro_worker.extraction.schemas import (
    ContractV2, EettExtractionV1, EquipmentItemV2, InsuranceItemV2, KeyPersonnelV2,
    PaymentV2, ProviderRequirementV2, RequirementsV2, TdrExtractionV2,
)

_PAGE_DESCRIPTION = "Numero de pagina del PDF original (la primera pagina del archivo es 1)."
_QUOTE_DESCRIPTION = "Cita literal breve del documento que respalda el requisito."


def lenient_page(value: Any) -> int | None:
    """A positive integer page; anything else (ranges, text, 0) is not a usable page."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,4}\s*", value):
        value = int(value)
    return value if isinstance(value, int) and value >= 1 else None


def _fold(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


def normalize_currency(value: Any) -> str | None:
    """PEN / USD / EUR from a symbol or word, else None (never guessed)."""
    if not isinstance(value, str):
        return None
    text = _fold(value).strip()
    if text in {"pen", "s/", "s/.", "sol", "soles", "nuevos soles"}:
        return "PEN"
    if text in {"usd", "us$", "$", "dolar", "dolares", "dolares americanos", "us dolares"}:
        return "USD"
    if text in {"eur", "€", "euro", "euros"}:
        return "EUR"
    return None


class ProviderRequirementV3(ProviderRequirementV2):
    pagina: int | None = Field(default=None, description=_PAGE_DESCRIPTION)
    moneda: str | None = Field(
        default=None, description="Moneda del monto del requisito: PEN, USD o EUR; null sin monto.")

    @field_validator("pagina", mode="before")
    @classmethod
    def _page(cls, value: Any) -> int | None:
        return lenient_page(value)

    @field_validator("moneda", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str | None:
        return normalize_currency(value)


class KeyPersonnelV3(KeyPersonnelV2):
    evidencia: str | None = Field(default=None, description=_QUOTE_DESCRIPTION)
    pagina: int | None = Field(default=None, description=_PAGE_DESCRIPTION)

    @field_validator("pagina", mode="before")
    @classmethod
    def _page(cls, value: Any) -> int | None:
        return lenient_page(value)


class EquipmentItemV3(EquipmentItemV2):
    evidencia: str | None = Field(default=None, description=_QUOTE_DESCRIPTION)
    pagina: int | None = Field(default=None, description=_PAGE_DESCRIPTION)

    @field_validator("pagina", mode="before")
    @classmethod
    def _page(cls, value: Any) -> int | None:
        return lenient_page(value)


class InsuranceItemV3(InsuranceItemV2):
    evidencia: str | None = Field(default=None, description=_QUOTE_DESCRIPTION)
    pagina: int | None = Field(default=None, description=_PAGE_DESCRIPTION)

    @field_validator("pagina", mode="before")
    @classmethod
    def _page(cls, value: Any) -> int | None:
        return lenient_page(value)


class RequirementsV3(RequirementsV2):
    provider: list[ProviderRequirementV3] = Field(default_factory=list)
    key_personnel: list[KeyPersonnelV3] = Field(default_factory=list)
    equipment: list[EquipmentItemV3] = Field(default_factory=list)
    insurance: list[InsuranceItemV3] = Field(default_factory=list)


class PaymentV3(PaymentV2):
    numero_pagos_declarado: int | None = Field(
        default=None, description="Cantidad de pagos que el documento declara expresamente.")

    @field_validator("numero_pagos_declarado", mode="before")
    @classmethod
    def _count(cls, value: Any) -> int | None:
        return lenient_page(value)


class ContractV3(ContractV2):
    forma_ejecucion: str | None = Field(
        default=None, description="remota, presencial o mixta segun el documento; null si no la define.")

    @field_validator("forma_ejecucion", mode="before")
    @classmethod
    def _form(cls, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = _fold(value).strip()
        if text in {"remota", "remoto", "virtual", "a distancia"}:
            return "remota"
        if text in {"presencial", "in situ"}:
            return "presencial"
        if text in {"mixta", "mixto", "hibrida", "hibrido"}:
            return "mixta"
        return None


class TdrExtractionV3(TdrExtractionV2):
    contract: ContractV3 = Field(default_factory=ContractV3)
    payment: PaymentV3 = Field(default_factory=PaymentV3)
    requirements: RequirementsV3 = Field(default_factory=RequirementsV3)


class EettExtractionV3(EettExtractionV1):
    contract: ContractV3 = Field(default_factory=ContractV3)
    payment: PaymentV3 = Field(default_factory=PaymentV3)
    requirements: RequirementsV3 = Field(default_factory=RequirementsV3)


__all__ = [
    "ContractV3", "EettExtractionV3", "EquipmentItemV3", "InsuranceItemV3", "KeyPersonnelV3",
    "PaymentV3", "ProviderRequirementV3", "RequirementsV3", "TdrExtractionV3",
    "lenient_page", "normalize_currency",
]
