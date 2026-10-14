"""Versioned PROD2 extraction scope; absent scope preserves the legacy contract.

The focused prompt replaces the legacy base prompt. It already contains the
participation and goods instructions: callers must not append those a second
time. Document-set provenance/page instructions may still be appended by the
selected reader. This policy changes output scope, never source coverage.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from buenapro_worker.extraction.schemas import (
    BasesGoodsExtractionV2,
    BasesGoodsV2,
    ContractManagementV2,
    ContractV2,
    EettExtractionV1,
    EquipmentItemV2,
    ExecutionV2,
    GoodsV1,
    InsuranceItemV2,
    KeyPersonnelV2,
    ParticipationV1,
    PenaltyV2,
    ProviderRequirementV2,
    StrictModel,
    SummaryV2,
    TdrExtractionV2,
)

QUALIFICATION_FOCUS_POLICY = "prod2_qualification_focus_v1"
SCOPE_POLICY_KEY = "llm1_scope_policy"
FOCUSED_PROMPT_FILENAME = "prod2_qualification_focus_v1.txt"
_DOC_CLASSES = frozenset({"tdr", "eett", "bases_service", "bases_good"})


class QualificationRequirementsV1(StrictModel):
    """Retain qualification/accreditation; omit the proposal attachment list."""

    provider: list[ProviderRequirementV2] = Field(default_factory=list)
    key_personnel: list[KeyPersonnelV2] = Field(default_factory=list)
    equipment: list[EquipmentItemV2] = Field(default_factory=list)
    insurance: list[InsuranceItemV2] = Field(default_factory=list)


class QualificationTdrExtractionV1(StrictModel):
    contract: ContractV2 = Field(default_factory=ContractV2)
    execution: ExecutionV2 = Field(default_factory=ExecutionV2)
    requirements: QualificationRequirementsV1 = Field(default_factory=QualificationRequirementsV1)
    penalties: list[PenaltyV2] = Field(default_factory=list)
    contract_management: ContractManagementV2 = Field(default_factory=ContractManagementV2)
    summary: SummaryV2 = Field(default_factory=SummaryV2)
    participation: ParticipationV1 = Field(default_factory=ParticipationV1)


class QualificationEettExtractionV1(QualificationTdrExtractionV1):
    goods: GoodsV1 = Field(default_factory=GoodsV1)


class QualificationBasesGoodsExtractionV1(QualificationEettExtractionV1):
    # Keep the explicit goods/items coverage contract of BasesGoodsExtractionV2.
    goods: BasesGoodsV2


def validate_scope_policy(policy: Any) -> str | None:
    """Validate a policy value; None represents the absent, legacy policy."""
    if policy is None:
        return None
    if not isinstance(policy, str) or policy != QUALIFICATION_FOCUS_POLICY:
        raise ValueError("Unsupported PROD2 LLM1 scope policy")
    return policy


def get_scope_policy(selection_context: Mapping[str, Any] | None) -> str | None:
    """Only an absent field selects legacy; a present null is not an opt-in."""
    if selection_context is None:
        return None
    if not isinstance(selection_context, Mapping):
        raise ValueError("PROD2 LLM1 scope policy requires a JSON context")
    if SCOPE_POLICY_KEY not in selection_context:
        return None
    policy = selection_context[SCOPE_POLICY_KEY]
    if policy is None:
        raise ValueError("Unsupported PROD2 LLM1 scope policy")
    return validate_scope_policy(policy)


def validate_scope_payload(
    raw: Mapping[str, Any], selection_context: Mapping[str, Any] | None
) -> None:
    """Reject out-of-scope generated keys without erasing paid raw evidence.

    Pydantic's legacy extra="ignore" must not silently turn an out-of-scope
    response into a compliant reading. Source/provenance metadata is allowed.
    """
    if get_scope_policy(selection_context) is None:
        return
    if not isinstance(raw, Mapping):
        raise ValueError("Focused PROD2 LLM1 payload must be a JSON object")
    if "payment" in raw:
        raise ValueError("Focused PROD2 LLM1 payload contains out-of-scope payment")
    requirements = raw.get("requirements")
    if isinstance(requirements, Mapping) and "proposal_documents" in requirements:
        raise ValueError(
            "Focused PROD2 LLM1 payload contains out-of-scope requirements.proposal_documents"
        )


def _doc_class(doc_class: str | None) -> str:
    value = doc_class or "tdr"
    if value not in _DOC_CLASSES:
        raise ValueError(f"Unsupported document class for extraction scope: {doc_class}")
    return value


def schema_for_scope(
    doc_class: str | None, selection_context: Mapping[str, Any] | None
) -> type[BaseModel]:
    document_class = _doc_class(doc_class)
    if get_scope_policy(selection_context) is None:
        return (
            BasesGoodsExtractionV2 if document_class == "bases_good"
            else EettExtractionV1 if document_class == "eett" else TdrExtractionV2
        )
    return (
        QualificationBasesGoodsExtractionV1 if document_class == "bases_good"
        else QualificationEettExtractionV1 if document_class == "eett"
        else QualificationTdrExtractionV1
    )


def prompt_for_scope(
    doc_class: str | None,
    selection_context: Mapping[str, Any] | None,
    legacy_prompt: str,
) -> str:
    document_class = _doc_class(doc_class)
    if get_scope_policy(selection_context) is None:
        return legacy_prompt
    # Lazy import keeps scope models independent of a provider client and avoids
    # a module cycle if the legacy reader imports this policy in the future.
    from buenapro_worker.extraction.gemini import read_prompt

    prompt = read_prompt(FOCUSED_PROMPT_FILENAME)
    prompt += f"\nTIPO_DOCUMENTAL={document_class}.\n"
    if document_class in {"bases_service", "bases_good"}:
        prompt += (
            "Estas leyendo bases de un procedimiento de seleccion. Los formatos "
            "estandar, opciones no elegidas y anexos genericos no constituyen "
            "requisitos especificos de esta convocatoria.\n"
        )
    if document_class in {"eett", "bases_good"}:
        prompt += (
            "Esta convocatoria es de BIENES. Incluye goods.items, incluso [] si no "
            "hay bienes identificables, y explica esa ausencia en las observaciones. "
            "Conserva cantidades, unidades, especificaciones, entrega y garantia. "
            "No confundas los bienes comprados con equipamiento del postor.\n"
        )
    return prompt


def prompt_version_for_scope(
    doc_class: str | None, selection_context: Mapping[str, Any] | None
) -> str:
    document_class = _doc_class(doc_class)
    if get_scope_policy(selection_context) is None:
        from buenapro_worker.extraction.gemini import prompt_version_for_doc_class

        return prompt_version_for_doc_class(document_class)
    return f"prod2_qualification_focus_{document_class}_prompt_v1"


def schema_version_for_scope(
    doc_class: str | None, selection_context: Mapping[str, Any] | None
) -> str:
    document_class = _doc_class(doc_class)
    if get_scope_policy(selection_context) is None:
        from buenapro_worker.extraction.gemini import (
            BASES_GOODS_SCHEMA_VERSION,
            BASES_SCHEMA_VERSION,
            EETT_SCHEMA_VERSION,
            SCHEMA_VERSION,
        )

        return (
            BASES_GOODS_SCHEMA_VERSION if document_class == "bases_good"
            else BASES_SCHEMA_VERSION if document_class == "bases_service"
            else EETT_SCHEMA_VERSION if document_class == "eett" else SCHEMA_VERSION
        )
    return f"prod2_qualification_focus_{document_class}_schema_v1"
