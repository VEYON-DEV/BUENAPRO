from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from buenapro_worker.extraction.gemini import (
    PARTICIPATION_INSTRUCTION,
    GeminiExtractor,
    prompt_version_for_doc_class,
)
from buenapro_worker.extraction.schemas import (
    BasesGoodsExtractionV2,
    EettExtractionV1,
    ParticipationTermV1,
    TdrExtractionV2,
)
from buenapro_worker.normalization.participation import derive_participation


@pytest.mark.parametrize("schema,payload", [
    (TdrExtractionV2, {}),
    (EettExtractionV1, {}),
    (BasesGoodsExtractionV2, {"goods": {"items": []}}),
])
def test_historical_extractions_default_to_unknown(schema, payload):
    extracted = schema.model_validate(payload)
    assert extracted.participation.model_dump() == derive_participation({})
    assert all(
        term["status"] == "not_identified"
        for term in extracted.participation.model_dump().values()
    )


@pytest.mark.parametrize("status", ["permitted", "prohibited", "conditional", "not_identified"])
def test_status_enum_is_valid_and_exposed_in_schema(status):
    term = ParticipationTermV1(status=status, clause="Cláusula", page=2)
    assert term.status == status
    assert status in ParticipationTermV1.model_json_schema()["properties"]["status"]["enum"]


@pytest.mark.parametrize("page", [0, -1, True, "2", 1.5])
def test_schema_rejects_invalid_page(page):
    with pytest.raises(ValidationError):
        ParticipationTermV1(page=page)


def test_schema_rejects_unknown_status_and_defaults_are_independent():
    with pytest.raises(ValidationError):
        ParticipationTermV1(status="allowed")
    first, second = TdrExtractionV2(), TdrExtractionV2()
    first.participation.consorcio.conditions.append("Máximo dos integrantes")
    assert second.participation.consorcio.conditions == []
    assert first.participation.subcontratacion.conditions == []


@pytest.mark.parametrize("status", ["permitted", "prohibited", "conditional"])
@pytest.mark.parametrize("evidence", [
    {}, {"clause": ""}, {"clause": "   ", "page": 1},
    {"clause": "Se permite", "page": 0},
    {"clause": "Se permite", "page": True},
    {"clause": "Se permite", "page": "1"},
    {"clause": "Se permite", "page": 1.5},
    {"clause": 123, "page": 1}, {"page": 1},
])
def test_missing_or_invalid_evidence_downgrades_classification(status, evidence):
    result = derive_participation({"participation": {"consorcio": {"status": status, **evidence}}})
    assert result["consorcio"]["status"] == "not_identified"
    assert result["subcontratacion"]["status"] == "not_identified"


@pytest.mark.parametrize("status", ["allowed", "PERMITTED", None, [], {}])
def test_invalid_status_never_grants_permission(status):
    term = {"status": status, "clause": "Se permite consorcio", "page": 1}
    assert derive_participation({"participation": {"consorcio": term}})["consorcio"]["status"] == "not_identified"


@pytest.mark.parametrize("term_name", ["consorcio", "subcontratacion"])
@pytest.mark.parametrize("status", ["permitted", "prohibited", "conditional"])
def test_evidenced_status_is_preserved_for_each_term(term_name, status):
    term = {"status": status, "clause": "Cláusula específica de convocatoria.",
            "page": 3, "conditions": ["Autorización previa"] if status == "conditional" else []}
    assert derive_participation({"participation": {term_name: term}})[term_name] == term


def test_normalizer_preserves_evidence_and_explicit_conditions():
    result = derive_participation({"participation": {
        "consorcio": {"status": "permitted", "clause": " Se permite con dos integrantes. ",
                      "page": 8, "conditions": [" Máximo dos integrantes ", "", 42]},
        "subcontratacion": {"status": "prohibited", "clause": "No se permite subcontratar.",
                            "page": 9},
    }})
    assert result["consorcio"] == {
        "status": "conditional", "clause": "Se permite con dos integrantes.",
        "page": 8, "conditions": ["Máximo dos integrantes"],
    }
    assert result["subcontratacion"]["status"] == "prohibited"


@pytest.mark.parametrize("participation", [None, "permitted", [], {"consorcio": True}])
def test_malformed_participation_defaults_without_crashing(participation):
    assert derive_participation({"participation": participation}) == derive_participation({})


@pytest.mark.parametrize("doc_class,prompt_version,schema_version", [
    ("tdr", "tdr_extraction_v3", "tdr_extraction_schema_v3"),
    ("eett", "eett_extraction_v2", "eett_extraction_schema_v2"),
    ("bases_service", "bases_extraction_v2", "bases_extraction_schema_v2"),
    ("bases_good", "bases_goods_extraction_v3", "bases_goods_extraction_schema_v3"),
])
def test_all_document_classes_receive_participation_prompt_and_new_versions(
    doc_class, prompt_version, schema_version,
):
    settings = SimpleNamespace(
        gemini_api_key="test-only", gemini_model="gemini-3.1-flash-lite",
        gemini_fallback_model="gemini-2.5-flash",
    )
    payload = {"contract": {"objeto": "Compra"}, "goods": {"items": [{"nombre": "Bien"}]},
               "participation": {"consorcio": {
                   "status": "conditional", "clause": "Máximo dos integrantes.",
                   "page": 4, "conditions": ["Máximo dos integrantes"],
               }}}
    with patch("buenapro_worker.extraction.gemini.genai.Client") as client:
        client.return_value.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps(payload), candidates=[], usage_metadata=None,
        )
        extractor = GeminiExtractor(settings)
        assert extractor.prompts[doc_class].endswith(PARTICIPATION_INSTRUCTION)
        result = extractor.extract(b"%PDF-test", doc_class=doc_class)
        config = client.return_value.models.generate_content.call_args.kwargs["config"]
        assert "participation" in config.response_schema.model_json_schema()["properties"]
    assert prompt_version_for_doc_class(doc_class) == result.prompt_version == prompt_version
    assert result.schema_version == schema_version
    assert result.extraction.participation.consorcio.page == 4
    assert "anexos genericos" in PARTICIPATION_INSTRUCTION
    assert "ausencia de clausula" in PARTICIPATION_INSTRUCTION
    assert "pagina original" in PARTICIPATION_INSTRUCTION
