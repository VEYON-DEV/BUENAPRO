import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.matching.compact_context import build_compact_match_profile, compact_json
from buenapro_worker.matching.substantive_policy import split_match_requirements, bind_substantive_requirements, guard_economic_comparisons
from buenapro_worker.jobs.analyze_match import _econ_capacity, _derive_verdict, clamp_score


def profile():
    amount = {"amount_decimal": "40000", "currency": "USD", "tax_basis": "includes_tax", "page": 7, "quote": "Total USD 40000", "label": "Factura", "literal": "40,000", "source_type": "Factura"}
    return {"razon_social": "Empresa", "experience_json": [{"objeto": "Servidores", "actividades": "Declaración no verificada larga", "documentary_summary": {
        "status": "pending_human_confirmation", "original_fields": {"monto": 999999}, "amount_basis": amount,
        "sources": [{"document_id": "doc1", "sha256": "sha", "status": "pending_human_confirmation", "extraction": {
            "summary": "Suministro de servidores y licencias", "amounts": [amount, amount], "principal_amount_index": 1,
            "facts": [{"field": "Avance real", "value": "66%", "quote": "avance66%", "page": 8},
                      {"field": "Fecha de recepción", "value": "2024-09-01", "quote": "Recepción el 01/09/2024", "page": 8}],
            "conformity": "partial", "warnings": ["Monto mixto"], "unknowns": ["Subtotal hardware"],
        }}]}}], "econ_experience_json": {"servicios": 999999, "areas": [{"monto": 40000, "documents": [{"id": "doc1"}]}]}}


def test_local_projection_preserves_citations_currency_partial_execution_and_remaps_principal():
    original = profile(); before = copy.deepcopy(original)
    compact = build_compact_match_profile(original)
    source = compact["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]
    assert len(source["amounts"]) == 1 and source["principal_amount_index"] == 0
    assert source["amounts"][0] == original["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]["amounts"][0]
    assert source["facts"][0]["value"] == "66%" and source["warnings"] == ["Monto mixto"]
    assert compact["econ_experience_json"]["areas"] == [] and _econ_capacity(compact) is None
    assert "original_fields" not in compact_json(compact) and "Declaración no verificada larga" not in compact_json(compact)
    assert original == before


def test_submission_forms_are_separate_but_sla_and_technical_license_stay():
    facets = [{"facet": f, "label": label, "required": True, "details_json": {}} for f, label in [
        ("rnp", "RNP vigente"), ("proposal_document", "Formato de propuesta económica"),
        ("proposal_document", "Documentación técnica oficial plataforma (API, términos, SLA)"),
        ("company_certification", "Certificado del fabricante"), ("economic_experience", "Experiencia S/40000"),
        ("penalty_condition", "Penalidad")]]
    reqs, checklist = split_match_requirements(facets)
    assert [r["source_index"] for r in reqs] == [3, 4, 5]
    assert len(checklist) == 2 and all(r["score_impact"] is False for r in checklist)


def test_documentary_uncertainty_does_not_cap_capability_score_or_use_global_total():
    assert _derive_verdict([{"estado": "requiere_revision", "critico": True}]) == "gris"
    assert clamp_score("gris", 78) == 78
    assert clamp_score("gris", -4) == 0 and clamp_score("gris", 140) == 100


def test_source_coverage_and_reference_binding_fail_closed():
    expected = [{"source_index": 3, "tipo": "economic_experience", "obligatorio": True}]
    req = {"source_index": 3, "estado": "cumple", "critico": True, "evidence_refs": []}
    bound = bind_substantive_requirements([req], expected, build_compact_match_profile(profile()))
    assert bound[0]["estado"] == "requiere_revision"
    with pytest.raises(ValueError, match="Unknown"):
        bind_substantive_requirements([{**req, "evidence_refs": ["other"]}], expected, build_compact_match_profile(profile()))
    with pytest.raises(ValueError, match="coverage"):
        bind_substantive_requirements([], expected, {})


def test_sent_failure_does_not_dispatch_fallback_or_retry():
    from buenapro_worker.matching.analyzer import MatchAnalyzer
    from buenapro_worker.settings import Settings
    client = MagicMock(); client.models.generate_content.side_effect = RuntimeError("sent failure")
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    with patch("buenapro_worker.matching.analyzer.genai.Client", return_value=client):
        analyzer = MatchAnalyzer(settings)
        with pytest.raises(RuntimeError):
            analyzer.analyze(perfil=profile(), oportunidad={}, requisitos=[])
    client.models.generate_content.assert_called_once()


@pytest.mark.parametrize("kind", ["economic_experience", "specific_experience"])
def test_high_model_score_cannot_attest_amount_without_computable_evidence(kind):
    reqs = [{"source_index": 1, "estado": "cumple", "evidence_refs": ["doc1"]}]
    source = [{"source_index": 1, "tipo": kind, "detalle": {"monto": 40000}}]
    issues = guard_economic_comparisons(reqs, source, build_compact_match_profile(profile()))
    assert issues[0]["code"] == "missing_computable_evidence"
    assert reqs[0]["estado"] == "requiere_revision"


def test_currency_conversion_and_mixed_contract_subtotal_are_not_invented():
    proof = {"document_id": "doc1", "amount_index": 0, "currency": "PEN", "eligible_amount_decimal": "40000",
             "compatible_scope_quote": "Suministro de servidores y licencias", "full_amount_comparable": True, "period_supported": True}
    source = [{"source_index": 1, "tipo": "economic_experience", "detalle": {"monto": 40000}}]
    reqs = [{"estado": "cumple", "evidence_refs": ["doc1"], "economic_comparison": [proof]}]
    issues = guard_economic_comparisons(reqs, source, build_compact_match_profile(profile()))
    assert issues[0]["code"] == "unsupported_scope_currency_period_or_duplicate_amount"
    assert reqs[0]["estado"] == "requiere_revision"
    compact = build_compact_match_profile(profile())
    compact["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]["amounts"][0]["currency"] = "PEN"
    reqs = [{"estado": "cumple", "evidence_refs": ["doc1"], "economic_comparison": [{**proof, "eligible_amount_decimal": "30000", "full_amount_comparable": False}]}]
    assert guard_economic_comparisons(reqs, source, compact)


def test_compatible_documented_amount_sufficient_and_duplicate_document_rejected():
    compact = build_compact_match_profile(profile())
    source = [{"source_index": 1, "tipo": "specific_experience", "detalle": {"monto": 40000, "moneda": "USD"}}]
    proof = {"document_id": "doc1", "amount_index": 0, "currency": "USD", "eligible_amount_decimal": "40000",
             "compatible_scope_quote": "Suministro de servidores y licencias", "full_amount_comparable": True, "period_supported": True}
    reqs = [{"estado": "cumple", "evidence_refs": ["doc1"], "economic_comparison": [proof]}]
    assert guard_economic_comparisons(reqs, source, compact) == []
    reqs[0]["economic_comparison"].append(proof)
    assert guard_economic_comparisons(reqs, source, compact)


@pytest.mark.parametrize("category", ["personal_clave", "otro", "documentacion"])
def test_missing_essential_evidence_is_review_not_confirmed_incapacity(category):
    req = {"source_index": 1, "categoria": category, "estado": "no_cumple", "evidence_refs": [], "gap": "No existe evidencia de certificación", "critico": True}
    source = [{"source_index": 1, "tipo": "key_personnel", "obligatorio": True}]
    assert bind_substantive_requirements([req], source, {})[0]["estado"] == "requiere_revision"


AUDIT_AT = datetime(2026, 10, 13, tzinfo=timezone.utc)


def economic_fixture():
    compact = build_compact_match_profile(profile())
    experience = compact["experience_json"][0]
    experience["numero_contrato"] = "OC-400"
    experience["documentary_summary"]["sources"][0]["extraction"]["amounts"][0]["currency"] = "PEN"
    expected = [{"source_index": 1, "tipo": "specific_experience", "requisito": "Experiencia facturada de los últimos diez años",
                 "detalle": {"monto": 40000}}]
    proof = {"document_id": "doc1", "amount_index": 0, "currency": "PEN", "eligible_amount_decimal": "40000",
             "compatible_scope_quote": "Suministro de servidores y licencias", "full_amount_comparable": True, "period_supported": True}
    results = [{"source_index": 1, "estado": "cumple", "evidence_refs": ["doc1"], "economic_comparison": [proof]}]
    return compact, expected, results


@pytest.mark.parametrize("date", ["2010-01-01", "2026-10-14", "2024-02-30", "no identificada"])
@pytest.mark.parametrize("period", ["diez", "10", "(10)"])
def test_model_period_claim_requires_real_documentary_date_within_numeric_or_written_period(date, period):
    compact, expected, results = economic_fixture()
    expected[0]["requisito"] = f"Experiencia facturada durante los últimos {period} años"
    extracted = compact["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]
    extracted["facts"][1].update(value=date, quote=f"Recepción {date}")
    issues = guard_economic_comparisons(results, expected, compact, at=AUDIT_AT)
    assert issues and results[0]["estado"] == "requiere_revision"
    assert any("fecha documental" in reason for reason in issues[0]["reasons"])


@pytest.mark.parametrize("date", ["2016-10-13", "2026-10-13", "13/10/2016"])
def test_documentary_period_boundaries_are_inclusive(date):
    compact, expected, results = economic_fixture()
    extracted = compact["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]
    extracted["facts"][1].update(value=date, quote=f"Recepción {date}")
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT) == []


def test_declared_profile_date_or_unrelated_fact_cannot_prove_documentary_period():
    compact, expected, results = economic_fixture()
    compact["experience_json"][0]["fecha_conformidad"] = "2024-09-01"
    extracted = compact["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]
    extracted["facts"][1]["field"] = "Referencia de cliente"
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT)


def test_distinct_documents_from_same_contract_cannot_double_its_amount():
    compact, expected, results = economic_fixture()
    second = copy.deepcopy(compact["experience_json"][0])
    second["numero_contrato"] = "oc-400"
    second["documentary_summary"]["sources"][0]["document_id"] = "doc2"
    compact["experience_json"].append(second)
    expected[0]["detalle"]["monto"] = 80000
    results[0]["evidence_refs"].append("doc2")
    results[0]["economic_comparison"].append({**results[0]["economic_comparison"][0], "document_id": "doc2"})
    issues = guard_economic_comparisons(results, expected, compact, at=AUDIT_AT)
    assert issues and results[0]["estado"] == "requiere_revision"
    assert any("mismo contrato" in reason for reason in issues[0]["reasons"])
    # Two independent, fully quoted contracts may contribute their own amounts.
    second["numero_contrato"] = "OC-401"
    results[0]["estado"] = "cumple"
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT) == []


@pytest.mark.parametrize("scope", ["Servidores", "Clasificación inferida de computadoras", "Objeto inexistente de imágenes satelitales"])
def test_scope_must_be_substantive_literal_citation_not_inferred_label(scope):
    compact, expected, results = economic_fixture()
    extracted = compact["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]
    extracted["classification"] = {"rubro": scope, "specialty": scope, "inferred": True}
    results[0]["economic_comparison"][0]["compatible_scope_quote"] = scope
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT)


@pytest.mark.parametrize("source_field", ["fact", "classification_quote"])
def test_literal_scope_quote_may_come_from_fact_or_quoted_classification(source_field):
    compact, expected, results = economic_fixture()
    extracted = compact["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]
    scope = "Suministro de equipos informáticos"
    if source_field == "fact":
        extracted["facts"].append({"field": "Objeto", "value": scope, "quote": scope, "page": 3})
    else:
        extracted["classification"] = {"rubro": "Equipos", "quote": scope, "page": 3}
    results[0]["economic_comparison"][0]["compatible_scope_quote"] = scope
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT) == []


def test_required_amount_fallback_keeps_currency_and_evidence_binding_without_global_sum():
    compact, expected, results = economic_fixture()
    expected[0]["detalle"] = {"monto": None, "amount": None, "required_amount": 40000}
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT) == []
    expected[0]["detalle"]["required_amount"] = 40001
    compact["econ_experience_json"] = {"servicios": 999999999}
    issues = guard_economic_comparisons(results, expected, compact, at=AUDIT_AT)
    assert issues[0]["code"] == "compatible_amount_below_required"
    results[0]["estado"] = "cumple"
    expected[0]["detalle"]["required_amount"] = 40000
    results[0]["evidence_refs"] = []
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT)


def test_year_duration_without_monetary_minimum_is_not_an_economic_approval():
    compact, expected, results = economic_fixture()
    expected[0]["tipo"] = "general_experience"
    expected[0]["detalle"] = {"descripcion": "Experiencia de cinco años"}
    results[0]["economic_comparison"] = []
    assert guard_economic_comparisons(results, expected, compact, at=AUDIT_AT) == []


def test_absent_evidence_is_review_but_documented_conflicting_fact_is_not_rewritten():
    expected = [{"source_index": 1, "tipo": "key_personnel", "obligatorio": True}]
    absent = {"source_index": 1, "estado": "no_cumple", "gap": "No se encontró evidencia de certificación ITIL", "evidence_refs": []}
    assert bind_substantive_requirements([absent], expected, {})[0]["estado"] == "requiere_revision"
    contradicted = {"source_index": 1, "estado": "no_cumple", "gap": "La formación documentada corresponde a otra profesión", "evidence_refs": ["doc1"]}
    assert bind_substantive_requirements([contradicted], expected, build_compact_match_profile(profile()))[0]["estado"] == "no_cumple"
