"""Opt-in commercial labels with documentary evidence; never a legacy backfill.

Catalog IDs are loaded from the same packaged JSON used by the web application.
These labels describe the purchased work, not supplier eligibility or a score.
"""
from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
import json
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, Field, create_model, model_validator

from .schemas import StrictModel

POLICY_KEY = "llm1_commercial_taxonomy"
TAXONOMY_VERSION = "commercial_taxonomy_v1"
_CATALOG = json.loads(files("buenapro_worker.extraction").joinpath(
    "commercial_taxonomy.v1.json").read_text(encoding="utf-8"))
if _CATALOG["version"] != TAXONOMY_VERSION:
    raise RuntimeError("Unsupported packaged commercial taxonomy")

AREA_IDS = tuple(row["id"] for row in _CATALOG["areas"])
WORK_TYPE_IDS = tuple(row["id"] for row in _CATALOG["work_types"])
SPECIALTY_IDS = tuple(row["id"] for row in _CATALOG["specialties"])
SUPPORT_STATUS_IDS = tuple(row["id"] for row in _CATALOG["support_statuses"])
Area = Literal[*AREA_IDS]
SecondaryArea = Literal[*tuple(value for value in AREA_IDS if value != "sin_clasificar")]
WorkType = Literal[*WORK_TYPE_IDS]
Specialty = Literal[*SPECIALTY_IDS]
SupportStatus = Literal[*SUPPORT_STATUS_IDS]
EvidenceValue = Literal[*tuple(value for value in AREA_IDS if value != "sin_clasificar"),
                        *WORK_TYPE_IDS, *SPECIALTY_IDS]


def _fold(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


_AI_EXPLICIT = re.compile(
    r"\b(?:inteligencia artificial|ia|ai|machine learning|aprendizaje automatico|"
    r"deep learning|aprendizaje profundo|llms?|modelos? de lenguaje|"
    r"large language models?|generative ai|artificial intelligence|openai|"
    r"gemini|chatgpt|gpt(?:[- .]?\d[\w.-]*)?|langgraph|crewai|autogen|"
    r"agent development kit)\b")
_AGENT_EXPLICIT = re.compile(
    r"\bagentes?\s+(?:(?:de|con)\s+)?(?:ia|ai|inteligencia artificial)\b|"
    r"\bagentes?\s+(?:autonomos?|inteligentes?)\b|\bai agents?\b|"
    r"\b(?:langgraph|crewai|autogen|agent development kit|openai agents)\b")


class CommercialEvidenceV1(StrictModel):
    axis: Literal["area", "work_type", "specialty"]
    value: EvidenceValue
    citation: str = Field(min_length=1)
    page: int = Field(ge=1, strict=True)

    @model_validator(mode="before")
    @classmethod
    def closed_fields(cls, value):
        # Keep Gemini-compatible JSON schema (no additionalProperties) while
        # rejecting undeclared fields rather than silently discarding evidence.
        if isinstance(value, Mapping) and set(value) - set(cls.model_fields):
            raise ValueError("Unknown commercial evidence field")
        return value

    @model_validator(mode="after")
    def valid_value_and_citation(self):
        allowed = {"area": AREA_IDS, "work_type": WORK_TYPE_IDS, "specialty": SPECIALTY_IDS}
        if self.value not in allowed[self.axis] or self.value == "sin_clasificar":
            raise ValueError("Unknown commercial evidence label")
        if not self.citation.strip():
            raise ValueError("Commercial evidence requires a literal citation")
        if self.value == "inteligencia_artificial" and not _AI_EXPLICIT.search(_fold(self.citation)):
            raise ValueError("Artificial intelligence requires explicit documentary evidence")
        if self.value == "agentes_ia" and not _AGENT_EXPLICIT.search(_fold(self.citation)):
            raise ValueError("AI agents require explicit documentary evidence")
        return self


class CommercialClassificationV1(StrictModel):
    taxonomy_version: Literal["commercial_taxonomy_v1"]
    primary_area: Area
    secondary_areas: list[SecondaryArea]
    work_types: list[WorkType]
    specialties: list[Specialty]
    support_status: SupportStatus
    evidence: list[CommercialEvidenceV1]

    @model_validator(mode="before")
    @classmethod
    def closed_fields(cls, value):
        if isinstance(value, Mapping) and set(value) - set(cls.model_fields):
            raise ValueError("Unknown commercial classification field")
        return value

    @model_validator(mode="after")
    def evidenced_labels(self):
        for labels in (self.secondary_areas, self.work_types, self.specialties):
            if len(labels) != len(set(labels)):
                raise ValueError("Duplicate commercial labels")
        if self.primary_area in self.secondary_areas or "sin_clasificar" in self.secondary_areas:
            raise ValueError("Invalid secondary commercial area")
        if self.primary_area == "sin_clasificar" and (
                self.support_status != "needs_review" or self.secondary_areas
                or self.work_types or self.specialties or self.evidence):
            raise ValueError("Unclassified work requires review and empty labels/evidence")
        selected = ({("area", area) for area in [self.primary_area, *self.secondary_areas]
                     if area != "sin_clasificar"}
                    | {("work_type", value) for value in self.work_types}
                    | {("specialty", value) for value in self.specialties})
        cited = {(item.axis, item.value) for item in self.evidence}
        if selected != cited:
            raise ValueError("Every commercial label requires its own applicable evidence")
        return self


def get_commercial_policy(context: Mapping[str, Any] | None) -> str | None:
    if context is None:
        return None
    if not isinstance(context, Mapping):
        raise ValueError("Commercial taxonomy requires a JSON policy context")
    if POLICY_KEY not in context:
        return None
    if context[POLICY_KEY] != TAXONOMY_VERSION:
        raise ValueError("Unsupported LLM1 commercial taxonomy policy")
    return TAXONOMY_VERSION


@lru_cache(maxsize=None)
def _extended_schema(base: type[BaseModel]) -> type[BaseModel]:
    return create_model("Commercial" + base.__name__, __base__=base,
                        commercial_classification=(CommercialClassificationV1, ...))


def schema_with_commercial(base: type[BaseModel], context) -> type[BaseModel]:
    return _extended_schema(base) if get_commercial_policy(context) else base


def version_with_commercial(version: str, context) -> str:
    return version + "_" + TAXONOMY_VERSION if get_commercial_policy(context) else version


def prompt_with_commercial(prompt: str, context) -> str:
    if not get_commercial_policy(context):
        return prompt
    catalog = {axis: [{"id": row["id"], "label": row["label"]} for row in _CATALOG[axis]]
               for axis in ("areas", "work_types", "specialties")}
    return prompt + (
        "\nCLASIFICACION_COMERCIAL=commercial_taxonomy_v1. Incluye obligatoriamente "
        "commercial_classification con taxonomy_version, primary_area, secondary_areas, "
        "work_types, specialties, support_status y evidence. Catalogo cerrado: "
        + json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + ". Clasifica el objeto comprado, no la capacidad de la empresa ni su puntaje. "
        "El rubro, el tipo de trabajo y la especialidad son ejes distintos: una compra "
        "puede combinar areas y trabajos. Para CADA etiqueta elegida incluye evidencia "
        "literal de la clausula aplicable con axis (area, work_type o specialty), value, "
        "citation y page (pagina GLOBAL original desde 1). No inventes documentos, UUID, "
        "hashes, paginas ni citas; la atribucion la conserva el sistema. El nombre de "
        "un sector o un substring no acredita una especialidad: un chatbot no implica "
        "agentes de IA y la secuencia de letras 'ia' no implica inteligencia artificial. "
        "No conviertas requisitos genericos, opciones no seleccionadas o exclusiones "
        "en el objeto adquirido. Sin evidencia suficiente omite la etiqueta. Si no "
        "puedes determinar el rubro usa primary_area=sin_clasificar, secondary_areas=[], "
        "work_types=[], specialties=[], support_status=needs_review y evidence=[]. "
        "support_status puede ser preliminary (clasificacion provisional), evidence_backed "
        "(etiquetas documentadas) o needs_review (ambiguedad/cobertura pendiente). "
        "Evidencia documental no certifica lectura exhaustiva, elegibilidad ni adjudicacion.\n"
    )


def normalize_commercial_classification(value: Any) -> dict:
    """Validate/project an existing block; never derive tags from an old summary."""
    return CommercialClassificationV1.model_validate(value).model_dump(mode="json")


def validate_commercial_payload(raw: Mapping[str, Any], context) -> None:
    if get_commercial_policy(context):
        if not isinstance(raw, Mapping) or "commercial_classification" not in raw:
            raise ValueError("Opt-in reading requires commercial classification")
        normalize_commercial_classification(raw["commercial_classification"])


def commercial_literal_errors(value: Any, blocks: list[dict]) -> list[dict]:
    """Check global pages and literal quotes against the exact retrieved text.

    This check does not assess visual layout or semantic applicability. Retain
    the paid body and mark the projection needs_review when a quote fails.
    """
    block = normalize_commercial_classification(value)
    source_blocks = [row for row in blocks if isinstance(row, Mapping)] if isinstance(blocks, list) else []
    normalized = lambda text: " ".join(text.split())
    return [{"kind": "unverified_commercial_citation", "citation_index": index}
            for index, item in enumerate(block["evidence"])
            if not any(row.get("global_page") == item["page"]
                       and isinstance(row.get("text"), str)
                       and normalized(item["citation"]) in normalized(row["text"])
                       for row in source_blocks)]
