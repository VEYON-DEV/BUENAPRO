from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TdrExtraction(ExtractionModel):
    contract: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    payment: dict[str, Any] = Field(default_factory=dict)
    requirements: dict[str, Any] = Field(default_factory=dict)
    penalties: list[dict[str, Any]] = Field(default_factory=list)
    contract_management: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --- Schema v2: estructura fija, usada como response_schema de Gemini ---
# Todo campo es opcional-con-default para que el modelo nunca falle por dato
# ausente en el TDR; la obligatoriedad semantica vive en el prompt ("null si
# no esta en el documento"). Los Literal se vuelven enums en el schema.

FACET_TYPES = Literal[
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
]


class StrictModel(BaseModel):
    # extra="ignore" (no "forbid"): el API de Gemini rechaza additionalProperties
    # en response_schema; la estructura ya queda restringida por el propio schema.
    model_config = ConfigDict(extra="ignore")


class ProviderRequirementV2(StrictModel):
    tipo: FACET_TYPES = "other"
    descripcion: str = ""
    obligatorio: bool = True
    monto: float | None = None
    evidencia: str | None = None


class KeyPersonnelV2(StrictModel):
    rol: str = ""
    formacion: list[str] = Field(default_factory=list)
    colegiatura_requerida: bool = False
    experiencia: str | None = None
    experiencia_anios: float | None = None
    capacitaciones: list[str] = Field(default_factory=list)


class EquipmentItemV2(StrictModel):
    nombre: str = ""
    cantidad: str | None = None
    caracteristicas: str | None = None
    obligatorio: bool = True


class InsuranceItemV2(StrictModel):
    tipo: str = ""
    descripcion: str | None = None
    obligatorio: bool = True


class DeliverableV2(StrictModel):
    nombre: str = ""
    plazo_texto: str | None = None
    plazo_dias: int | None = None


class ArmadaV2(StrictModel):
    numero: int = 1
    porcentaje: float | None = None
    condicion: str | None = None


class PaymentV2(StrictModel):
    tipo: Literal["pago_unico", "armadas", "mensual", "por_entregable", "no_determinado"] = "no_determinado"
    detalle: str | None = None
    plazo_pago_dias: int | None = None
    armadas: list[ArmadaV2] = Field(default_factory=list)


class PenaltyV2(StrictModel):
    tipo: Literal["mora", "otra", "resolucion"] = "otra"
    descripcion: str = ""
    formula: str | None = None
    tope_pct: float | None = None
    base_calculo: str | None = None


class ContractV2(StrictModel):
    objeto: str | None = None
    entidad: str | None = None
    area_usuaria: str | None = None
    lugar_ejecucion: str | None = None
    plazo_dias: int | None = None
    plazo_texto: str | None = None
    valor_estimado_monto: float | None = None
    moneda: str | None = None
    modalidad: str | None = None
    condicion_inicio: str | None = None


class ExecutionV2(StrictModel):
    actividades: list[str] = Field(default_factory=list)
    entregables: list[DeliverableV2] = Field(default_factory=list)
    lugar: str | None = None


class RequirementsV2(StrictModel):
    provider: list[ProviderRequirementV2] = Field(default_factory=list)
    key_personnel: list[KeyPersonnelV2] = Field(default_factory=list)
    equipment: list[EquipmentItemV2] = Field(default_factory=list)
    insurance: list[InsuranceItemV2] = Field(default_factory=list)
    proposal_documents: list[str] = Field(default_factory=list)


class ContractManagementV2(StrictModel):
    conformidad: str | None = None
    confidencialidad: str | None = None
    vicios_ocultos: str | None = None
    resolucion_controversias: str | None = None


class SummaryV2(StrictModel):
    descripcion_corta: str = ""
    observaciones_clave: list[str] = Field(default_factory=list)


class TdrExtractionV2(StrictModel):
    contract: ContractV2 = Field(default_factory=ContractV2)
    execution: ExecutionV2 = Field(default_factory=ExecutionV2)
    payment: PaymentV2 = Field(default_factory=PaymentV2)
    requirements: RequirementsV2 = Field(default_factory=RequirementsV2)
    penalties: list[PenaltyV2] = Field(default_factory=list)
    contract_management: ContractManagementV2 = Field(default_factory=ContractManagementV2)
    summary: SummaryV2 = Field(default_factory=SummaryV2)


class GoodsItemV1(StrictModel):
    nombre: str = ""
    cantidad: float | None = None
    unidad_medida: str | None = None
    especificaciones: list[str] = Field(default_factory=list)


class GoodsDeliveryV1(StrictModel):
    lugar: str | None = None
    plazo_texto: str | None = None
    plazo_dias: int | None = None
    condiciones: str | None = None


class GoodsWarrantyV1(StrictModel):
    plazo_texto: str | None = None
    cobertura: str | None = None
    condiciones: str | None = None


class GoodsV1(StrictModel):
    items: list[GoodsItemV1] = Field(default_factory=list)
    entrega: GoodsDeliveryV1 = Field(default_factory=GoodsDeliveryV1)
    garantia: GoodsWarrantyV1 = Field(default_factory=GoodsWarrantyV1)


class EettExtractionV1(TdrExtractionV2):
    """Mantiene los campos consumidos por la normalizacion y agrega datos de Bienes."""

    goods: GoodsV1 = Field(default_factory=GoodsV1)


class BasesGoodsV2(GoodsV1):
    # Required structure, not fabricated content: [] remains valid when the
    # official document does not disclose identifiable purchased items.
    items: list[GoodsItemV1]


class BasesGoodsExtractionV2(EettExtractionV1):
    """Goods bases must explicitly report goods/items, including an empty list."""

    goods: BasesGoodsV2


def repair_optional_fields(payload: dict[str, Any]) -> dict[str, Any]:
    repaired = dict(payload)
    for key in ("contract", "execution", "payment", "requirements", "contract_management"):
        value = repaired.get(key)
        repaired[key] = value if isinstance(value, dict) else {}

    penalties = repaired.get("penalties")
    if isinstance(penalties, dict):
        repaired["penalties"] = [penalties]
    elif isinstance(penalties, list):
        repaired["penalties"] = [item for item in penalties if isinstance(item, dict)]
    else:
        repaired["penalties"] = []

    summary = repaired.get("summary")
    if isinstance(summary, str):
        repaired["summary"] = {"description": summary}
    elif isinstance(summary, dict):
        repaired["summary"] = summary
    else:
        repaired["summary"] = {}

    return repaired
