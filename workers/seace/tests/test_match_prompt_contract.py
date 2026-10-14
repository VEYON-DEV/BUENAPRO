"""Offline guardrails for the shared, evidence-bound LLM2 evaluation contract.

These assertions protect instructions, not model accuracy. They deliberately do
not call a provider or infer correctness from a model-generated score.
"""

from pathlib import Path
import json
import re
import unicodedata
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
MATCH_PROMPTS = ("match_analysis_v2.txt", "match_analysis_v5.txt", "match_analysis_prod4_v1.txt")


def normalized(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(char)
    )


@pytest.fixture(params=MATCH_PROMPTS)
def prompt(request: pytest.FixtureRequest) -> str:
    return normalized((PROMPTS / request.param).read_text(encoding="utf-8"))


def test_prompt_is_universal_without_few_shot_or_company_fixtures(prompt: str) -> None:
    for fixture in ("veyon", "dinaut", "din automatizacion"):
        assert fixture not in prompt
    assert not re.search(r"\bej\.|por ejemplo|ejemplo\s*:", prompt)


def test_prior_score_cannot_anchor_a_new_evaluation(prompt: str) -> None:
    for instruction in (
        "su score es tu ancla",
        "devuelve exactamente el mismo veredicto y score",
        "mueve el score desde el previo",
        "manten estabilidad respecto a analisis_previo",
    ):
        assert instruction not in prompt


def test_distinguishes_declaration_missing_evidence_and_incompatibility(prompt: str) -> None:
    assert "declar" in prompt
    assert "evidencia" in prompt
    assert "demo" in prompt
    assert "requiere_revision" in prompt
    assert "no_cumple" in prompt
    assert "cumple_con_accion" in prompt
    assert "contratabl" in prompt
    assert "metadatos" in prompt or "nombres de adjuntos" in prompt


def test_experience_is_contextual_and_not_added_twice(prompt: str) -> None:
    for criterion in ("especialidad", "moneda", "periodo", "participacion", "respaldo"):
        assert criterion in prompt
    assert "no sum" in prompt
    assert "conviert" in prompt or "convers" in prompt


def test_rnp_declaration_does_not_prove_vigency(prompt: str) -> None:
    assert "rnp" in prompt
    assert "vigencia" in prompt
    assert "declaracion" in prompt
    assert "acredit" in prompt


def test_untrusted_input_instructions_are_not_followed(prompt: str) -> None:
    assert "instrucciones" in prompt
    assert "datos" in prompt
    assert "ignora" in prompt or "no obedezcas" in prompt


def test_complete_requirement_evaluation_and_explicit_score_bands(prompt: str) -> None:
    assert "cada requisito" in prompt or "todos los requisitos" in prompt
    text = prompt.replace("–", "-")
    if "separado del veredicto" in prompt:
        assert all(band in text for band in ("0-29", "30-80", "81-100", "0-100"))
        assert "colores de presentacion" in prompt and "no" in prompt
        return
    for verdict, band in (("verde", "85-100"), ("ambar", "50-84"),
                          ("gris", "0-100" if "capacidad sustantiva" in prompt else "30-49"), ("rojo", "0-29")):
        assert verdict in prompt
        assert band in text
    assert "puntaje" in prompt or "score" in prompt


def test_output_actions_are_grounded_in_participation_conditions(prompt: str) -> None:
    for condition in ("permitted", "prohibited", "conditional", "not_identified"):
        assert condition in prompt
    assert "consorcio" in prompt
    assert "subcontratacion" in prompt


@pytest.mark.parametrize("filename", MATCH_PROMPTS)
def test_fake_model_receives_complete_current_data_without_previous_score(filename: str) -> None:
    from buenapro_worker.matching.analyzer import MatchAnalyzer
    from buenapro_worker.settings import Settings

    profile = {
        "identity_json": {"company_summary": "Actividad comercial declarada"},
        "econ_experience_json": {"areas": [{"rubro": "Area declarada", "monto": 10, "moneda": "PEN"}]},
        "team_json": [{"role": "Rol declarado", "certifications": []}],
    }
    opportunity = {"id": 123, "resumen": {"participation": {"consorcio": {"status": "not_identified"}}}}
    requirements = [{"id": "req-1", "facet": "specific_experience", "required": True}]
    fake_response = SimpleNamespace(
        text=json.dumps({"veredicto": "gris", "score": 40, "resumen": "Falta verificar respaldo",
                         "requisitos": [], "acciones_recomendadas": []}),
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=20),
        candidates=[],
    )
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    with patch("buenapro_worker.matching.analyzer.genai.Client", return_value=fake_client):
        analyzer = MatchAnalyzer(settings, prompt_filename=filename, prompt_version="test-universal")
        result = analyzer.analyze(perfil=profile, oportunidad=opportunity, requisitos=requirements,
                                  analisis_previo={"score": 99, "veredicto": "verde"})

    call = fake_client.models.generate_content.call_args.kwargs
    assert json.loads(call["contents"][0]) == {
        "perfil": profile, "oportunidad": opportunity, "requisitos": requirements,
    }
    assert call["config"].temperature == 0
    assert call["config"].response_mime_type == "application/json"
    assert result.prompt_version == "test-universal"
    fake_client.models.generate_content.assert_called_once()
