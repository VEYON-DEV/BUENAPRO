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
from buenapro_worker.matching.schemas import SubstantiveMatchAnalysis
from buenapro_worker.matching.compact_context import build_compact_match_profile, compact_json
from buenapro_worker.matching.substantive_policy import MATCH_POLICY_VERSION
from buenapro_worker.settings import Settings


ANALYSIS_PROMPT_VERSION = MATCH_POLICY_VERSION
ANALYSIS_PROMPT_FILENAME = "match_analysis_v5.txt"
SUBSTANTIVE_PROMPT_FILES = {
    "match_analysis_v4_substantive": "match_analysis_v2.txt",
    MATCH_POLICY_VERSION: ANALYSIS_PROMPT_FILENAME,
}


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
        prompt_filename: str | None = None,
        prompt_version: str = ANALYSIS_PROMPT_VERSION,
    ) -> None:
        self.settings = settings
        self.client = genai.Client(api_key=settings.gemini_api_key,
                                  http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)))
        # An explicitly recovered v4 request retains its text, compact payload
        # and substantive schema even after the default moves to v5.
        selected_prompt = prompt_filename or SUBSTANTIVE_PROMPT_FILES.get(prompt_version, ANALYSIS_PROMPT_FILENAME)
        self.prompt = read_analysis_prompt(selected_prompt)
        self.prompt_version = prompt_version

    def analyze(
        self,
        *,
        perfil: dict[str, Any],
        oportunidad: dict[str, Any],
        requisitos: list[dict[str, Any]],
        analisis_previo: dict[str, Any] | None = None,
    ) -> MatchAnalysisResult:
        current_profile = build_compact_match_profile(perfil) if self.prompt_version in SUBSTANTIVE_PROMPT_FILES else perfil
        body: dict[str, Any] = {"perfil": current_profile, "oportunidad": oportunidad, "requisitos": requisitos}
        # Previous scores are not evidence and must not anchor fresh evaluations.
        payload = compact_json(body)
        # A sent request with no valid response has uncertain cost. Never create
        # an additional paid call merely to hide an error or malformed response.
        return self._analyze_with_model(self.settings.gemini_model, payload)

    def _analyze_with_model(self, model: str, payload: str) -> MatchAnalysisResult:
        response = self.client.models.generate_content(
            model=model,
            contents=[payload],
            config=types.GenerateContentConfig(
                system_instruction=self.prompt,
                response_mime_type="application/json",
                response_schema=SubstantiveMatchAnalysis if self.prompt_version in SUBSTANTIVE_PROMPT_FILES else MatchAnalysisV1,
                temperature=0,
                max_output_tokens=8192,
            ),
        )
        if is_truncated(response):
            raise TruncatedResponseError("Match analysis truncated by max output tokens")
        raw_json = parse_json_response(response.text or "{}")
        schema = SubstantiveMatchAnalysis if self.prompt_version in SUBSTANTIVE_PROMPT_FILES else MatchAnalysisV1
        analysis = schema.model_validate(raw_json)
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
