from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from buenapro_worker.extraction.gemini import (
    TruncatedResponseError,
    estimate_cost,
    is_truncated,
    parse_json_response,
)
from buenapro_worker.matching.schemas import MatchAnalysisV1
from buenapro_worker.settings import Settings


ANALYSIS_PROMPT_VERSION = "match_analysis_v2"
ANALYSIS_PROMPT_FILENAME = "match_analysis_v2.txt"


@dataclass(frozen=True)
class MatchAnalysisResult:
    analysis: MatchAnalysisV1
    raw_json: dict[str, Any]
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class MatchAnalyzer:
    def __init__(
        self,
        settings: Settings,
        *,
        prompt_filename: str = ANALYSIS_PROMPT_FILENAME,
        prompt_version: str = ANALYSIS_PROMPT_VERSION,
    ) -> None:
        self.settings = settings
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.prompt = read_analysis_prompt(prompt_filename)
        self.prompt_version = prompt_version

    def analyze(
        self,
        *,
        perfil: dict[str, Any],
        oportunidad: dict[str, Any],
        requisitos: list[dict[str, Any]],
        analisis_previo: dict[str, Any] | None = None,
    ) -> MatchAnalysisResult:
        body: dict[str, Any] = {"perfil": perfil, "oportunidad": oportunidad, "requisitos": requisitos}
        if analisis_previo:
            body["analisis_previo"] = analisis_previo
        payload = json.dumps(body, ensure_ascii=False)
        try:
            return self._analyze_with_model(self.settings.gemini_model, payload)
        except Exception:
            return self._analyze_with_model(self.settings.gemini_fallback_model, payload)

    def _analyze_with_model(self, model: str, payload: str) -> MatchAnalysisResult:
        response = self.client.models.generate_content(
            model=model,
            contents=[payload],
            config=types.GenerateContentConfig(
                system_instruction=self.prompt,
                response_mime_type="application/json",
                response_schema=MatchAnalysisV1,
                temperature=0,
                max_output_tokens=8192,
            ),
        )
        if is_truncated(response):
            raise TruncatedResponseError("Match analysis truncated by max output tokens")
        raw_json = parse_json_response(response.text or "{}")
        analysis = MatchAnalysisV1.model_validate(raw_json)
        usage = response.usage_metadata
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        return MatchAnalysisResult(
            analysis=analysis,
            raw_json=raw_json,
            model=model,
            prompt_version=self.prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost(model, input_tokens, output_tokens),
        )


def read_analysis_prompt(filename: str = ANALYSIS_PROMPT_FILENAME) -> str:
    candidates = [
        Path(__file__).resolve().parents[3] / "prompts" / filename,
        Path.cwd() / "prompts" / filename,
        Path("/app/workers/seace/prompts") / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Prompt {filename} not found. Searched: {searched}")
