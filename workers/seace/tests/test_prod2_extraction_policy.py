"""Offline contracts for qualification scope, without provider/DB requests."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from buenapro_worker.extraction import gemini
from buenapro_worker.extraction.schemas import (
    BasesGoodsExtractionV2,
    EettExtractionV1,
    RequirementsV2,
    TdrExtractionV2,
)
from buenapro_worker.prod2.extraction_policy import (
    QUALIFICATION_FOCUS_POLICY,
    QualificationRequirementsV1,
    get_scope_policy,
    prompt_for_scope,
    prompt_version_for_scope,
    schema_for_scope,
    schema_version_for_scope,
    validate_scope_payload,
    validate_scope_policy,
)

FOCUSED = {"llm1_scope_policy": QUALIFICATION_FOCUS_POLICY}
LEGACY_VERSIONS = {
    "tdr": (gemini.PROMPT_VERSION, gemini.SCHEMA_VERSION),
    "eett": (gemini.EETT_PROMPT_VERSION, gemini.EETT_SCHEMA_VERSION),
    "bases_service": (gemini.BASES_PROMPT_VERSION, gemini.BASES_SCHEMA_VERSION),
    "bases_good": (gemini.BASES_GOODS_PROMPT_VERSION, gemini.BASES_GOODS_SCHEMA_VERSION),
}


@pytest.mark.parametrize("doc_class", LEGACY_VERSIONS)
def test_absent_policy_preserves_legacy_schema_prompt_and_versions(doc_class):
    expected_schema = (
        BasesGoodsExtractionV2 if doc_class == "bases_good"
        else EettExtractionV1 if doc_class == "eett" else TdrExtractionV2
    )
    legacy_prompt = "\n ORIGINAL bytes con acentos: ejecución y monto S/100. \n"
    for context in (None, {}, {"other_policy": "anything"}):
        assert schema_for_scope(doc_class, context) is expected_schema
        assert prompt_for_scope(doc_class, context, legacy_prompt) is legacy_prompt
        assert (
            prompt_version_for_scope(doc_class, context),
            schema_version_for_scope(doc_class, context),
        ) == LEGACY_VERSIONS[doc_class]


@pytest.mark.parametrize("doc_class", LEGACY_VERSIONS)
def test_focused_schema_removes_only_two_requested_fields_and_keeps_block_types(doc_class):
    original = schema_for_scope(doc_class, {})
    focused = schema_for_scope(doc_class, FOCUSED)
    assert set(focused.model_fields) == set(original.model_fields) - {"payment"}
    assert set(QualificationRequirementsV1.model_fields) == (
        set(RequirementsV2.model_fields) - {"proposal_documents"}
    )
    for name, field in original.model_fields.items():
        if name not in {"payment", "requirements"}:
            assert focused.model_fields[name].annotation == field.annotation
            assert focused.model_fields[name].is_required() == field.is_required()
    for name, field in QualificationRequirementsV1.model_fields.items():
        assert field.annotation == RequirementsV2.model_fields[name].annotation
    schema = focused.model_json_schema()
    assert "payment" not in schema["properties"]
    assert "proposal_documents" not in schema["$defs"]["QualificationRequirementsV1"]["properties"]
    assert "PaymentV2" not in schema["$defs"]


def material_payload():
    return {
        "contract": {"objeto": "Licencias y soporte", "valor_estimado_monto": 350000,
                     "plazo_texto": "24 meses", "moneda": "PEN"},
        "execution": {"actividades": ["Instalar y capacitar"],
                      "entregables": [{"nombre": "Instalacion", "plazo_dias": 30}]},
        "payment": {"tipo": "mensual", "plazo_pago_dias": 10},
        "requirements": {
            "provider": [
                {"tipo": "economic_experience", "monto": 310610,
                 "descripcion": "Experiencia S/310610 en 10 años; máximo 20 contratos; "
                                "acreditar con contratos y conformidades.",
                 "evidencia": "S/310610; diez años; contratos y conformidades (doc1, p.39)"},
                {"tipo": "economic_experience", "monto": 77652.50,
                 "descripcion": "Alternativa MYPE; todos los consorciados deben ser MYPE."},
            ],
            "key_personnel": [{"rol": "Especialista", "formacion": ["Ingeniero de sistemas"],
                               "experiencia_anios": 3, "colegiatura_requerida": True,
                               "capacitaciones": ["Certificacion exigida"]}],
            "equipment": [{"nombre": "Servidor", "cantidad": "2", "obligatorio": True}],
            "insurance": [{"tipo": "SCTR", "obligatorio": True}],
            "proposal_documents": ["Anexo de oferta"],
        },
        "penalties": [{"tipo": "mora", "formula": "0.10 x monto / (0.40 x plazo)",
                       "tope_pct": 10, "base_calculo": "monto del contrato"}],
        "contract_management": {"conformidad": "Area usuaria, diez dias", "vicios_ocultos": "1 año"},
        "summary": {"descripcion_corta": "Licencias y soporte por 24 meses.",
                    "observaciones_clave": ["Umbral alternativo MYPE condicionado."]},
        "participation": {"consorcio": {"status": "conditional", "clause": "Mayor al 40%",
                                        "page": 40, "conditions": ["Mayor al 40%, sin incluir 40%"]}},
        "goods": {"items": [{"nombre": "Licencia", "cantidad": 60, "unidad_medida": "usuario",
                              "especificaciones": ["24 meses", "compatibilidad documentada"]}],
                  "entrega": {"plazo_dias": 30}, "garantia": {"plazo_texto": "24 meses"}},
    }


@pytest.mark.parametrize("doc_class", LEGACY_VERSIONS)
def test_qualification_accreditation_and_all_material_blocks_survive_validation(doc_class):
    source = material_payload()
    original = schema_for_scope(doc_class, {}).model_validate(source).model_dump()
    expected = deepcopy(original)
    expected.pop("payment")
    expected["requirements"].pop("proposal_documents")
    result = schema_for_scope(doc_class, FOCUSED).model_validate(source).model_dump()
    assert result == expected
    assert len(result["requirements"]["provider"]) == 2
    assert "contratos y conformidades" in result["requirements"]["provider"][0]["descripcion"]
    assert result["participation"]["consorcio"]["conditions"] == ["Mayor al 40%, sin incluir 40%"]


def test_bases_goods_still_requires_explicit_goods_and_items_not_fabricated_content():
    focused = schema_for_scope("bases_good", FOCUSED)
    with pytest.raises(ValidationError):
        focused.model_validate({})
    with pytest.raises(ValidationError):
        focused.model_validate({"goods": {}})
    assert focused.model_validate({"goods": {"items": []}}).goods.items == []
    assert "goods" in focused.model_json_schema()["required"]
    assert "items" in focused.model_json_schema()["$defs"]["BasesGoodsV2"]["required"]


@pytest.mark.parametrize("value", [None, "", "future", {}, True, [QUALIFICATION_FOCUS_POLICY]])
def test_invalid_present_policy_fails_closed(value):
    with pytest.raises(ValueError, match="scope policy"):
        get_scope_policy({"llm1_scope_policy": value})
    with pytest.raises(ValueError, match="scope policy"):
        schema_for_scope("tdr", {"llm1_scope_policy": value})


def test_policy_context_validation_and_versioned_prompt_identity():
    assert validate_scope_policy(None) is None
    assert validate_scope_policy(QUALIFICATION_FOCUS_POLICY) == QUALIFICATION_FOCUS_POLICY
    with pytest.raises(ValueError):
        get_scope_policy([])
    with pytest.raises(ValueError):
        prompt_for_scope("unknown", FOCUSED, "original")
    for doc_class in LEGACY_VERSIONS:
        assert prompt_version_for_scope(doc_class, FOCUSED) != LEGACY_VERSIONS[doc_class][0]
        assert schema_version_for_scope(doc_class, FOCUSED) != LEGACY_VERSIONS[doc_class][1]


def test_prompt_preserves_accreditation_and_explicit_participation_evidence_without_hidden_cuts():
    prompt = prompt_for_scope("bases_good", FOCUSED, "never reuse payment prompt")
    assert "never reuse payment prompt" not in prompt
    assert "payment (pagos contractuales)" in prompt
    assert "requirements.proposal_documents" in prompt
    assert "Esta exclusion NO elimina la forma de acreditar experiencia" in prompt
    assert "No hay limite de cantidad de requisitos" in prompt
    assert "formula completa" in prompt and "reglas MYPE" in prompt
    assert '"No aplica" NO prueba permiso ni prohibicion' in prompt
    assert "not_identified, clause=null, page=null, conditions=[]" in prompt
    assert "pagina GLOBAL" in prompt
    assert "garantia" in prompt and "goods.items" in prompt
    assert prompt.count("POLITICA_DE_ALCANCE=") == 1


@pytest.mark.parametrize("payload, path", [
    ({"payment": None}, "payment"),
    ({"payment": {}}, "payment"),
    ({"requirements": {"proposal_documents": []}}, "requirements.proposal_documents"),
])
def test_raw_scope_validation_rejects_excluded_keys_without_erasing_paid_evidence(payload, path):
    before = deepcopy(payload)
    with pytest.raises(ValueError, match=path):
        validate_scope_payload(payload, FOCUSED)
    assert payload == before
    validate_scope_payload(payload, {})
    assert payload == before


def test_raw_scope_validation_accepts_provenance_metadata_but_not_invalid_policy():
    payload = {
        "requirements": {"provider": [{"monto": 100000}]},
        "source_document_manifest": [{"document_uuid": "example"}],
        "extraction_coverage": {"payment": "not_evaluated"},
    }
    before = deepcopy(payload)
    validate_scope_payload(payload, FOCUSED)
    assert payload == before
    with pytest.raises(ValueError, match="scope policy"):
        validate_scope_payload(payload, {"llm1_scope_policy": "unknown"})
    with pytest.raises(ValueError, match="JSON object"):
        validate_scope_payload([], FOCUSED)
