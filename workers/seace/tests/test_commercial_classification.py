"""Offline contracts: commercial opt-in never rewrites historical readings."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from buenapro_worker.extraction import gemini
from buenapro_worker.extraction.commercial_classification import (
    AREA_IDS, POLICY_KEY, SPECIALTY_IDS, TAXONOMY_VERSION, WORK_TYPE_IDS,
    CommercialClassificationV1, commercial_literal_errors, get_commercial_policy,
    normalize_commercial_classification, prompt_with_commercial,
)
from buenapro_worker.normalization.facets import derive_summary
from buenapro_worker.prod2.extraction_policy import (
    QUALIFICATION_FOCUS_POLICY, prompt_for_scope, schema_for_scope, validate_scope_payload,
)
from buenapro_worker.prod2.input_policy import (
    COMPACT_RETRIEVED_TRANSPORT_POLICY, EMBEDDING_RETRIEVED_TEXT_POLICY,
    OCR_TEXT_LAYOUT_REVIEW_POLICY, SOURCE_COVERAGE_POLICY,
    prompt_for_input, prompt_version_for_input, schema_for_input,
    schema_version_for_input, validate_retrieved_evidence,
)

OPT_IN = {POLICY_KEY: TAXONOMY_VERSION}
FOCUSED = {"llm1_scope_policy": QUALIFICATION_FOCUS_POLICY}
V5 = {**FOCUSED, "llm1_input_policy": EMBEDDING_RETRIEVED_TEXT_POLICY,
      "llm1_source_coverage_policy": SOURCE_COVERAGE_POLICY,
      "llm1_visual_review_policy": OCR_TEXT_LAYOUT_REVIEW_POLICY,
      "llm1_retrieved_transport_policy": COMPACT_RETRIEVED_TRANSPORT_POLICY}
DOCS = ("tdr", "eett", "bases_service", "bases_good")

# Captured before adding the opt-in. Compare exact prompt bytes and serialized
# JSON schemas for legacy, separate Qualification*, and retrieved V5 contracts.
BASELINE_HASHES = (
    ("63e500bd8b552e2c3b3679a5e8b65caef45a939ffa413cf8b677094d109712a0", "a3e8d7a952ea437d44850cfcfea439925a4cd3087b2dff7e1116d5a72714d3cd"),
    ("fc5f24f764e874fcec8a4cb760a4451baec59b40da714712abcfc85fad6eb55b", "29f5797cf5e8aa4edbf71822466f5cda2dcac8c5d0bab5b1a982637d0ce68e45"),
    ("8cc223a81ccb0966e8596879bb332f9698de486105a0054821ffd7f4cd28febd", "a3e8d7a952ea437d44850cfcfea439925a4cd3087b2dff7e1116d5a72714d3cd"),
    ("21abcec2223a3857a66c884f07a1b2819e0fa875405d953752d1182a22da7327", "272258082b1aca21c13bdb91200d56dfaa1805b711146daed90c0ac599311d71"),
    ("20b3e3fdeeac0ce57489044e36f383d8ca39bc1bc24f6953c5daf1450e0eb88c", "d853b9b4890452574a8e5eb9b26dcb7852413cf02aa3171ace07f2cec9f8066d"),
    ("5645a0aad84c1a4b48320a079f1f8b5e125b640d418646aa173109972ffd2034", "3c543761c59581ff338df98a2bee1f7bc0f28a044f514137b8d5dbadce8d6493"),
    ("1c780cf9d0c435b10ffc112b88d061eb42c1fcdf7ff706bccd5f1f954d50d858", "d853b9b4890452574a8e5eb9b26dcb7852413cf02aa3171ace07f2cec9f8066d"),
    ("f3e81c9789203f3e43ae7604fd84c3ad057be5944f827333c474e39c5195c10f", "b5d713595b4f7d9aaaee24a2c1a32ac096abe3d566add441e2ad22ebc6bb8aa4"),
    ("4b680b8b5224462462c00c2901fe754e51901a651ce96513cc0c45ad1f25793d", "1a4e21e6f6f8af6282c04c5798135ed2ca684627a50a83ebcdd9eeabab9ee7f3"),
    ("d87a1174476a8e5f0b07fc5eac2da4ab5e6d61198e27171ca90e0906a37e27b9", "c4f4d2e057e91c72c679befeae6386f9919635ab06e0abfa32218f70d6336341"),
    ("579633235085c4929934ec92b0ec28e64cca1c67b3848c36d6d1a3a8c66a8263", "1a4e21e6f6f8af6282c04c5798135ed2ca684627a50a83ebcdd9eeabab9ee7f3"),
    ("c6c33d6042254bffe9d6b19fc9f7decae1cd2dc86492e387cce4d45186289e93", "8d1f0caa82da4bed8d8fc1cfb16f231438ab551b6a06e092c09721b752e2e25a"),
)


def legacy_prompt(doc):
    filename = (gemini.BASES_PROMPT_FILENAME if doc.startswith("bases")
                else gemini.EETT_PROMPT_FILENAME if doc == "eett" else gemini.PROMPT_FILENAME)
    return (gemini.read_prompt(filename)
            + (gemini.BASES_GOODS_INSTRUCTION if doc == "bases_good" else "")
            + gemini.PARTICIPATION_INSTRUCTION)


def mixed_classification():
    quote = "Suministro de tableros eléctricos e implementación de software SCADA."
    labels = [("area", "industrial_electrica"), ("area", "software_ti"),
              ("work_type", "suministro_equipos"), ("work_type", "implementacion_integracion"),
              ("specialty", "tableros_electricos"), ("specialty", "plc_scada")]
    return {"taxonomy_version": TAXONOMY_VERSION, "primary_area": "industrial_electrica",
            "secondary_areas": ["software_ti"], "work_types": ["suministro_equipos", "implementacion_integracion"],
            "specialties": ["tableros_electricos", "plc_scada"], "support_status": "evidence_backed",
            "evidence": [{"axis": axis, "value": value, "citation": quote, "page": 4}
                         for axis, value in labels]}


@pytest.mark.parametrize("index", range(12))
def test_absence_preserves_exact_prompt_and_schema_bytes(index):
    context, doc = ({}, FOCUSED, V5)[index // 4], DOCS[index % 4]
    sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
    actual = (sha(prompt_for_input(doc, context, legacy_prompt(doc))),
              sha(json.dumps(schema_for_input(doc, context).model_json_schema(),
                             sort_keys=True, separators=(",", ":"))))
    assert actual == BASELINE_HASHES[index]
    if index < 4:
        assert prompt_version_for_input(doc, context) == gemini.prompt_version_for_doc_class(doc)
    elif index < 8:
        assert prompt_version_for_input(doc, context) == f"prod2_qualification_focus_{doc}_prompt_v1"
    else:
        assert prompt_version_for_input(doc, context) == f"prod2_qualification_retrieved_{doc}_prompt_v5"
        assert schema_version_for_input(doc, context) == f"prod2_qualification_retrieved_{doc}_schema_v1"


@pytest.mark.parametrize("context", [None, {}, {"unrelated": "ignored"}, FOCUSED])
def test_absent_wrapper_is_same_schema_object_and_prompt_identity(context):
    schema = schema_for_scope("tdr", context)
    assert "commercial_classification" not in schema.model_fields
    original = "  Literal previo.\n"
    assert prompt_with_commercial(original, context) is original
    if not context or "llm1_scope_policy" not in context:
        assert prompt_for_scope("tdr", context, original) is original


@pytest.mark.parametrize("context", [{POLICY_KEY: None}, {POLICY_KEY: "v2"}, {POLICY_KEY: True}, []])
def test_bad_policy_fails_before_client_or_generation(context):
    with pytest.raises(ValueError):
        get_commercial_policy(context)
    with pytest.raises(ValueError):
        schema_for_scope("tdr", context)


@pytest.mark.parametrize("context", [{}, FOCUSED, V5])
@pytest.mark.parametrize("doc", DOCS)
def test_opt_in_requires_valid_block_and_has_distinct_identity(context, doc):
    selected = {**context, **OPT_IN}
    legacy = schema_for_input(doc, context)
    schema = schema_for_input(doc, selected)
    payload = {"goods": {"items": []}} if doc == "bases_good" else {}
    with pytest.raises(ValidationError):
        schema.model_validate(payload)
    payload["commercial_classification"] = mixed_classification()
    result = schema.model_validate(payload).model_dump(mode="json")
    assert result["commercial_classification"] == mixed_classification()
    assert set(schema.model_fields) == set(legacy.model_fields) | {"commercial_classification"}
    assert prompt_version_for_input(doc, selected) == prompt_version_for_input(doc, context) + "_" + TAXONOMY_VERSION
    assert schema_version_for_input(doc, selected) == schema_version_for_input(doc, context) + "_" + TAXONOMY_VERSION
    assert "CLASIFICACION_COMERCIAL=" in prompt_for_input(doc, selected, legacy_prompt(doc))
    if context:
        assert "payment" not in schema.model_fields
        assert "proposal_documents" not in schema.model_fields["requirements"].annotation.model_fields


@pytest.mark.parametrize("field,value", [("taxonomy_version", "wrong"), ("primary_area", "chatbot"),
    ("secondary_areas", ["new_area"]), ("work_types", ["software_ti"]),
    ("specialties", ["inteligencia_artificial"]), ("support_status", "approved")])
def test_catalog_is_closed_on_every_axis(field, value):
    block = mixed_classification()
    block[field] = value
    with pytest.raises(ValidationError):
        normalize_commercial_classification(block)


@pytest.mark.parametrize("mutation", [
    lambda value: value["evidence"].pop(),
    lambda value: value["evidence"][0].update(page=0),
    lambda value: value["evidence"][0].update(page=True),
    lambda value: value["evidence"][0].update(page="4"),
    lambda value: value["evidence"][0].update(page=1.5),
    lambda value: value["evidence"][0].update(citation="  \n"),
    lambda value: value["evidence"][0].update(axis="specialty"),
    lambda value: value["evidence"][0].update(value="unknown"),
    lambda value: value["evidence"][0].update(document_uuid="invented"),
    lambda value: value.update(confidence=0.9),
    lambda value: value["secondary_areas"].append("industrial_electrica"),
    lambda value: value["work_types"].append("suministro_equipos"),
    lambda value: value["specialties"].append("rpa"),
])
def test_evidence_and_selected_labels_fail_closed(mutation):
    block = mixed_classification()
    mutation(block)
    with pytest.raises(ValidationError):
        normalize_commercial_classification(block)


def test_sin_clasificar_requires_review_and_entirely_empty_labels():
    block = {"taxonomy_version": TAXONOMY_VERSION, "primary_area": "sin_clasificar",
             "secondary_areas": [], "work_types": [], "specialties": [],
             "support_status": "needs_review", "evidence": []}
    assert normalize_commercial_classification(block) == block
    for field, value in (("support_status", "preliminary"), ("secondary_areas", ["software_ti"]),
                         ("work_types", ["consultoria"]), ("specialties", ["chatbot"])):
        with pytest.raises(ValidationError):
            normalize_commercial_classification({**block, field: value})


@pytest.mark.parametrize("quote", ["Servicio de consultoría.", "Implementar un chatbot para consultas.",
                                    "Tecnología y maquinaria eléctrica."])
def test_no_ai_from_substring_or_chatbot(quote):
    block = {"taxonomy_version": TAXONOMY_VERSION, "primary_area": "inteligencia_artificial",
             "secondary_areas": [], "work_types": [], "specialties": [],
             "support_status": "preliminary", "evidence": [
                 {"axis": "area", "value": "inteligencia_artificial", "citation": quote, "page": 1}]}
    with pytest.raises(ValidationError):
        normalize_commercial_classification(block)
    block.update(primary_area="software_ti", specialties=["agentes_ia"])
    block["evidence"][0]["value"] = "software_ti"
    block["evidence"].append({"axis": "specialty", "value": "agentes_ia", "citation": quote, "page": 1})
    with pytest.raises(ValidationError):
        normalize_commercial_classification(block)


@pytest.mark.parametrize("quote", ["Provisión de créditos de OpenAI.", "Implementar un large language model.",
                                    "Suministrar Gemini para la entidad.", "Desarrollo con generative AI."])
def test_explicit_ai_technology_is_not_rejected_by_language_or_brand(quote):
    block = {"taxonomy_version": TAXONOMY_VERSION, "primary_area": "inteligencia_artificial",
             "secondary_areas": [], "work_types": [], "specialties": [],
             "support_status": "preliminary", "evidence": [
                 {"axis": "area", "value": "inteligencia_artificial", "citation": quote, "page": 1}]}
    assert normalize_commercial_classification(block) == block


@pytest.mark.parametrize("quote", ["Implementación de agentes de IA.", "Desarrollo de agentes inteligentes.",
                                    "Implementación de LangGraph para el servicio.", "Integración con CrewAI."])
def test_explicit_agents_and_frameworks_are_supported(quote):
    block = {"taxonomy_version": TAXONOMY_VERSION, "primary_area": "software_ti",
             "secondary_areas": [], "work_types": [], "specialties": ["agentes_ia"],
             "support_status": "preliminary", "evidence": [
                 {"axis": "area", "value": "software_ti", "citation": quote, "page": 1},
                 {"axis": "specialty", "value": "agentes_ia", "citation": quote, "page": 1}]}
    assert normalize_commercial_classification(block) == block


def test_validated_mixed_rubros_project_without_mutating_paid_body():
    raw = {"commercial_classification": mixed_classification()}
    before = deepcopy(raw)
    summary = derive_summary(raw)
    assert summary["commercial_classification"] == raw["commercial_classification"]
    summary["commercial_classification"]["work_types"].append("consultoria")
    assert raw == before
    assert not any(key in raw["commercial_classification"] for key in ("document_uuid", "hash", "score"))


def test_literal_audit_uses_original_page_and_exact_retrieved_text_and_preserves_raw():
    block = mixed_classification()
    quote = block["evidence"][0]["citation"]
    proof = {"route": "retrieved_text", "context_sha256": "a" * 64, "retrieval_hash": "b" * 64,
             "selected_blocks": [{"block_id": "one", "global_page": 4, "text": quote}]}
    raw = {"commercial_classification": block, "llm1_input": proof, "selection_context": OPT_IN,
           "evidence_citations": [{"field_path": "commercial_classification.primary_area",
                                   "block_id": "one", "quote": quote, "page": 4}]}
    assert commercial_literal_errors(block, proof["selected_blocks"]) == []
    assert validate_retrieved_evidence(raw, proof)["citations_valid"]
    assert derive_summary(raw)["commercial_classification"]["support_status"] == "evidence_backed"
    raw["commercial_classification"]["evidence"][0]["page"] = 5
    before = deepcopy(raw)
    assert not validate_retrieved_evidence(raw, proof)["citations_valid"]
    assert derive_summary(raw)["commercial_classification"]["support_status"] == "needs_review"
    assert raw == before


@pytest.mark.parametrize("value", [None, "unknown", {}, {"primary_area": "software_ti"}])
def test_unversioned_extra_does_not_change_historical_retrieved_audit(value):
    proof = {"context_sha256": "a" * 64, "retrieval_hash": "b" * 64,
             "selected_blocks": [{"block_id": "one", "global_page": 4, "text": "Texto histórico."}]}
    raw = {"evidence_citations": [{"field_path": "summary.descripcion_corta",
                                  "block_id": "one", "quote": "Texto histórico.", "page": 4}]}
    expected = validate_retrieved_evidence(raw, proof)
    for context in (None, {}, FOCUSED, V5):
        historical = {**raw, "commercial_classification": value}
        if context is not None:
            historical["selection_context"] = context
        assert validate_retrieved_evidence(historical, proof) == expected


def test_old_summary_is_byte_identical_and_never_guessed_from_keywords():
    raw = {"contract": {"objeto": "Tableros industriales"}, "requirements": {"provider": [
        {"tipo": "specific_experience", "descripcion": "Tableros", "monto": 10000}]}}
    expected = {"descripcion_corta": None, "documentos_clave": [], "entregables_count": 0,
        "modalidad": "no_indicada", "observaciones_clave": [], "participation": {
            "consorcio": {"clause": None, "conditions": [], "page": None, "status": "not_identified"},
            "subcontratacion": {"clause": None, "conditions": [], "page": None, "status": "not_identified"}},
        "penalidad_tope_pct": None, "plazo_ejecucion_dias": None,
        "requirement_facets": ["specific_experience"], "roles_requeridos": [],
        "tipo_pago": "no_determinado", "valor_estimado": {"moneda": "PEN", "monto": None, "no_informado": True}}
    encoded = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    assert encoded(derive_summary(raw)) == encoded(expected)


@pytest.mark.parametrize("value", [None, "unknown", {}, {"primary_area": "software_ti"}])
def test_malformed_historical_extra_does_not_destroy_original_summary(value):
    raw = {"summary": {"descripcion_corta": "Texto histórico sin clasificar"}}
    expected = derive_summary(raw)
    assert derive_summary({**raw, "commercial_classification": value}) == expected


def test_malformed_historical_retrieval_metadata_requires_review_without_crashing():
    raw = {"commercial_classification": mixed_classification(),
           "llm1_input": {"route": "retrieved_text", "selected_blocks": [None, "invalid"]}}
    assert derive_summary(raw)["commercial_classification"]["support_status"] == "needs_review"


def test_explicit_general_gemini_configuration_is_local_and_no_default_activation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(gemini.genai, "Client", MagicMock(return_value=client))
    settings = SimpleNamespace(gemini_api_key="offline-placeholder", gemini_model="gemini-3.1-flash-lite")
    legacy = gemini.GeminiExtractor(settings)
    opted = gemini.GeminiExtractor(settings, policy_context=OPT_IN)
    assert legacy.prompts["tdr"] == legacy_prompt("tdr")
    assert "commercial_classification" not in legacy._response_schema("tdr").model_fields
    assert "commercial_classification" in opted._response_schema("tdr").model_fields
    assert opted._prompt_version("tdr") == legacy._prompt_version("tdr") + "_" + TAXONOMY_VERSION
    assert opted._schema_version("tdr") == legacy._schema_version("tdr") + "_" + TAXONOMY_VERSION
    opted._extract_with_model = MagicMock(side_effect=ValueError("invalid paid output"))
    with pytest.raises(ValueError):
        opted.extract(b"offline-placeholder")
    assert opted._extract_with_model.call_count == 1
    client.models.generate_content.assert_not_called()


def test_payload_requires_classification_only_when_explicitly_enabled():
    validate_scope_payload({}, {})
    with pytest.raises(ValueError):
        validate_scope_payload({}, OPT_IN)
    validate_scope_payload({"commercial_classification": mixed_classification()}, OPT_IN)


def test_packaged_json_has_same_ids_as_response_schema_without_additional_properties():
    schema = CommercialClassificationV1.model_json_schema()
    assert schema["properties"]["primary_area"]["enum"] == list(AREA_IDS)
    assert schema["properties"]["work_types"]["items"]["enum"] == list(WORK_TYPE_IDS)
    assert schema["properties"]["specialties"]["items"]["enum"] == list(SPECIALTY_IDS)
    assert "additionalProperties" not in json.dumps(schema)
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "commercial_taxonomy.v1.json" in config["tool"]["setuptools"]["package-data"]["buenapro_worker.extraction"]
