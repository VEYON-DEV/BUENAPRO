from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from buenapro_worker.matching.jev_recommendations import (
    JEV_MODEL, build_jev_payload, build_profile_cards, build_recommendation_context,
    economic_comparison, expand_model_projection, model_projection, parse_jev_answers, score_match as _score_match, token_budget,
    build_small_recommendation_context, build_recommendation_batch, validate_recommendation_batch,
    expand_small_document_cards,
    validate_recommendations,
)

AT = datetime(2026, 10, 15, tzinfo=timezone.utc)


def profile(*, amount="95000", currency="PEN", reviewed=True):
    selected = {"amount_decimal": amount, "currency": currency, "label": "Monto contractual",
                "tax_basis": "includes_tax", "source_type": "contract", "page": 2,
                "quote": f"Monto total {currency} {amount}"}
    principal = {**selected, "amount_decimal": "120000", "label": "Otra base documental"}
    return {"razon_social": "DIN", "ruc": "123-private", "identity_json": {
        "services_declared_by_user": ["Software y automatización"], "rnp": "private"},
        "team_json": [{"nombre": "Private Name", "dni": "not-for-model", "grado": "Ingeniería",
                       "experiencia": [], "document_text": {"full_text": "NEVER"}}],
        "experience_json": [{"numero_contrato": "2024-1", "objeto": "Licencias y soporte software",
            "entidad": "Banco", "monto": amount, "moneda": currency,
            "modalidad_participacion": "individual", "documentary_summary": {
                "status": "reviewed" if reviewed else "pending_human_confirmation",
                "no_legal_accreditation": not reviewed, "amount_review_required": False,
                "amount_basis": {**selected, "document_id": "uuid-1", "sha256": "hash-1"},
                "sources": [{"document_id": "uuid-1", "sha256": "hash-1",
                    "status": "reviewed" if reviewed else "pending_human_confirmation",
                    "extraction": {"summary": "Suministro de licencias y soporte de software",
                                   "full_text": "NEVER", "amounts": [selected, principal],
                                   "principal_amount_index": 1,
                                   "conformity": "explicit_documentary_evidence",
                                   "facts": [{"field": "Fecha conformidad", "value": "2025-01-01",
                                              "quote": "Conformidad el 01/01/2025", "page": 3},
                                             {"field": "Avance", "value": "100%", "page": 3}],
                                   "warnings": [], "unknowns": []}}]}}]}


def facet(kind="economic_experience", label="Experiencia económica", **details):
    return {"facet": kind, "label": label, "required": True, "details_json": details}


def payload(*, p=None, facets=None):
    cards = build_profile_cards(p or profile())
    class LocalTestPayload(dict):
        pass
    request = LocalTestPayload(build_jev_payload(cards, {"objeto": "Licencias y soporte software"},
                                                facets if facets is not None else [facet(monto="100000", moneda="PEN", periodo_anios=3)]))
    request.full_cards = cards
    return request


def score_match(request, result, *, at):
    return _score_match(request, result, at=at, profile_cards=request.full_cards)


def response(p, *, status="backed", scope="compatible", score=4, confidence=.95):
    answers = {}
    for qid, question in p["questions"].items():
        if question["type"] == "score":
            answers[qid] = {"type": "score", "score": score, "confidence": confidence,
                            "probabilities": {str(i): 1. if i == 4 else 0. for i in range(5)}}
        else:
            selected = status if qid.endswith("_status") else scope
            answers[qid] = {"type": "choice", "choice": selected, "confidence": confidence,
                            "probabilities": {key: float(key == selected) for key in question["criteria"]}}
    return {"model": JEV_MODEL, "answers": answers, "usage": {"input_tokens": 2500, "output_tokens": 100}}


def test_cards_keep_all_sources_facts_and_different_amount_basis_without_text_or_ids():
    original = profile(); before = copy.deepcopy(original)
    cards = build_profile_cards(original)
    assert cards["coverage"]["document_count"] == 1
    assert cards["coverage"]["truncated"] is False
    assert cards["documents"][0]["principal_amount"]["amount_decimal"] == "120000"
    assert cards["contracts"][0]["amount_basis"]["amount_decimal"] == "95000"
    assert cards["contracts"][0]["amount_basis_supported"] is True
    assert cards["documents"][0]["facts"][1]["value"] == "100%"
    assert cards["reference_map"]["D1"]["document_id"] == "uuid-1"
    assert "NEVER" not in str(cards) and "not-for-model" not in str(cards)
    assert original == before


def test_all_43_cards_are_preserved_not_sliced():
    p = profile()
    records = []
    for i in range(43):
        item = copy.deepcopy(p["experience_json"][0]); summary = item["documentary_summary"]
        item["numero_contrato"] = str(i)
        summary["sources"][0]["document_id"] = f"uuid-{i}"
        summary["sources"][0]["sha256"] = f"sha-{i}"
        summary["amount_basis"]["document_id"] = f"uuid-{i}"
        records.append(item)
    p["experience_json"] = records
    cards = build_profile_cards(p)
    assert len(cards["documents"]) == len(cards["contracts"]) == 43
    assert cards["documents"][-1]["ref"] == "D43"
    plain = model_projection(cards, factor_defaults=False)
    factored = model_projection(cards)
    assert factored["shared_defaults"]["documents"]["status"] == "reviewed"
    assert factored["shared_defaults"]["contracts"]["amount_basis"]["currency"] == "PEN"
    assert expand_model_projection(factored) == plain
    assert len(factored["documents"]) == len(factored["contracts"]) == 43
    assert model_projection(factored) == factored
    assert len(str(factored)) < len(str(plain)) * .85


def test_defaults_preserve_explicit_false_zero_and_empty_exceptions_exactly():
    from buenapro_worker.matching.jev_recommendations import _factor_rows
    original = [{"id": 1, "optional": None, "flag": False, "amount": 0, "nested": {"currency": "PEN", "page": 1}},
                {"id": 2, "optional": "Known", "flag": False, "amount": 0, "nested": {"currency": "PEN", "page": 2}},
                {"id": 3, "optional": None, "flag": False, "amount": 0, "nested": {"currency": "PEN", "page": 3}}]
    rows = copy.deepcopy(original)
    defaults = _factor_rows(rows)
    assert defaults["optional"] is None
    assert "flag" not in defaults and "amount" not in defaults
    assert all(row["flag"] is False and row["amount"] == 0 for row in rows)
    assert expand_model_projection({"contracts": rows, "documents": [], "shared_defaults": {"contracts": defaults}})["contracts"] == original


def test_defaults_factor_repeated_nonempty_values_with_lossless_explicit_exceptions():
    from buenapro_worker.matching.jev_recommendations import _factor_rows
    original = [{"ref": "C1", "rubro": "Software", "basis": {"currency": "PEN", "amount": "20"}},
                {"ref": "C2", "rubro": "Software", "basis": {"currency": "PEN", "amount": "30"}},
                {"ref": "C3", "rubro": "Industrial", "basis": {"currency": "USD", "amount": "40"}}]
    rows = copy.deepcopy(original); defaults = _factor_rows(rows)
    assert defaults["rubro"] == "Software" and defaults["basis"]["currency"] == "PEN"
    assert rows[2]["rubro"] == "Industrial" and rows[2]["basis"]["currency"] == "USD"
    restored = expand_model_projection({"documents": [], "contracts": rows, "shared_defaults": {"contracts": defaults}})
    assert restored["contracts"] == original


def test_full_common_policy_occurs_once_not_in_each_atomic_question():
    from buenapro_worker.matching.jev_recommendations import COMMON_INSTRUCTIONS
    request = payload()
    assert request["state"]["fixed_rules"]["apply_to_every_question"] == COMMON_INSTRUCTIONS
    assert all(COMMON_INSTRUCTIONS not in q["instructions"] for q in request["questions"].values())
    assert all("state.fixed_rules" in q["instructions"] for q in request["questions"].values())
    assert "mixed unallocated amounts" in request["state"]["fixed_rules"]["economic_scope"]


def test_basis_is_not_assumed_from_principal_or_forged_amount():
    p = profile(); p["experience_json"][0]["documentary_summary"]["amount_basis"]["amount_decimal"] = "9000000"
    cards = build_profile_cards(p)
    assert cards["contracts"][0]["amount_basis_supported"] is False
    request = payload(p=p)
    comparison = score_match(request, response(request), at=AT)["economic"][0]
    assert comparison["score"] is None and comparison["subtotal"] is None


def test_missing_basis_stays_unknown_not_principal_selected():
    p = profile(); del p["experience_json"][0]["documentary_summary"]["amount_basis"]
    request = payload(p=p)
    comparison = score_match(request, response(request), at=AT)["economic"][0]
    assert comparison["score"] is None and comparison["subtotal"] is None


def test_administrative_paperwork_excluded_and_no_private_map_sent():
    request = payload(facets=[facet("rnp", "RNP vigente"), facet("ruc_status", "RUC activo"),
                              facet("proposal_document", "Formato propuesta económica"),
                              facet("specific_experience", "Licencias y soporte")])
    assert [r["source_index"] for r in request["state"]["requirements"]] == [4]
    assert len(request["state"]["administrative_checklist"]) == 3
    assert all(not c["score_impact"] for c in request["state"]["administrative_checklist"])
    assert "reference_map" not in request["state"]["profile"]
    assert "facts" not in request["state"]["profile"]["documents"][0]
    assert "objeto" not in request["state"]["profile"]["contracts"][0]
    assert all("r0001" not in qid for qid in request["questions"])
    result = score_match(request, response(request), at=AT)
    assert result["score"] == 100
    assert result["breakdown"]["economica"]["applicability"] == "not_applicable"
    assert result["coverage"]["required_weight"] == 50


def test_strict_offline_context_gate_has_no_silent_truncation_or_long_context():
    request = payload()
    budget = token_budget(request)
    assert budget["long_context"] is False
    huge = copy.deepcopy(request); huge["state"]["data"] = "x" * 64000
    with pytest.raises(ValueError, match="default context limit"):
        token_budget(huge)
    assert len(huge["state"]["data"]) == 64000
    with pytest.raises(ValueError, match="Invalid local token counter"):
        token_budget(request, token_counter=lambda text: True)
    assert token_budget(request, token_counter=lambda text: len(text) // 4)["counter"] == "local_proxy_with_margin"


def test_context_gate_does_not_retokenize_large_state_for_every_atomic_question():
    request = payload(); calls = []
    request["state"]["marker"] = "large-state-marker"
    for index in range(100):
        request["questions"][f"extra{index}"] = request["questions"]["rubro_scope"]
    def counter(text):
        calls.append(text)
        return len(text) // 4
    token_budget(request, token_counter=counter)
    assert sum("large-state-marker" in text for text in calls) == 2


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(model="wrong"),
    lambda d: d["answers"].pop("rubro_scope"),
    lambda d: d["answers"]["rubro_score"].update(score=True),
    lambda d: d["answers"]["rubro_score"].update(score=float("nan")),
    lambda d: d["answers"]["rubro_scope"].update(confidence=2),
    lambda d: d["answers"]["rubro_scope"].update(type="score"),
    lambda d: d["answers"]["rubro_scope"].update(probabilities={"compatible": .2}),
    lambda d: d["answers"]["rubro_scope"].update(probabilities={"invented": 1}),
    lambda d: d["usage"].update(input_tokens=-1),
])
def test_malformed_or_incomplete_paid_response_fails_closed(mutation):
    request = payload(); result = response(request); mutation(result)
    with pytest.raises(ValueError):
        parse_jev_answers(request, result)


def test_exact_decimal_near_threshold_not_waived_or_cliff_zero():
    request = payload(); result = score_match(request, response(request), at=AT)
    economic = result["economic"][0]
    assert economic["subtotal"] == "95000" and economic["shortfall"] == "5000"
    assert economic["score"] == 95 and economic["near_threshold"] is True
    assert economic["minimum_met_documentarily"] is False
    assert result["requisitos"][0]["estado"] == "no_cumple"
    assert result["requisitos"][0]["critico"] is True
    assert result["score"] == 97  #20*100+30*95, renormalized over applicable50.
    assert result["score_validated"] is False and result["legal_accreditation"] is False


def test_participation_uses_exact_share_and_no_currency_conversion():
    p = profile(); p["experience_json"][0]["porcentaje_participacion"] = "50"
    request = payload(p=p); result = score_match(request, response(request), at=AT)["economic"][0]
    assert result["subtotal"] == "47500" and result["score"] == 47.5
    p["experience_json"][0]["moneda"] = "USD"
    request = payload(p=p); result = score_match(request, response(request), at=AT)["economic"][0]
    assert result["subtotal"] is None and result["score"] is None


@pytest.mark.parametrize("change", [
    lambda c: c.pop("modalidad_participacion"),
    lambda c: c.update(porcentaje_participacion="150"),
    lambda c: c["documentary_summary"]["sources"][0]["extraction"].update(conformity="partial"),
    lambda c: c["documentary_summary"]["sources"][0]["extraction"]["facts"][0].update(value="2001-01-01"),
    lambda c: c["documentary_summary"]["sources"][0]["extraction"]["facts"][0].update(value="2027-01-01"),
])
def test_unvalidated_dates_execution_or_participation_do_not_invent_total(change):
    p = profile(); change(p["experience_json"][0]); request = payload(p=p)
    comparison = score_match(request, response(request), at=AT)["economic"][0]
    assert comparison["subtotal"] is None and comparison["score"] is None and comparison["uncertain"]


def test_pending_source_keeps_computable_subtotal_provisional():
    request = payload(p=profile(reviewed=False)); result = score_match(request, response(request), at=AT)
    assert result["economic"][0]["subtotal"] == "95000"
    assert result["source_review_required"] and result["provisional"]
    assert result["requisitos"][0]["estado"] == "requiere_revision"


def test_duplicate_contract_or_same_bytes_different_uuid_not_double_counted():
    p = profile(); record = copy.deepcopy(p["experience_json"][0]); record["numero_contrato"] = "OTHER"
    record["documentary_summary"]["sources"][0]["document_id"] = "uuid-2"
    record["documentary_summary"]["amount_basis"]["document_id"] = "uuid-2"
    p["experience_json"].append(record)
    request = payload(p=p); comparison = score_match(request, response(request), at=AT)["economic"][0]
    assert comparison["subtotal"] == "95000"
    assert comparison["unresolved"][0]["reasons"] == ["duplicate_contract_or_document"]


def test_scope_incompatible_is_excluded_while_unknown_is_not_zero():
    request = payload(); res = response(request)
    res["answers"]["r0001_C1_scope"].update(choice="incompatible", probabilities={"incompatible": 1})
    comparison = score_match(request, res, at=AT)["economic"][0]
    assert comparison["subtotal"] == "0" and comparison["score"] == 0
    res["answers"]["r0001_C1_scope"].update(choice="insufficient", probabilities={"insufficient": 1})
    comparison = score_match(request, res, at=AT)["economic"][0]
    assert comparison["subtotal"] is None and comparison["score"] is None


def test_unknown_required_economic_dimension_not_na_or_falsely_fulfilled():
    request = payload(facets=[facet(monto="100000", periodo_anios=3)])  #No sourcedcurrency.
    result = score_match(request, response(request), at=AT)
    assert result["breakdown"]["economica"]["applicability"] == "required"
    assert result["breakdown"]["economica"]["score"] is None
    assert result["coverage"] == {"known_weight": 20, "required_weight": 50, "partial_score": True}
    assert result["provisional"] and result["requisitos"][0]["estado"] == "requiere_revision"
    assert result["economic"][0]["subtotal"] is None


def test_no_minimum_required_does_not_add_economic_penalty():
    request = payload(facets=[facet(monto=0)])
    result = score_match(request, response(request), at=AT)
    assert result["economic"][0]["applicability"] == "not_applicable"
    assert result["breakdown"]["economica"]["applicability"] == "not_applicable"
    assert result["score"] == 100


def test_low_confidence_personnel_missing_evidence_not_proven_incapacity():
    request = payload(facets=[facet("key_personnel", "Ingeniero con 5 años IA")])
    result = score_match(request, response(request, status="insufficient", score=2, confidence=.5), at=AT)
    assert result["requisitos"][0]["estado"] == "requiere_revision"
    assert result["breakdown"]["perfiles"]["score"] == 50
    assert result["breakdown"]["perfiles"]["review_required"]
    assert result["score"] == 50 and result["provisional"]


def test_unknown_personnel_ordinal_retained_instead_of_inflated_rubro_only_100():
    request = payload(facets=[facet("key_personnel", "Profesional IA 5 años")])
    res = response(request, status="insufficient")
    res["answers"]["r0001_score"].update(score=2, confidence=.5)
    result = score_match(request, res, at=AT)
    assert result["breakdown"]["rubro"]["score"] == 100
    assert result["breakdown"]["perfiles"]["score"] == 50
    assert result["score"] == 75
    assert result["requisitos"][0]["estado"] == "requiere_revision"
    assert result["provisional"]


def test_required_technical_gap_stays_visible_with_high_commercial_score():
    request = payload(facets=[facet("license", "Autorización fabricante")])
    result = score_match(request, response(request, status="contradicted"), at=AT)
    assert result["score"] == 100
    assert result["technical_required_gaps"] == [1]
    assert result["requisitos"][0]["estado"] == "no_cumple" and result["requisitos"][0]["critico"]


@pytest.mark.parametrize("score,color", [(0, "rojo"), (29, "rojo"), (30, "ambar"), (80, "ambar"), (81, "verde"), (100, "verde")])
def test_score_colors_match_user_thresholds(score, color):
    request = payload(facets=[facet("license", "Licencia técnica")])
    result = score_match(request, response(request, score=score / 25), at=AT)
    assert result["score"] == score and result["color"] == color


def recommendation():
    return {"resumen": "Faltan S/5000 de evidencia compatible.", "requisitos": [
        {"source_index": 1, "evidencia": "D1: contrato licencias S/95000", "brecha": "S/5000",
         "accion": "Buscar otro contrato compatible con conformidad.", "evidence_refs": ["D1"]}],
        "acciones_recomendadas": ["Verificar evidencia adicional compatible."]}


def test_recommendations_are_explanations_only_with_exact_source_coverage_and_refs():
    request = payload(); score = score_match(request, response(request), at=AT)
    context = build_recommendation_context(request, score)
    assert context["score_is_immutable"] and context["rules_result"] == score
    assert validate_recommendations(recommendation(), request)["requisitos"][0]["evidence_refs"] == ["D1"]
    bad = recommendation(); bad["score"] = 100
    with pytest.raises(ValidationError):
        validate_recommendations(bad, request)
    bad = recommendation(); bad["requisitos"][0]["estado"] = "cumple"
    with pytest.raises(ValidationError):
        validate_recommendations(bad, request)
    bad = recommendation(); bad["requisitos"][0]["evidence_refs"] = ["uuid-other"]
    with pytest.raises(ValueError, match="unknown documentary"):
        validate_recommendations(bad, request)
    bad = recommendation(); bad["requisitos"] = []
    with pytest.raises(ValueError, match="coverage/order"):
        validate_recommendations(bad, request)


def test_explicit_economic_cutoff_required():
    request = payload()
    with pytest.raises(ValueError, match="timezone-aware"):
        economic_comparison(request["state"]["requirements"][0], request["state"]["profile"],
                            parse_jev_answers(request, response(request)), at=datetime(2026, 10, 15))


def test_local_facts_retained_and_frozen_projection_bound_before_scoring():
    request = payload()
    assert request.full_cards["documents"][0]["facts"]
    assert model_projection(request.full_cards) == request["state"]["profile"]
    unsupported = _score_match(request, response(request), at=AT)
    assert unsupported["economic"][0]["score"] is None
    assert unsupported["economic"][0]["subtotal"] is None
    altered = copy.deepcopy(request.full_cards)
    altered["documents"][0]["summary"] = "Other source"
    with pytest.raises(ValueError, match="frozen model projection"):
        _score_match(request, response(request), at=AT, profile_cards=altered)


def test_small_gemma_context_preserves_summaries_sources_amounts_alternatives_and_warnings():
    from buenapro_worker.matching.jev_recommendations import _merge_defaults
    p = profile()
    for index in range(1, 4):
        contract = copy.deepcopy(p["experience_json"][0])
        contract["numero_contrato"] = f"N{index}"
        source = contract["documentary_summary"]["sources"][0]
        source["document_id"] = f"id{index}"; source["sha256"] = f"sha{index}"
        contract["documentary_summary"]["amount_basis"]["document_id"] = f"id{index}"
        source["extraction"]["warnings"] = [f"Unique warning {index}"]
        source["extraction"]["unknowns"] = [f"Unknown {index}"]
        p["experience_json"].append(contract)
    request = payload(p=p); before = copy.deepcopy(request)
    score = score_match(request, response(request), at=AT)
    context = build_small_recommendation_context(request, score, request.full_cards)
    docs = expand_small_document_cards(context["profile"])
    assert len(docs) == 4
    for original, short in zip(request.full_cards["documents"], docs, strict=True):
        assert short["summary"] == original["summary"]
        assert short.get("warning_count", 0) == len(original["warnings"])
        assert short["unknowns"] == original["unknowns"]
        if original["warnings"]:
            assert short["warning_codes"] and short["warning_text_retained_locally"]
        assert short["ref"] == original["ref"]
        assert short["amount_basis"]["amount_decimal"] == "95000"
        assert short["principal_alternative"]["amount_decimal"] == "120000"
        assert short["dates"][0]["date"] == "2025-01-01"
    assert context["score_is_immutable"]
    assert context["evaluation"]["rules_result"]["score"] == score["score"]
    assert "economic_comparison" not in context["evaluation"]["rules_result"]["requisitos"][0]
    assert request == before  #No mutation to the frozen paid JEV request.


def test_economic_grouping_preserves_each_unresolved_contract_and_reason():
    from buenapro_worker.matching.jev_recommendations import _small_economic
    original = {"minimum": "100000", "subtotal": None, "shortfall": None, "uncertain": True,
                "unresolved": [{"contract_ref": "C1", "reasons": ["scope_not_confirmed", "currency_not_comparable"]},
                               {"contract_ref": "C2", "reasons": ["currency_not_comparable", "scope_not_confirmed"]},
                               {"contract_ref": "C3", "reasons": ["scope_not_confirmed"]}]}
    compact = _small_economic(original)
    restored = {ref: set(group["reasons"]) for group in compact["unresolved_groups"] for ref in group["contract_refs"]}
    assert restored == {row["contract_ref"]: set(row["reasons"]) for row in original["unresolved"]}
    assert len(compact["unresolved_groups"]) == 2
    assert compact["subtotal"] is None and compact["shortfall"] is None


def test_gemma_batch_shares_profile_and_strictly_binds_case_and_requirement_results():
    request = payload(); scoring = score_match(request, response(request), at=AT)
    small = build_small_recommendation_context(request, scoring, request.full_cards)
    batch = build_recommendation_batch({"one": small, "two": small})
    assert [row["case_key"] for row in batch["evaluations"]] == ["one", "two"]
    assert "profile" not in batch["evaluations"][0]
    results = {"evaluations": [{"case_key": "one", **recommendation()}, {"case_key": "two", **recommendation()}]}
    validated = validate_recommendation_batch(results, {"one": request, "two": request})
    assert list(validated) == ["one", "two"]
    results["evaluations"][1]["case_key"] = "one"
    with pytest.raises(ValueError, match="case coverage/order"):
        validate_recommendation_batch(results, {"one": request, "two": request})
    results["evaluations"][1]["case_key"] = "two"
    results["evaluations"][1]["score"] = 100
    with pytest.raises(ValidationError):
        validate_recommendation_batch(results, {"one": request, "two": request})
    altered = copy.deepcopy(small); altered["profile"]["company"]["razon_social"] = "Other"
    with pytest.raises(ValueError, match="profiles"):
        build_recommendation_batch({"one": small, "other": altered})
