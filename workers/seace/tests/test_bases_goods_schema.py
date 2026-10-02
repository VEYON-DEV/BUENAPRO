from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from buenapro_worker.extraction.gemini import (
    BASES_GOODS_INSTRUCTION,
    BASES_GOODS_PROMPT_VERSION,
    BASES_GOODS_SCHEMA_VERSION,
    BASES_PROMPT_VERSION,
    BASES_SCHEMA_VERSION,
    EETT_PROMPT_VERSION,
    GeminiExtractor,
    prompt_version_for_doc_class,
)
from buenapro_worker.extraction.schemas import (
    BasesGoodsExtractionV2,
    EettExtractionV1,
    TdrExtractionV2,
)


def extract(payload: dict, doc_class: str = "bases_good"):
    extractor = object.__new__(GeminiExtractor)
    extractor.prompts = {doc_class: "Original bases prompt" + BASES_GOODS_INSTRUCTION}
    extractor.client = MagicMock()
    extractor.client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps(payload),
        candidates=[],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=20),
    )
    result = extractor._extract_with_model(
        "gemini-3.1-flash-lite", b"%PDF-test", mime="application/pdf",
        doc_class=doc_class, max_output_tokens=16384,
    )
    return result, extractor.client.models.generate_content.call_args.kwargs["config"]


def test_bases_goods_schema_requires_structure_without_forcing_items() -> None:
    schema = BasesGoodsExtractionV2.model_json_schema()
    assert "goods" in schema["required"]
    assert "items" in schema["$defs"]["BasesGoodsV2"]["required"]
    with pytest.raises(ValidationError):
        BasesGoodsExtractionV2.model_validate({"summary": {"descripcion_corta": "Document"}})
    with pytest.raises(ValidationError):
        BasesGoodsExtractionV2.model_validate({"goods": {}})
    assert BasesGoodsExtractionV2.model_validate({"goods": {"items": []}}).goods.items == []


def test_missing_goods_cannot_silently_become_a_valid_bases_extraction() -> None:
    with pytest.raises(ValidationError):
        extract({"summary": {"descripcion_corta": "Known goods contract"}})


def test_explicit_empty_goods_preserves_review_and_new_version() -> None:
    result, config = extract({"goods": {"items": []}})
    assert result.requires_human_review
    assert result.prompt_version == BASES_GOODS_PROMPT_VERSION
    assert result.schema_version == BASES_GOODS_SCHEMA_VERSION
    assert config.response_schema is BasesGoodsExtractionV2
    assert "no inventes items" in config.system_instruction


def test_identified_goods_do_not_require_review() -> None:
    result, _ = extract({"goods": {"items": [{"nombre": "Servidor", "cantidad": 1}]}})
    assert not result.requires_human_review
    assert result.raw_json["goods"]["items"][0]["nombre"] == "Servidor"


def test_eett_and_service_behavior_and_versions_are_unchanged() -> None:
    assert "goods" not in EettExtractionV1.model_json_schema().get("required", [])
    assert EettExtractionV1.model_validate({}).goods.items == []
    assert prompt_version_for_doc_class("eett") == EETT_PROMPT_VERSION
    assert prompt_version_for_doc_class("bases_service") == BASES_PROMPT_VERSION
    result, config = extract({"summary": {"descripcion_corta": "Servicio"}}, "bases_service")
    assert config.response_schema is TdrExtractionV2
    assert result.schema_version == BASES_SCHEMA_VERSION
    assert not result.requires_human_review
