"""Offline transport/contract checks grounded in two preserved bad responses.

These tests verify what future calls receive, not that a model will obey it.
Old paid responses remain audit evidence; this does not regenerate them.
"""
import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.matching.analyzer import ANALYSIS_PROMPT_FILENAME, ANALYSIS_PROMPT_VERSION, MatchAnalyzer, read_analysis_prompt
from buenapro_worker.matching.substantive_policy import MATCH_POLICY_VERSION
from buenapro_worker.matching.compact_context import build_compact_match_profile, compact_json
from buenapro_worker.matching.schemas import SubstantiveMatchAnalysis
from buenapro_worker.settings import Settings

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/llm2_concrete_evidence_cases.json").read_text())
CASES = FIXTURE["cases"]


def test_effective_individual_defaults_are_versioned_and_web_reads_the_same_new_file():
    assert ANALYSIS_PROMPT_FILENAME == "match_analysis_v5.txt"
    assert ANALYSIS_PROMPT_VERSION == MATCH_POLICY_VERSION == "match_analysis_v5_concrete_evidence"
    web = (ROOT / "apps/web/server/services/matchAnalysis.ts").read_text()
    policy = (ROOT / "apps/web/server/services/matchAnalysisPolicy.ts").read_text()
    assert "match_analysis_v5.txt" in web and "match_analysis_v2.txt" not in web
    assert MATCH_POLICY_VERSION in policy
    assert read_analysis_prompt() == read_analysis_prompt("match_analysis_v5.txt")
    assert read_analysis_prompt() != read_analysis_prompt("match_analysis_v2.txt")
    assert hashlib.sha256(read_analysis_prompt("match_analysis_v2.txt").encode()).hexdigest() == "283c18dcac0abd112aa92e1ab49e0711411d31d7f6c5adddba27f3d258e75c47"
    assert hashlib.sha256(read_analysis_prompt("prod2_convenio_v4.txt").encode()).hexdigest() == "2b04f5e3d37e31cd73f8c6907a5bb8cb34e69de49e2269485f886199f6df5623"


def test_explicit_v4_recovery_keeps_its_prompt_compact_payload_and_substantive_schema_while_default_is_v5():
    profile = {"experience_json": [{"numero_contrato": "OS-PRESERVED", "objeto": "Mantenimiento de licencias",
        "documentary_summary": {"status": "pending_human_confirmation", "original_fields": {"audit": "AUDIT_SENTINEL"},
            "sources": [{"document_id": "existing-document", "extraction": {"summary": "Mantenimiento de licencias",
                "facts": [{"quote": "MANTENIMIENTO DE LICENCIA SOFTWARE", "page": 1}],
                "amounts": [{"amount_decimal": "42315.00", "currency": "USD", "quote": "Sub.Total 42315.00", "page": 1}],
                "full_text": "FULL_TEXT_SENTINEL"}}]}}]}
    requirements = [{"source_index": 4, "tipo": "specific_experience", "obligatorio": True}]
    opportunity = {"codigo": "PRESERVED"}
    original = copy.deepcopy(profile)
    response = SimpleNamespace(candidates=[], usage_metadata=None, text=json.dumps({"veredicto": "gris", "score": 70,
        "resumen": "Transporte sintético, no evaluación nueva.", "requisitos": [], "acciones_recomendadas": []}))
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    old_client, new_client = MagicMock(), MagicMock()
    old_client.models.generate_content.return_value = response
    new_client.models.generate_content.return_value = response
    with patch("buenapro_worker.matching.analyzer.genai.Client", side_effect=[old_client, new_client]):
        recovered = MatchAnalyzer(settings, prompt_version="match_analysis_v4_substantive")
        future = MatchAnalyzer(settings)
        old_result = recovered.analyze(perfil=profile, oportunidad=opportunity, requisitos=requirements)
        future.analyze(perfil=profile, oportunidad=opportunity, requisitos=requirements)
    frozen_prompt = read_analysis_prompt("match_analysis_v2.txt")
    assert recovered.prompt == frozen_prompt and old_result.prompt_version == "match_analysis_v4_substantive"
    assert future.prompt == read_analysis_prompt("match_analysis_v5.txt")
    expected_payload = compact_json({"perfil": build_compact_match_profile(original), "oportunidad": opportunity, "requisitos": requirements})
    for client, prompt in [(old_client, frozen_prompt), (new_client, future.prompt)]:
        client.models.generate_content.assert_called_once()
        call = client.models.generate_content.call_args.kwargs
        assert call["config"].response_schema is SubstantiveMatchAnalysis
        assert call["config"].system_instruction == prompt
        assert call["contents"] == [expected_payload]
        assert "FULL_TEXT_SENTINEL" not in call["contents"][0] and "AUDIT_SENTINEL" not in call["contents"][0]
    assert profile == original
    assert hashlib.sha256(frozen_prompt.encode()).hexdigest() == "283c18dcac0abd112aa92e1ab49e0711411d31d7f6c5adddba27f3d258e75c47"


@pytest.mark.parametrize("filename", ["match_analysis_v5.txt", "prod2_convenio_v5.txt"])
def test_concrete_contract_requires_the_actual_clause_useful_citation_missing_activity_and_viable_action(filename):
    prompt = read_analysis_prompt(filename).lower()
    for term in ["cláusula", "alternativas", "contrato", "actividad", "cita/página", "no está acreditado",
                 "incompatibilidad", "revisar compatibilidad", "misma marca", "partner", "scada", "tanques",
                 "cableado", "créditos de ia", "sla", "propuesta", "no_cumple", "requiere_revision",
                 "factura y/o constancia y/o contrato", "computable", "moneda", "neto+bruto"]:
        assert term in prompt, (filename, term)
    assert "0–29" in prompt and "30–80" in prompt and "81–100" in prompt
    assert "0–100" in prompt and "colores de presentación" in prompt
    assert re.search(r"no impone (?:una )?nota baja", prompt)
    assert "no reducen puntaje" in prompt or "no agregarlos, puntuarlos" in prompt


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["id"]))
def test_mocked_future_individual_transport_passes_concrete_v5_and_preserves_source_ids_facts_and_alternatives(case):
    profile = {"experience_json": [{"numero_contrato": evidence["contract"], "objeto": evidence["activity"], "documentary_summary": {
        "status": "pending_human_confirmation", "sources": [{"document_id": evidence["document_id"], "status": "pending_human_confirmation",
        "extraction": {"summary": evidence["activity"], "classification": {"quote": evidence["quote"], "page": evidence["page"]},
        "facts": [{"field": "Objeto", "value": evidence["activity"], "quote": evidence["quote"], "page": evidence["page"]}], "amounts": []}}]}}
        for evidence in case["evidence"]]}
    before = copy.deepcopy(profile)
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(candidates=[], usage_metadata=None,
        text=json.dumps({"veredicto": "gris", "score": 70, "resumen": "Respuesta sintética de transporte, no análisis nuevo.", "requisitos": [], "acciones_recomendadas": []}))
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    with patch("buenapro_worker.matching.analyzer.genai.Client", return_value=client):
        analyzer = MatchAnalyzer(settings)
        analyzer.analyze(perfil=profile, oportunidad={}, requisitos=case["requirements"], analisis_previo={"score": case["recorded_score"]})
    assert client.models.generate_content.call_count == 1
    call = client.models.generate_content.call_args.kwargs
    assert call["config"].system_instruction == read_analysis_prompt("match_analysis_v5.txt")
    payload = json.loads(call["contents"][0])
    assert payload["requisitos"] == case["requirements"]
    assert "analisis_previo" not in payload
    sources = [source for experience in payload["perfil"]["experience_json"] for source in experience["documentary_summary"]["sources"]]
    assert [source["document_id"] for source in sources] == [evidence["document_id"] for evidence in case["evidence"]]
    assert [source["extraction"]["facts"][0]["quote"] for source in sources] == [evidence["quote"] for evidence in case["evidence"]]
    assert profile == before


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["id"]))
def test_golden_failures_bind_to_the_preserved_paid_receipts_when_local_evidence_exists(case):
    folder = ROOT / "tmp/dinaut-five-substantive-compact-20261013"
    receipt = folder / f"{case['id']}.response.private.json"
    if not receipt.exists():
        pytest.skip("Unique private paid receipts are not shipped to a clean checkout")
    before = receipt.read_bytes()
    assert hashlib.sha256(before).hexdigest() == case["receipt_sha256"]
    raw = json.loads(before)["response"]["parsed"]
    assert raw["score"] == case["recorded_score"]
    manifest = json.loads((folder / "manifest.private.json").read_text())
    frozen = next(row for row in manifest["cases"] if row["id"] == case["id"])
    assert [row["source_index"] for row in frozen["request"]["requisitos"]] == [row["source_index"] for row in case["requirements"]]
    if case["id"] == 101436:
        assert "partner de SparxSystems" in " ".join(raw["acciones_recomendadas"])
        assert frozen["request"]["requisitos"][0]["detalle"]["monto"] is None
        assert "partner" not in frozen["request"]["requisitos"][0]["detalle"]["evidencia"].lower()
    else:
        assert raw["requisitos"][0]["estado"] == "cumple" and raw["requisitos"][1]["estado"] == "no_cumple"
        assert raw["requisitos"][2]["estado"] == "no_cumple"
        assert "SLA" in frozen["request"]["requisitos"][2]["detalle"]["value"]
    assert receipt.read_bytes() == before
