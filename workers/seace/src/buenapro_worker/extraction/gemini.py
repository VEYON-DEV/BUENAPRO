from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from buenapro_worker.extraction.schemas import EettExtractionV1, TdrExtractionV2
from buenapro_worker.settings import Settings


PROMPT_VERSION = "tdr_extraction_v2"
SCHEMA_VERSION = "tdr_extraction_schema_v2"
PROMPT_FILENAME = "tdr_extraction_v2.txt"
EETT_PROMPT_VERSION = "eett_extraction_v1"
EETT_SCHEMA_VERSION = "eett_extraction_schema_v1"
EETT_PROMPT_FILENAME = "eett_extraction_v1.txt"
BASES_PROMPT_VERSION = "bases_extraction_v1"
BASES_SCHEMA_VERSION = "bases_extraction_schema_v1"
BASES_PROMPT_FILENAME = "bases_extraction_v1.txt"


def prompt_version_for_doc_class(doc_class: str | None) -> str:
    if doc_class in {"bases_good", "bases_service"}:
        return BASES_PROMPT_VERSION
    return EETT_PROMPT_VERSION if doc_class == "eett" else PROMPT_VERSION

MODEL_PRICES_USD_PER_MILLION = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
}


@dataclass(frozen=True)
class ExtractionResult:
    extraction: TdrExtractionV2 | EettExtractionV1
    raw_json: dict[str, Any]
    model: str
    prompt_version: str
    schema_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    requires_human_review: bool = False

    @property
    def summary_json(self) -> dict[str, Any]:
        return self.extraction.summary.model_dump()


class GeminiExtractor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.prompts = {
            "tdr": read_prompt(PROMPT_FILENAME),
            "eett": read_prompt(EETT_PROMPT_FILENAME),
            "bases_good": read_prompt(BASES_PROMPT_FILENAME),
            "bases_service": read_prompt(BASES_PROMPT_FILENAME),
        }

    def count_tokens(self, pdf_bytes: bytes, *, mime: str = "application/pdf") -> int:
        response = self.client.models.count_tokens(
            model=self.settings.gemini_model,
            contents=self._contents(pdf_bytes, mime=mime),
        )
        return int(response.total_tokens or 0)

    def extract(
        self, pdf_bytes: bytes, *, mime: str = "application/pdf", doc_class: str = "tdr"
    ) -> ExtractionResult:
        if doc_class not in self.prompts:
            raise ValueError(f"Unsupported document class for extraction: {doc_class}")
        try:
            return self._extract_with_model(
                self.settings.gemini_model,
                pdf_bytes,
                mime=mime,
                doc_class=doc_class,
                max_output_tokens=16384,
            )
        except TruncatedResponseError:
            try:
                return self._extract_with_model(
                    self.settings.gemini_model,
                    pdf_bytes,
                    mime=mime,
                    doc_class=doc_class,
                    max_output_tokens=24576,
                    requires_human_review=True,
                )
            except Exception:
                return self._extract_with_model(
                    self.settings.gemini_fallback_model,
                    pdf_bytes,
                    mime=mime,
                    doc_class=doc_class,
                    max_output_tokens=24576,
                    requires_human_review=True,
                )
        except Exception:
            return self._extract_with_model(
                self.settings.gemini_fallback_model,
                pdf_bytes,
                mime=mime,
                doc_class=doc_class,
                max_output_tokens=24576,
                requires_human_review=True,
            )

    def _contents(self, pdf_bytes: bytes, *, mime: str) -> list[Any]:
        return [
            types.Part.from_bytes(data=pdf_bytes, mime_type=mime),
        ]

    def _extract_with_model(
        self,
        model: str,
        pdf_bytes: bytes,
        *,
        mime: str,
        doc_class: str,
        max_output_tokens: int,
        requires_human_review: bool = False,
    ) -> ExtractionResult:
        schema = EettExtractionV1 if doc_class in {"eett", "bases_good"} else TdrExtractionV2
        response = self.client.models.generate_content(
            model=model,
            contents=self._contents(pdf_bytes, mime=mime),
            config=types.GenerateContentConfig(
                system_instruction=self.prompts[doc_class],
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0,
                max_output_tokens=max_output_tokens,
            ),
        )
        if is_truncated(response):
            raise TruncatedResponseError("Gemini response was truncated by max output tokens")
        raw_json = parse_json_response(response.text or "{}")
        if not raw_json or not any(raw_json.values()):
            # PDF probablemente escaneado/ilegible: forzar el fallback (flash),
            # que lee mejor documentos dificiles, y marcar para revision humana.
            raise EmptyExtractionError("Gemini returned an empty extraction")
        extraction = schema.model_validate(raw_json)
        if isinstance(extraction, EettExtractionV1) and not extraction.goods.items:
            requires_human_review = True
        usage = response.usage_metadata
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        return ExtractionResult(
            extraction=extraction,
            raw_json=raw_json,
            model=model,
            prompt_version=prompt_version_for_doc_class(doc_class),
            schema_version=(
                BASES_SCHEMA_VERSION if doc_class in {"bases_good", "bases_service"}
                else EETT_SCHEMA_VERSION if doc_class == "eett" else SCHEMA_VERSION
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost(model, input_tokens, output_tokens),
            requires_human_review=requires_human_review,
        )


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("Extractor returned non-object JSON")
    return payload


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = MODEL_PRICES_USD_PER_MILLION.get(model, MODEL_PRICES_USD_PER_MILLION["gemini-3.1-flash-lite"])
    return (input_tokens / 1_000_000 * price["input"]) + (
        output_tokens / 1_000_000 * price["output"]
    )


class TruncatedResponseError(RuntimeError):
    pass


class EmptyExtractionError(RuntimeError):
    pass


def read_prompt(filename: str = PROMPT_FILENAME) -> str:
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


def is_truncated(response: Any) -> bool:
    for candidate in getattr(response, "candidates", []) or []:
        finish_reason = str(getattr(candidate, "finish_reason", "") or "")
        if "MAX_TOKENS" in finish_reason:
            return True
    return False
