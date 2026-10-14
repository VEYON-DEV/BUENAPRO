from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from buenapro_worker.prod2.analysis import (
    JevDescriptionAnalyzer, JointMatchAnalysis, JointMatchAnalyzer, analyze_profile,
    extract_document, prepare_joint_profile, jev_request_hash, extraction_request_hash,
    match_request_hash, extraction_for_persistence, match_for_persistence, select_requirements_document,
    diagnose_joint_coverage, joint_response_schema, normalize_joint_response, JOINT_PROMPT_VERSION,
    joint_response_schema_v3, joint_response_schema_hash,
    analyze_substantive_profile, substantive_match_request_hash, SubstantiveJointMatchAnalyzer,
    assemble_cached_joint, assembly_request_hash, SUBSTANTIVE_PROMPT_VERSION, ASSEMBLY_POLICY_VERSION,
    CONCRETE_SUBSTANTIVE_PROMPT_VERSION, SUBSTANTIVE_POLICY_VERSION, _substantive_input,
)
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.matching.analyzer import read_analysis_prompt
from buenapro_worker.matching.profile_evidence import EVIDENCE_POLICY_VERSION


def settings():
    return SimpleNamespace(gemini_model="gemini-3.1-flash-lite", gemini_fallback_model="gemini-2.5-flash")


def commercial():
    return [{"descripcion": "Software y nube", "lineas": ["Licencias", "Software"]},
            {"descripcion": "PLC y software", "lineas": ["PLC", "Software"]}]


def jev_response(*, domain="informatica", information="claro", score=3.5, confidence=0.8):
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 1000, "output_tokens": 90},
            "answers": {
                "dominio": {"type": "choice", "choice": domain, "confidence": 0.9, "probabilities": {domain: 1}},
                "afinidad": {"type": "score", "score": score, "confidence": confidence, "probabilities": {"3": 0.5, "4": 0.5}},
                "informacion": {"type": "choice", "choice": information, "confidence": 0.9, "probabilities": {information: 1}},
            }}


def jev_client(response=None):
    client = MagicMock()
    client.post.return_value.status_code = 200
    client.post.return_value.json.return_value = response or jev_response()
    return client


def test_jev_commercial_union_score_cache_and_private_field_boundary():
    client, cache = jev_client(), {}
    analyzer = JevDescriptionAnalyzer("fake-test-key", client=client)
    profiles = commercial()
    profiles[0].update(dni="not-for-model", contracts=[{"amount": 999999}])
    result = analyzer.classify("Licencias software", commercial_profiles=profiles, cache=cache)
    assert result["affinity_index"] == 87.5
    assert result["cost_usd"] == pytest.approx(0.000042)
    assert result["scope"] == "commercial_union"
    assert result["usage"] == {"input_tokens": 1000, "output_tokens": 90}
    assert result["raw_response"] == jev_response()
    assert result["raw_answers"]["afinidad"]["type"] == "score"
    payload = client.post.call_args.kwargs["json"]
    assert set(payload["state"]["empresas"][0]) == {"descripcion", "lineas"}
    repeated = analyzer.classify("Licencias software", commercial_profiles=list(reversed(profiles)), cache=cache)
    assert repeated["cache_hit"] and repeated["incurred_cost_usd"] == 0
    assert client.post.call_count == 1
    repeated["raw_answers"]["afinidad"]["score"] = 0
    assert cache[result["cache_key"]]["raw_answers"]["afinidad"]["score"] == 3.5


def test_jev_ambiguity_is_separate_from_high_confidence_low_affinity():
    client = jev_client(jev_response(domain="ambiguo", information="insuficiente", score=1, confidence=0.95))
    result = JevDescriptionAnalyzer("fake-test-key", client=client).classify("Mantenimiento de sistemas", commercial_profiles=commercial())
    assert result["needs_detail"] and result["affinity_index"] == 25
    assert result["affinity_confidence"] == 0.95


@pytest.mark.parametrize("mutate", [
    lambda data: data["answers"].pop("informacion"),
    lambda data: data["answers"]["afinidad"].update(score=float("nan")),
    lambda data: data["answers"]["dominio"].update(choice="inventado"),
    lambda data: data["answers"]["afinidad"].update(probabilities={"3": 0.4}),
    lambda data: data["usage"].update(input_tokens=-1),
])
def test_jev_invalid_responses_are_not_cached(mutate):
    response = jev_response()
    mutate(response)
    cache = {}
    with pytest.raises(ValueError):
        JevDescriptionAnalyzer("fake-test-key", client=jev_client(response)).classify("Software", commercial_profiles=commercial(), cache=cache)
    assert not cache


def test_jev_errors_do_not_echo_body_or_credentials_and_profile_change_invalidates():
    client = jev_client()
    client.post.return_value.status_code = 401
    client.post.return_value.text = "fake-test-key secret response"
    analyzer = JevDescriptionAnalyzer("fake-test-key", client=client)
    with pytest.raises(RuntimeError, match="^Jev HTTP 401$"):
        analyzer.classify("Software", commercial_profiles=commercial())
    client.post.return_value.status_code = 200
    cache = {}
    analyzer.classify("Software", commercial_profiles=commercial(), cache=cache)
    changed = commercial()
    changed[0]["lineas"].append("Ciberseguridad")
    analyzer.classify("Software", commercial_profiles=changed, cache=cache)
    assert client.post.call_count == 3


def extractor(raw=None, review=False):
    client = MagicMock()
    client.extract.return_value = SimpleNamespace(raw_json=raw or {
        "requirements": {"provider": [{"tipo": "specific_experience", "descripcion": "Experiencia software", "obligatorio": True}]},
        "summary": {"descripcion_corta": "Software"}}, model="gemini-3.1-flash-lite",
        prompt_version="bases_extraction_v2", schema_version="bases_extraction_schema_v2",
        requires_human_review=review, input_tokens=100, output_tokens=30, cost_usd=0.00007)
    return client


def test_llm1_reuses_document_not_profile_and_changed_derivative_invalidates():
    client, cache = extractor(), {}
    result = extract_document(settings(), b"%PDF-first", object_type="service", original_sha256="a" * 64, extractor=client, cache=cache)
    assert not result["requires_human_review"] and len(result["facets"]) == 1
    repeat = extract_document(settings(), b"%PDF-first", object_type="service", original_sha256="a" * 64, extractor=client, cache=cache)
    assert repeat["cache_hit"] and client.extract.call_count == 1
    extract_document(settings(), b"%PDF-other", object_type="service", original_sha256="a" * 64, extractor=client, cache=cache)
    assert client.extract.call_count == 2
    assert client.extract.call_args.kwargs["doc_class"] == "bases_service"


def test_llm1_rejects_non_pdf_before_paid_call_and_flags_generic_bases():
    client = extractor()
    with pytest.raises(ValueError, match="verified PDF"):
        extract_document(settings(), b"<html>403</html>", object_type="good", extractor=client)
    client.extract.assert_not_called()
    generic = extractor({"requirements": {"proposal_documents": ["Anexo declaracion jurada"]}})
    result = extract_document(settings(), b"%PDF-generic", object_type="good", extractor=generic)
    assert result["requires_human_review"]
    assert generic.extract.call_args.kwargs["doc_class"] == "bases_good"


def test_loose_document_context_changes_paid_identity_and_reaches_exact_model_instruction():
    from buenapro_worker.extraction.gemini import read_prompt, BASES_PROMPT_FILENAME, PARTICIPATION_INSTRUCTION
    from buenapro_worker.prod2.document_set import DIRECT_DOCUMENTS_POLICY
    client = extractor()
    client.prompts = {"bases_service": read_prompt(BASES_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION}
    before = copy.deepcopy(client.prompts)
    pages = [{"document_uuid": "one", "start_page": 1, "end_page": 2}]
    context = {"document_scope": DIRECT_DOCUMENTS_POLICY,
        "document_set": [{"document_uuid": "one", "original_sha256": "a" * 64}],
        "excluded_documents": []}
    old = extraction_request_hash(settings(), b"%PDF-loose", object_type="service", original_sha256="a" * 64)
    new = extraction_request_hash(settings(), b"%PDF-loose", object_type="service", original_sha256="a" * 64,
        manifest=pages, document_context=context)
    result = extract_document(settings(), b"%PDF-loose", object_type="service", original_sha256="a" * 64,
        manifest=pages, document_context=context, extractor=client)
    assert new != old and result["cache_key"] == "prod2:llm1:" + new
    assert result["raw_extraction_json"]["document_scope"] == DIRECT_DOCUMENTS_POLICY
    assert result["raw_extraction_json"]["source_document_manifest"] == pages
    assert result["document_set"] == context["document_set"]
    assert client.prompts == before  # Reused extractor must not accumulate prompts.
    changed = copy.deepcopy(context)
    changed["document_set"][0]["original_sha256"] = "b" * 64
    assert extraction_request_hash(settings(), b"%PDF-loose", object_type="service", original_sha256="a" * 64,
        manifest=pages, document_context=changed) != new


def test_mixed_loose_documents_or_deferred_archives_keep_a_review_flag():
    from buenapro_worker.extraction.gemini import read_prompt, BASES_PROMPT_FILENAME, PARTICIPATION_INSTRUCTION
    from buenapro_worker.prod2.document_set import DIRECT_DOCUMENTS_POLICY
    for document_set, excluded in [([{"document_uuid": "one"}, {"document_uuid": "two"}], []),
                                    ([{"document_uuid": "one"}], [{"document_uuid": "zip", "reason": "archive_deferred"}])]:
        client = extractor()
        client.prompts = {"bases_service": read_prompt(BASES_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION}
        result = extract_document(settings(), b"%PDF-mixed", object_type="service",
            manifest=[{"start_page": 1, "end_page": 2}],
            document_context={"document_scope": DIRECT_DOCUMENTS_POLICY, "document_set": document_set,
                              "excluded_documents": excluded}, extractor=client)
        assert result["requires_human_review"] is True


def joint_profile():
    return {"members": [
        {"profile_id": "veyon", "profile": {"razon_social": "VEYON", "experience_json": [{"descripcion": "software", "amount": 100}],
                                               "econ_experience_json": {"legacy": 100}}},
        {"profile_id": "dinaut", "profile": {"razon_social": "Dinaut", "experience_json": [{"descripcion": "PLC", "amount": 200}],
                                                "econ_experience_json": {"legacy": 200}}},
    ]}


def extraction():
    return {"cache_key": "llm1-fixture", "original_sha256": "a" * 64,
            "requires_human_review": False, "summary_json": {"participation": {"consorcio": {"status": "not_identified"}}},
            "facets": [{"facet": "specific_experience", "label": "Experiencia software", "required": True,
                        "details": {"descripcion": "Software"}, "evidence": []}]}


def match_analyzer(*, state="cumple", critical=True, ids=None, refs=None, actions=None):
    client = MagicMock()
    ids = ids or ["r0001"]
    requirements = [{"requirement_id": rid, "requisito": "Experiencia software", "categoria": "experiencia", "estado": state,
                     "critico": critical, "gap": None, "accion": None,
                     "member_gaps": [{"profile_id": pid, "estado": "requiere_revision", "gap": "Verificar evidencia", "accion": "Revisar",
                                      "evidence_refs": refs.get(pid, []) if refs else []} for pid in ("veyon", "dinaut")]}
                    for rid in ids]
    raw = {"veredicto": "verde", "score": 99, "resumen": "Afinidad con evidencia pendiente",
           "requisitos": requirements, "acciones_recomendadas": actions or []}
    client.analyze.return_value = SimpleNamespace(analysis=JointMatchAnalysis.model_validate(raw), raw_json=raw,
        model="gemini-3.1-flash-lite", prompt_version=JOINT_PROMPT_VERSION, input_tokens=200, output_tokens=100, cost_usd=0.0002)
    return client


def test_joint_preparation_preserves_member_economics_and_filters_demo_private_data():
    profile = joint_profile()
    profile["members"][0]["profile"]["team_json"] = [{"is_demo": True, "role": "ficticio"}, {"role": "QA", "dni": "hidden"}]
    output = prepare_joint_profile(profile)
    profiles = {member["profile_id"]: member["profile"] for member in output["members"]}
    assert profiles["veyon"]["econ_experience_json"] == {"legacy": 100}
    assert profiles["dinaut"]["econ_experience_json"] == {"legacy": 200}
    assert profiles["veyon"]["team_json"] == [{"role": "QA"}]
    assert not output["joint_qualification_verified"]
    assert output["evidence_catalog"]["dinaut:experience_json:0001"]["profile_id"] == "dinaut"


def test_joint_duplicate_members_rejected():
    profile = joint_profile()
    profile["members"][1]["profile_id"] = "veyon"
    with pytest.raises(ValueError, match="duplicated"):
        prepare_joint_profile(profile)


def test_llm2_is_one_call_for_two_members_with_legal_guard_and_cache():
    client, cache = match_analyzer(), {}
    result = analyze_profile(settings(), extraction(), profile=joint_profile(), opportunity={"codigo": "CP-1"}, analyzer=client, cache=cache)
    assert result["coverage_valid"] and result["status"] == "analyzed"
    # The evidence/eligibility guard remains gray without capping capability.
    assert result["verdict"] == "gris" and result["score"] == 99
    assert result["model_proposed_score"] == 99 and not result["joint_qualification_verified"]
    assert result["requisitos"][0]["estado"] == "requiere_revision"
    assert len(client.analyze.call_args.kwargs["perfil"]["members"]) == 2
    assert client.analyze.call_args.kwargs["requisitos"][0]["requirement_id"] == "r0001"
    repeat = analyze_profile(settings(), extraction(), profile=joint_profile(), opportunity={"codigo": "CP-1"}, analyzer=client, cache=cache)
    assert repeat["cache_hit"] and repeat["incurred_cost_usd"] == 0 and client.analyze.call_count == 1


@pytest.mark.parametrize("ids", [["r9999"], ["r0001", "r0001"]])
def test_llm2_wrong_or_duplicate_requirement_ids_force_review(ids):
    result = analyze_profile(settings(), extraction(), profile=joint_profile(), opportunity={}, analyzer=match_analyzer(ids=ids))
    assert result["status"] == "review_required" and not result["coverage_valid"]
    assert result["verdict"] == "gris"
    assert result["coverage_diagnostics"]["missing_requirement_ids"] or result["coverage_diagnostics"]["duplicate_requirement_ids"]


def test_llm2_cannot_cite_another_members_evidence():
    client = match_analyzer(refs={"veyon": ["dinaut:experience_json:0001"]})
    result = analyze_profile(settings(), extraction(), profile=joint_profile(), opportunity={}, analyzer=client)
    assert not result["coverage_valid"] and result["status"] == "review_required"
    assert result["coverage_diagnostics"]["evidence_errors"][0]["reason"] == "other_member_reference"


def test_llm2_review_required_document_never_calls_model():
    document = extraction()
    document["requires_human_review"] = True
    client = match_analyzer()
    result = analyze_profile(settings(), document, profile=joint_profile(), opportunity={}, analyzer=client)
    assert result["reason"] == "document_requires_review" and result["incurred_cost_usd"] == 0
    client.analyze.assert_not_called()


def test_llm2_prohibited_participation_action_is_guarded_and_profile_change_invalidates():
    document = extraction()
    document["summary_json"]["participation"]["consorcio"]["status"] = "prohibited"
    client = match_analyzer(actions=["Formar consorcio para completar experiencia"])
    cache = {}
    result = analyze_profile(settings(), document, profile=joint_profile(), opportunity={}, analyzer=client, cache=cache)
    assert result["acciones_recomendadas"] == ["Buscar alternativa permitida por las bases"]
    changed = joint_profile()
    changed["members"][0]["profile"]["experience_json"][0]["amount"] = 150
    other = analyze_profile(settings(), document, profile=changed, opportunity={}, analyzer=client, cache=cache)
    assert other["profile_hash"] != result["profile_hash"] and client.analyze.call_count == 2


def test_llm2_confirmed_critical_incompatibility_stays_red():
    result = analyze_profile(settings(), extraction(), profile=joint_profile(), opportunity={}, analyzer=match_analyzer(state="no_cumple"))
    assert result["verdict"] == "rojo" and result["score"] == 29


def test_joint_schema_does_not_invent_no_aplica():
    raw = match_analyzer().analyze.return_value.raw_json
    raw = copy.deepcopy(raw)
    raw["requisitos"][0]["estado"] = "no_aplica"
    with pytest.raises(ValidationError):
        JointMatchAnalysis.model_validate(raw)


def test_offline_reservation_hashes_equal_paid_call_keys_and_include_real_inputs():
    jev_hash = jev_request_hash("Software", commercial_profiles=commercial())
    jev = JevDescriptionAnalyzer("fake-test-key", client=jev_client()).classify("Software", commercial_profiles=commercial())
    assert jev["cache_key"].split(":")[-1] == jev_hash
    sha = extraction_request_hash(settings(), b"%PDF-fixture", object_type="service")
    bundle = extract_document(settings(), b"%PDF-fixture", object_type="service", extractor=extractor())
    assert bundle["cache_key"].split(":")[-1] == sha
    request_hash = match_request_hash(settings(), bundle, profile=joint_profile(), opportunity={"codigo": "CP1"})
    result = analyze_profile(settings(), bundle, profile=joint_profile(), opportunity={"codigo": "CP1"}, analyzer=match_analyzer())
    assert result["cache_key"].split(":")[-1] == request_hash
    changed = copy.deepcopy(bundle)
    changed["summary_json"]["participation"] = {"consorcio": {"status": "prohibited"}}
    assert match_request_hash(settings(), changed, profile=joint_profile(), opportunity={"codigo": "CP1"}) != request_hash
    assert extraction_request_hash(settings(), b"%PDF-fixture", object_type="good") != sha


def test_persistence_mappers_keep_guarded_output_and_reject_incomplete_evaluation():
    document = extract_document(settings(), b"%PDF-fixture", object_type="service", extractor=extractor())
    result = extraction_for_persistence(document)
    assert result.raw_json == document["raw_extraction_json"] and result.model == document["model"]
    result.raw_json["summary"]["descripcion_corta"] = "mutated"
    assert document["raw_extraction_json"]["summary"]["descripcion_corta"] == "Software"
    evaluation = analyze_profile(settings(), document, profile=joint_profile(), opportunity={}, analyzer=match_analyzer())
    payload = match_for_persistence(evaluation)
    assert payload["score"] == 99 and payload["verdict"] == "gris"
    assert payload["breakdown_json"]["meta"]["source_key"] == "seace_prod2"
    assert not payload["breakdown_json"]["meta"]["joint_qualification_verified"]
    assert payload["breakdown_json"]["meta"]["member_names"] == {"veyon": "VEYON", "dinaut": "Dinaut"}
    assert set(payload["breakdown_json"]["meta"]["commercial_context_label"].split(" + ")) == {"VEYON", "Dinaut"}
    assert payload["breakdown_json"]["meta"]["legal_structure"] == "convenio declarado; elegibilidad conjunta/consorcio no verificada"
    assert payload["missing_actions_json"][0]["estado"] == "requiere_revision"
    assert {member["profile_id"] for member in payload["missing_actions_json"][0]["member_gaps"]} == {"veyon", "dinaut"}
    evaluation["coverage_valid"] = False
    with pytest.raises(ValueError, match="incomplete"):
        match_for_persistence(evaluation)


def test_real_joint_schema_envelope_with_fake_sdk_preserves_ids_and_usage():
    analyzer = object.__new__(JointMatchAnalyzer)
    analyzer.settings = settings()
    analyzer.prompt, analyzer.prompt_version = "test-only prompt", JOINT_PROMPT_VERSION
    analyzer.client = MagicMock()
    raw = match_analyzer().analyze.return_value.raw_json
    analyzer.client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps(raw), candidates=[], usage_metadata=SimpleNamespace(prompt_token_count=500, candidates_token_count=100))
    payload = json.dumps({"requisitos": [{"requirement_id": "r0001"}], "perfil": {"members": [{"profile_id": "veyon"}, {"profile_id": "dinaut"}]}})
    result = analyzer._analyze_with_model("gemini-3.1-flash-lite", payload)
    assert result.analysis.requisitos[0].requirement_id == "r0001"
    assert result.input_tokens == 500 and result.output_tokens == 100
    config = analyzer.client.models.generate_content.call_args.kwargs["config"]
    schema = config.response_schema.model_json_schema()
    assert schema["properties"]["requisitos"]["type"] == "array" and config.temperature == 0
    assert isinstance(result.raw_json["requisitos"], list) and isinstance(result.analysis.requisitos, list)


def test_missing_member_response_is_review_required_not_a_publishable_match():
    analyzer = match_analyzer()
    analyzer.analyze.return_value.analysis.requisitos[0].member_gaps.pop()
    result = analyze_profile(settings(), extraction(), profile=joint_profile(), opportunity={}, analyzer=analyzer)
    assert result["status"] == "review_required" and not result["coverage_valid"]
    with pytest.raises(ValueError, match="incomplete"):
        match_for_persistence(result)


def test_member_display_metadata_is_from_snapshot_and_can_enrich_old_cache():
    profile = joint_profile()
    analyzer, cache = match_analyzer(), {}
    result = analyze_profile(settings(), extraction(), profile=profile, opportunity={}, analyzer=analyzer, cache=cache)
    cache[result["cache_key"]].pop("member_names")
    cached = analyze_profile(settings(), extraction(), profile=profile, opportunity={}, analyzer=analyzer, cache=cache)
    assert cached["member_names"] == {"veyon": "VEYON", "dinaut": "Dinaut"}
    assert analyzer.analyze.call_count == 1
    result.pop("member_names")
    with pytest.raises(ValueError, match="evaluated profile snapshot"):
        match_for_persistence(result)
    assert match_for_persistence(result, profile=profile)["breakdown_json"]["meta"]["member_names"]["veyon"] == "VEYON"
    profile["members"][0]["profile"]["razon_social"] = "Unrelated company"
    with pytest.raises(ValueError, match="does not match"):
        match_for_persistence(result, profile=profile)


def test_joint_schema_serializes_with_installed_gemini_developer_sdk_offline():
    # This is the SDK's actual schema transform, not a mocked generate_content.
    # It confirms request compatibility, not remote model acceptance or quality.
    from google.genai import _transformers
    schema = _transformers.t_schema(SimpleNamespace(vertexai=False), JointMatchAnalysis)
    serialized = schema.model_dump(exclude_none=True)
    requirement = serialized["properties"]["requisitos"]["items"]
    gap = requirement["properties"]["member_gaps"]["items"]
    assert {"requirement_id", "member_gaps"} <= set(requirement["required"])
    assert {"profile_id", "estado"} <= set(gap["required"])
    assert set(gap["properties"]["estado"]["enum"]) == {"cumple", "cumple_con_accion", "no_cumple", "requiere_revision"}
    assert "$defs" not in serialized and "$ref" not in json.dumps(serialized)
    assert "additionalProperties" not in json.dumps(serialized)


def document(uid, filename, published="01/10/2026 10:00", kind="Otros documentos"):
    return {"uuid": uid, "filename": filename, "type": kind, "stage": "Convocatoria", "published_at": published}


def test_requirements_selector_uses_latest_integrated_archive_before_old_pdf():
    rows = [document("old", "BASES_INTEGRADAS.pdf"),
            document("new", "BASES_INTEGRADAS.zip", "05/10/2026 10:00"),
            document("tdr", "TDR.pdf", "05/10/2026 12:00")]
    selected = select_requirements_document(rows, inventory_complete=True)
    assert selected["uuid"] == "new" and selected["filename"].endswith(".zip")
    selected["filename"] = "mutated"
    assert rows[1]["filename"] == "BASES_INTEGRADAS.zip"


def test_requirements_selector_prefers_specific_requirements_and_supports_repository_dates():
    rows = [document("admin", "BASES_ADMINISTRATIVAS.pdf", "05/10/2026 12:00"),
            document("tdr", "Términos de referencia.docx", "04/10/2026 10:00"),
            document("eett", "Especificaciones técnicas.pdf", "2026-10-05T16:00:00+00:00")]
    assert select_requirements_document(rows, inventory_complete=True)["uuid"] == "eett"


@pytest.mark.parametrize("filename", ["RESUMEN_EJECUTIVO_BASES.pdf", "OFERTA_TDR.pdf", "ACTA_BUENA_PRO_BASES.pdf",
                                      "PROPUESTA_BASES.pdf", "PLIEGO_ABSOLUTORIO_BASES.pdf", "CONTRATO_TDR.pdf"])
def test_requirements_selector_does_not_classify_awards_offers_or_summaries_as_bases(filename):
    assert select_requirements_document([document("irrelevant", filename)], inventory_complete=True) is None


def test_requirements_selector_requires_complete_inventory_and_rejects_uncertain_version():
    with pytest.raises(ValueError, match="complete document inventory"):
        select_requirements_document([document("tdr", "TDR.pdf")], inventory_complete=False)
    rows = [document("known", "TDR.pdf"), document("unknown", "TDR.zip", "")]
    with pytest.raises(ValueError, match="publication date missing"):
        select_requirements_document(rows, inventory_complete=True)
    rows[1]["published_at"] = "not-a-date"
    with pytest.raises(ValueError, match="publication date"):
        select_requirements_document(rows, inventory_complete=True)


def test_requirements_selector_pdf_twin_and_complementary_files():
    rows = [document("pdf", "BASES_INTEGRADAS.pdf"), document("zip", "BASES_INTEGRADAS.zip")]
    assert select_requirements_document(rows, inventory_complete=True)["uuid"] == "pdf"
    rows = [document("tdr", "TDR.pdf"), document("eett", "EETT.pdf")]
    with pytest.raises(ValueError, match="complementary files"):
        select_requirements_document(rows, inventory_complete=True)


def test_saved_analysis_coverage_diagnostics_preserve_missing_evaluations():
    document = extraction()
    document["facets"].extend([{**copy.deepcopy(document["facets"][0]), "facet": "proposal_document"} for _ in range(2)])
    rows = [item.model_dump() for item in match_analyzer(ids=["r0001", "r0003"]).analyze.return_value.analysis.requisitos]
    before = copy.deepcopy(rows)
    report = diagnose_joint_coverage(document, profile=joint_profile(), evaluated=rows)
    assert not report["coverage_valid"] and report["missing_requirement_ids"] == ["r0002"]
    assert report["expected_requirement_count"] == 3 and report["returned_requirement_count"] == 2
    assert not report["member_errors"] and not report["evidence_errors"]
    assert rows == before


def wire_response(analysis):
    wire = copy.deepcopy(analysis)
    rows = {}
    for row in wire["requisitos"]:
        row["member_gaps"] = {member["profile_id"]: member for member in row["member_gaps"]}
        rows[row["requirement_id"]] = row
    wire["requisitos"] = rows
    return wire


def test_dynamic_joint_schema_requires_every_requirement_and_exact_member_literal():
    schema = joint_response_schema(["r0001", "r0002"], ["veyon", "dinaut"])
    raw = wire_response(match_analyzer(ids=["r0001", "r0002"]).analyze.return_value.raw_json)
    assert schema.model_validate(raw).model_dump(by_alias=True)["requisitos"]["r0001"]["member_gaps"]["veyon"]["profile_id"] == "veyon"
    del raw["requisitos"]["r0002"]
    with pytest.raises(ValidationError):
        schema.model_validate(raw)
    raw = wire_response(match_analyzer(ids=["r0001", "r0002"]).analyze.return_value.raw_json)
    raw["requisitos"]["r0001"]["member_gaps"]["veyon"]["profile_id"] = "dinaut"
    with pytest.raises(ValidationError):
        schema.model_validate(raw)


def test_dynamic_schema_sdk_serialization_requires_ten_rows_and_two_uuid_members():
    from google.genai import _transformers
    ids = [f"r{index:04d}" for index in range(1, 11)]
    members = ["bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"]
    schema = joint_response_schema(ids, members)
    serialized = _transformers.t_schema(SimpleNamespace(vertexai=False), schema).model_dump(exclude_none=True)
    requirements = serialized["properties"]["requisitos"]
    assert set(requirements["required"]) == set(ids)
    for rid in ids:
        row = requirements["properties"][rid]
        assert row["properties"]["requirement_id"]["enum"] == [rid]
        gaps = row["properties"]["member_gaps"]
        assert set(gaps["required"]) == set(members)
        for pid in members:
            assert gaps["properties"][pid]["properties"]["profile_id"]["enum"] == [pid]
    assert "$ref" not in json.dumps(serialized) and "additionalProperties" not in json.dumps(serialized)


def test_wire_normalization_preserves_raw_and_missing_evaluations_without_filling():
    raw = wire_response(match_analyzer(ids=["r0001", "r0003"]).analyze.return_value.raw_json)
    before = copy.deepcopy(raw)
    normalized = normalize_joint_response(raw)
    validated = JointMatchAnalysis.model_validate(normalized)
    assert [item.requirement_id for item in validated.requisitos] == ["r0001", "r0003"]
    assert raw == before
    document = extraction()
    document["facets"].extend([copy.deepcopy(document["facets"][0]) for _ in range(2)])
    report = diagnose_joint_coverage(document, profile=joint_profile(), evaluated=normalized["requisitos"], wire_response=raw)
    assert not report["coverage_valid"] and report["missing_requirement_ids"] == ["r0002"]
    assert report["wire_errors"][0]["reason"] == "wire_requirement_keys_mismatch"


def test_wire_keys_and_ids_must_agree_even_when_array_ids_cover_inputs():
    raw = wire_response(match_analyzer().analyze.return_value.raw_json)
    gaps = raw["requisitos"]["r0001"]["member_gaps"]
    gaps["veyon"], gaps["dinaut"] = gaps["dinaut"], gaps["veyon"]
    normalized = normalize_joint_response(raw)
    report = diagnose_joint_coverage(extraction(), profile=joint_profile(), evaluated=normalized["requisitos"], wire_response=raw)
    assert not report["coverage_valid"] and len(report["wire_errors"]) == 2
    assert not report["member_errors"]


def test_v3_compact_sdk_schema_has_exact_lengths_and_shared_id_enums():
    from google.genai import _transformers
    ids = [f"r{index:04d}" for index in range(1, 14)]
    members = ["bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"]
    schema = joint_response_schema_v3(ids, members)
    serialized = _transformers.t_schema(SimpleNamespace(vertexai=False), schema).model_dump(exclude_none=True)
    rows = serialized["properties"]["requisitos"]
    assert int(rows["min_items"]) == int(rows["max_items"]) == 13
    assert set(rows["items"]["properties"]["requirement_id"]["enum"]) == set(ids)
    gaps = rows["items"]["properties"]["member_gaps"]
    assert int(gaps["min_items"]) == int(gaps["max_items"]) == 2
    assert set(gaps["items"]["properties"]["profile_id"]["enum"]) == set(members)
    legacy = _transformers.t_schema(SimpleNamespace(vertexai=False), joint_response_schema(ids, members)).model_dump(exclude_none=True)
    assert len(json.dumps(serialized)) < len(json.dumps(legacy)) / 5
    assert "$ref" not in json.dumps(serialized) and "additionalProperties" not in json.dumps(serialized)


def test_v3_schema_checks_lengths_enums_but_application_still_rejects_duplicate_ids():
    schema = joint_response_schema_v3(["r0001", "r0002"], ["veyon", "dinaut"])
    raw = copy.deepcopy(match_analyzer(ids=["r0001", "r0002"]).analyze.return_value.raw_json)
    schema.model_validate(raw)
    raw["requisitos"].pop()
    with pytest.raises(ValidationError):
        schema.model_validate(raw)
    raw = copy.deepcopy(match_analyzer(ids=["r0001", "r0001"]).analyze.return_value.raw_json)
    schema.model_validate(raw)
    document = extraction()
    document["facets"].append(copy.deepcopy(document["facets"][0]))
    report = diagnose_joint_coverage(document, profile=joint_profile(), evaluated=raw["requisitos"])
    assert not report["coverage_valid"] and report["duplicate_requirement_ids"] == ["r0001"]
    raw["requisitos"][0]["member_gaps"][0]["profile_id"] = "other-member"
    with pytest.raises(ValidationError):
        schema.model_validate(raw)


def test_v3_reservation_contains_schema_hash_and_no_requirements_still_skips_model():
    result = analyze_profile(settings(), extraction(), profile=joint_profile(), opportunity={}, analyzer=match_analyzer())
    expected = joint_response_schema_hash(["r0001"], ["dinaut", "veyon"])
    assert result["response_schema_hash"] == expected
    assert match_for_persistence(result)["breakdown_json"]["meta"]["response_schema_hash"] == expected
    assert joint_response_schema_hash(["r0001", "r0002"], ["dinaut", "veyon"]) != expected
    document = extraction()
    document["facets"] = []
    analyzer = match_analyzer()
    result = analyze_profile(settings(), document, profile=joint_profile(), opportunity={}, analyzer=analyzer)
    assert result["reason"] == "no_requirements"
    analyzer.analyze.assert_not_called()


def administrative_extraction(*, missing_substantive=False):
    document = extraction()
    if missing_substantive:
        document["facets"].append(copy.deepcopy(document["facets"][0]))
    document["facets"].append({"facet": "proposal_document", "label": "Anexo de declaración", "required": True,
                                "details": {}, "evidence": []})
    return document


def v1_source(document, *, ids=None, refs=None, state="cumple"):
    result = analyze_profile(settings(), document, profile=joint_profile(), opportunity={},
        analyzer=match_analyzer(ids=ids, refs=refs, state=state))
    requirements = [{"requirement_id": f"r{i:04d}", "tipo": f["facet"], "requisito": f["label"],
                     "obligatorio": f["required"], "detalle": f["details"], "evidencia_documental": f.get("evidence", [])}
                    for i,f in enumerate((f for f in document["facets"] if f["facet"]!="penalty_condition"),1)]
    prompt_hash = canonical_hash(read_analysis_prompt("prod2_convenio_v1.txt"))
    paid_hash = canonical_hash({"profile":prepare_joint_profile(joint_profile()),"extraction":document["cache_key"],
        "original_sha256":document["original_sha256"],"summary":document["summary_json"],"requirements":requirements,
        "opportunity":{},"prompt_hash":prompt_hash,"policy":"prod2_joint_evidence_v1",
        "evidence_policy":EVIDENCE_POLICY_VERSION,"model":settings().gemini_model,"fallback_model":settings().gemini_fallback_model})
    result.update(prompt_version="prod2_convenio_v1",prompt_hash=prompt_hash,policy_version="prod2_joint_evidence_v1",
                  cache_key="prod2:llm2:"+paid_hash,extraction_id="source-extraction")
    if state=="no_cumple":
        for row in result["raw_analysis"]["requisitos"]:
            for member in row["member_gaps"]:
                member["estado"]="no_cumple"
    return result


def test_v4_assembly_only_adds_explicit_pending_admin_rules_and_preserves_paid_response():
    document = administrative_extraction()
    source = v1_source(document)
    before = copy.deepcopy(source)
    result = assemble_cached_joint(settings(),source,document,joint_profile(),{},source_paid_evaluation_id="paid-evaluation")
    assert result["status"]=="analyzed" and result["coverage_valid"]
    assert not result["model_coverage_valid"] and result["rule_filled_ids"]==["r0002"]
    rule=result["requisitos"][1]
    assert rule["evaluacion_metodo"]=="rules_pending_document_v1" and rule["estado"]=="requiere_revision"
    assert rule["obligatorio"] is True and rule["critico"] is False
    assert all(m["estado"]=="requiere_revision" and m["evidence_refs"]==[] for m in rule["member_gaps"])
    assert result["input_tokens"]==result["output_tokens"]==result["cost_usd"]==0
    assert result["source_usage"]["cost_usd"]==source["cost_usd"]
    assert result["verdict"]=="gris" and not result["joint_qualification_verified"]
    assert source==before and result["raw_analysis"]==source["raw_analysis"]
    assert result["cache_key"].split(":")[-1]==assembly_request_hash(settings(),source,document,joint_profile(),{})
    meta=match_for_persistence(result)["breakdown_json"]["meta"]
    assert meta["policy_version"]==ASSEMBLY_POLICY_VERSION and meta["rule_filled_ids"]==["r0002"]
    assert meta["source_paid_evaluation_id"]=="paid-evaluation" and meta["source_extraction_id"]=="source-extraction"
    assert meta["source_usage"]["input_tokens"]==source["input_tokens"]


def test_v4_assembly_does_not_fill_when_substantive_or_attribution_is_missing():
    document = administrative_extraction(missing_substantive=True)
    source=v1_source(document)
    result=assemble_cached_joint(settings(),source,document,joint_profile(),{})
    assert result["status"]=="review_required" and not result["coverage_valid"]
    assert result["rule_filled_ids"]==[]
    document=administrative_extraction()
    source=v1_source(document,refs={"veyon":["dinaut:experience_json:0001"]})
    result=assemble_cached_joint(settings(),source,document,joint_profile(),{})
    assert result["status"]=="review_required" and result["rule_filled_ids"]==[]


def test_v4_assembly_rejects_changed_snapshot_and_binds_source_response_hash():
    document=administrative_extraction()
    source=v1_source(document)
    first=assembly_request_hash(settings(),source,document,joint_profile(),{})
    changed=copy.deepcopy(source)
    changed["raw_analysis"]["resumen"]="Changed paid response"
    assert assembly_request_hash(settings(),changed,document,joint_profile(),{})!=first
    profile=joint_profile();profile["members"][0]["profile"]["experience_json"][0]["amount"]=900
    with pytest.raises(ValueError,match="snapshot mismatch"):
        assemble_cached_joint(settings(),source,document,profile,{})
    document["extraction_id"]="other-extraction"
    with pytest.raises(ValueError,match="identity mismatch"):
        assemble_cached_joint(settings(),source,document,joint_profile(),{})


def test_v4_assembly_keeps_red_critical_incompatibility_and_zero_cost():
    document=administrative_extraction()
    source=v1_source(document,state="no_cumple",refs={"veyon":["veyon:experience_json:0001"],"dinaut":["dinaut:experience_json:0001"]})
    result=assemble_cached_joint(settings(),source,document,joint_profile(),{})
    assert result["coverage_valid"] and result["verdict"]=="rojo" and result["score"]==29
    assert result["requisitos"][0]["estado"]=="no_cumple" and result["cost_usd"]==0


def test_v4_substantive_read_keeps_original_ids_and_assembles_admin_without_second_call():
    document=extraction()
    document["facets"].insert(0,{"facet":"rnp","label":"RNP vigente","required":True,"details":{},"evidence":[]})
    document["facets"].append({"facet":"proposal_document","label":"Anexo","required":False,"details":{},"evidence":[]})
    analyzer=match_analyzer(ids=["r0002"])
    analyzer.analyze.return_value.prompt_version=SUBSTANTIVE_PROMPT_VERSION
    source=analyze_substantive_profile(settings(),document,profile=joint_profile(),opportunity={},analyzer=analyzer)
    assert source["coverage_valid"] and source["coverage_scope"]=="substantive_only"
    assert [r["requirement_id"] for r in analyzer.analyze.call_args.kwargs["requisitos"]]==["r0002"]
    assert not source["joint_qualification_verified"]
    assert source["cache_key"].split(":")[-1]==substantive_match_request_hash(settings(),document,profile=joint_profile(),opportunity={})
    result=assemble_cached_joint(settings(),source,document,joint_profile(),{})
    assert result["coverage_valid"] and result["rule_filled_ids"]==["r0001","r0003"]
    assert result["requisitos"][2]["obligatorio"] is False and analyzer.analyze.call_count==1


def test_v4_primary_analyzer_uses_original_static_schema_and_never_falls_back():
    analyzer=object.__new__(SubstantiveJointMatchAnalyzer)
    analyzer.settings=settings();analyzer.prompt="test-only";analyzer.prompt_version=SUBSTANTIVE_PROMPT_VERSION
    analyzer.client=MagicMock()
    raw=match_analyzer().analyze.return_value.raw_json
    analyzer.client.models.generate_content.return_value=SimpleNamespace(text=json.dumps(raw),candidates=[],usage_metadata=None)
    result=analyzer.analyze(perfil={},oportunidad={},requisitos=[])
    assert result.analysis.requisitos[0].requirement_id=="r0001"
    assert analyzer.client.models.generate_content.call_args.kwargs["config"].response_schema is JointMatchAnalysis
    analyzer.client.models.generate_content.reset_mock()
    analyzer.client.models.generate_content.side_effect=RuntimeError("test provider failure")
    with pytest.raises(RuntimeError):
        analyzer.analyze(perfil={},oportunidad={},requisitos=[])
    assert analyzer.client.models.generate_content.call_count==1


def test_v4_absent_evidence_changes_no_cumple_to_review_without_rewriting_raw():
    document=administrative_extraction()
    source=v1_source(document,state="no_cumple")
    result=assemble_cached_joint(settings(),source,document,joint_profile(),{})
    assert result["coverage_valid"] and result["verdict"]=="gris"
    assert result["requisitos"][0]["estado"]=="requiere_revision"
    assert all(member["estado"]=="requiere_revision" for member in result["requisitos"][0]["member_gaps"])
    assert result["raw_analysis"]["requisitos"][0]["estado"]=="no_cumple"
    meta=match_for_persistence(result)["breakdown_json"]["meta"]
    assert meta["evidence_absence_guard_version"]=="evidence_absence_guard_v1"
    assert len(meta["evidence_absence_guards"])==3
    assert {c["model_state"] for c in meta["evidence_absence_guards"]}=={"no_cumple"}


def test_v4_substantive_absence_guard_also_preserves_proposed_model_states():
    analyzer=match_analyzer(state="no_cumple")
    analyzer.analyze.return_value.prompt_version=SUBSTANTIVE_PROMPT_VERSION
    for member in analyzer.analyze.return_value.analysis.requisitos[0].member_gaps:
        member.estado="no_cumple"
    for member in analyzer.analyze.return_value.raw_json["requisitos"][0]["member_gaps"]:
        member["estado"]="no_cumple"
    result=analyze_substantive_profile(settings(),extraction(),profile=joint_profile(),opportunity={},analyzer=analyzer)
    assert result["requisitos"][0]["estado"]=="requiere_revision"
    assert result["raw_analysis"]["requisitos"][0]["estado"]=="no_cumple"
    assert result["coverage_valid"] and len(result["evidence_absence_guards"])==3


def test_v5_substantive_prompt_is_manual_opt_in_and_v4_request_fingerprint_keeps_its_legacy_formula():
    document, profile, opportunity = extraction(), joint_profile(), {"codigo": "CP-1"}
    prepared, full, substantive, prompt_hash, schema_hash, default_hash = _substantive_input(settings(), document, profile, opportunity)
    original_formula = canonical_hash({"profile": prepared, "extraction": document["cache_key"],
        "original_sha256": document["original_sha256"], "summary": document["summary_json"],
        "requirements": substantive, "full_requirements_hash": canonical_hash(full), "opportunity": opportunity,
        "prompt_hash": canonical_hash(read_analysis_prompt("prod2_convenio_v4.txt")), "response_schema_hash": schema_hash,
        "transport": "response_schema_static_v1", "policy": SUBSTANTIVE_POLICY_VERSION,
        "evidence_policy": EVIDENCE_POLICY_VERSION, "model": settings().gemini_model, "automatic_fallback": False})
    assert SUBSTANTIVE_PROMPT_VERSION == "prod2_convenio_v4" and default_hash == original_formula
    v4_explicit = substantive_match_request_hash(settings(), document, profile=profile, opportunity=opportunity, prompt_version=SUBSTANTIVE_PROMPT_VERSION)
    assert v4_explicit == default_hash
    v5_explicit = substantive_match_request_hash(settings(), document, profile=profile, opportunity=opportunity, prompt_version=CONCRETE_SUBSTANTIVE_PROMPT_VERSION)
    assert v5_explicit != default_hash
    with pytest.raises(ValueError, match="Unknown substantive prompt"):
        substantive_match_request_hash(settings(), document, profile=profile, opportunity=opportunity, prompt_version="unknown")


def test_v5_manual_transport_uses_its_prompt_without_changing_v4_default_or_creating_a_provider_request():
    configured = SimpleNamespace(gemini_api_key="unused", gemini_model=settings().gemini_model)
    client = MagicMock()
    with patch("buenapro_worker.prod2.analysis.genai.Client", return_value=client):
        old = SubstantiveJointMatchAnalyzer(configured)
        future = SubstantiveJointMatchAnalyzer(configured, prompt_version=CONCRETE_SUBSTANTIVE_PROMPT_VERSION)
    assert old.prompt == read_analysis_prompt("prod2_convenio_v4.txt")
    assert future.prompt == read_analysis_prompt("prod2_convenio_v5.txt")
    assert old.prompt_version == SUBSTANTIVE_PROMPT_VERSION
    assert future.prompt_version == CONCRETE_SUBSTANTIVE_PROMPT_VERSION
    client.models.generate_content.assert_not_called()


def test_v5_mocked_manual_response_and_assembly_preserve_paid_cache_provenance_with_no_second_call():
    document = extraction()
    analyzer = match_analyzer()
    analyzer.analyze.return_value.prompt_version = CONCRETE_SUBSTANTIVE_PROMPT_VERSION
    before = copy.deepcopy(analyzer.analyze.return_value.raw_json)
    cache = {}
    kwargs = {"profile": joint_profile(), "opportunity": {}, "analyzer": analyzer, "cache": cache,
              "prompt_version": CONCRETE_SUBSTANTIVE_PROMPT_VERSION}
    source = analyze_substantive_profile(settings(), document, **kwargs)
    assert source["policy_version"] == "prod2_substantive_concrete_evidence_v5"
    assert source["prompt_hash"] == canonical_hash(read_analysis_prompt("prod2_convenio_v5.txt"))
    assert source["raw_analysis"] == before
    again = analyze_substantive_profile(settings(), document, **kwargs)
    assert again["cache_hit"] and analyzer.analyze.call_count == 1
    assembled = assemble_cached_joint(settings(), source, document, joint_profile(), {})
    assert assembled["source_prompt_version"] == CONCRETE_SUBSTANTIVE_PROMPT_VERSION
    assert assembled["raw_analysis"] == before
    assert assembled["cost_usd"] == 0 and assembled["input_tokens"] == 0
    assert analyzer.analyze.call_count == 1
