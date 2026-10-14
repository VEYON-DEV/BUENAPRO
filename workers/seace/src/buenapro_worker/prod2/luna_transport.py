"""Explicit GPT-6 Luna/low transport without retries, uploads, or receipt storage.

The caller owns reservations, private receipts and document/schema validation.
``on_response`` runs before HTTP/JSON checks so even an unusable paid response
can be committed before the caller decides whether processing may continue.
Official model/file/pricing contracts were checked on 2026-10-07.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


LUNA_MODEL = "gpt-6-luna"
RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_PDF_BYTES = 50_000_000
MAX_OUTPUT_TOKENS = 128_000
LONG_CONTEXT_INPUT_TOKENS = 272_000
LUNA_PRICES_USD_PER_MILLION = {"input": 0.10, "cached_input": 0.01, "output": 0.50}
PDF_DATA_PREFIX = "data:application/pdf;base64,"
REQUEST_TIMEOUT = httpx.Timeout(connect=10, read=300, write=60, pool=10)


class LunaTransportError(RuntimeError):
    """Safe failure metadata; never embeds credentials, URLs or provider bodies."""

    def __init__(self, code: str, *, status_code: int | None = None,
                 request_dispatched: bool = False, response_received: bool = False):
        self.code = code
        self.status_code = status_code
        self.request_dispatched = request_dispatched
        self.response_received = response_received
        super().__init__(f"Luna transport failed ({code})")


def _tokens(value: Any) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class LunaUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def cost_usd(self) -> float:
        # output_tokens already includes reasoning_tokens in Responses usage.
        return estimate_luna_cost(self.input_tokens, self.output_tokens, self.cached_input_tokens)


@dataclass(frozen=True)
class LunaParsedResponse:
    status: str
    text: str = field(repr=False)
    incomplete_reason: str | None = None
    refusal: str | None = field(default=None, repr=False)
    usage: LunaUsage | None = None

    @property
    def truncated(self) -> bool:
        return self.status == "incomplete" and self.incomplete_reason == "max_output_tokens"

    @property
    def completed(self) -> bool:
        return self.status == "completed" and self.refusal is None and bool(self.text.strip())

    @property
    def usage_known(self) -> bool:
        return self.usage is not None


def estimate_luna_cost(input_tokens: int, output_tokens: int,
                       cached_input_tokens: int = 0) -> float:
    """Estimate Standard cost; output is total billed output including reasoning.

    Above 272K input tokens, both input/cache rates double and the output rate
    is multiplied by 1.5 for the entire request, including the cached prefix.
    """
    if (not all(_tokens(value) for value in (input_tokens, output_tokens, cached_input_tokens))
            or cached_input_tokens > input_tokens):
        raise LunaTransportError("invalid_usage")
    long_context = input_tokens > LONG_CONTEXT_INPUT_TOKENS
    rates = LUNA_PRICES_USD_PER_MILLION
    input_cost = ((input_tokens - cached_input_tokens) * rates["input"]
                  + cached_input_tokens * rates["cached_input"]) * (2 if long_context else 1)
    output_cost = output_tokens * rates["output"] * (1.5 if long_context else 1)
    return (input_cost + output_cost) / 1_000_000


def parse_luna_usage(response: dict) -> LunaUsage | None:
    """Return validated usage or unknown; never turn absent/malformed counts into zero."""
    if not isinstance(response, dict) or not isinstance(usage := response.get("usage"), dict):
        return None
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if not _tokens(input_tokens) or not _tokens(output_tokens):
        return None
    input_details = usage.get("input_tokens_details", {})
    output_details = usage.get("output_tokens_details", {})
    if not isinstance(input_details, dict) or not isinstance(output_details, dict):
        return None
    # Missing cache details use the conservative full input rate.
    cached = input_details.get("cached_tokens", 0)
    reasoning = output_details.get("reasoning_tokens")
    total = usage.get("total_tokens")
    if (not _tokens(cached) or cached > input_tokens
            or reasoning is not None and (not _tokens(reasoning) or reasoning > output_tokens)
            or total is not None and (not _tokens(total) or total != input_tokens + output_tokens)):
        return None
    return LunaUsage(input_tokens, output_tokens, cached, reasoning, total)


def parse_luna_response(response: dict) -> LunaParsedResponse:
    """Extract assistant text/refusal/status, leaving application JSON validation to the caller."""
    if (not isinstance(response, dict)
            or not isinstance(response.get("status"), str)
            or response.get("status") not in {"completed", "incomplete", "failed", "cancelled",
                                               "queued", "in_progress"}):
        raise LunaTransportError("invalid_response")
    status = response["status"]
    incomplete = response.get("incomplete_details")
    if incomplete is not None and not isinstance(incomplete, dict):
        raise LunaTransportError("invalid_response")
    reason = incomplete.get("reason") if incomplete is not None else None
    if reason is not None and (not isinstance(reason, str)
                              or reason not in {"max_output_tokens", "content_filter"}):
        raise LunaTransportError("invalid_response")
    if status == "completed" and (reason is not None or response.get("error") is not None):
        raise LunaTransportError("invalid_response")
    output = response.get("output", [])
    if not isinstance(output, list):
        raise LunaTransportError("invalid_response")
    texts, refusals = [], []
    for item in output:
        if not isinstance(item, dict):
            raise LunaTransportError("invalid_response")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise LunaTransportError("unexpected_output")
        if status == "completed" and item.get("status") not in {None, "completed"}:
            raise LunaTransportError("invalid_response")
        if not isinstance(content := item.get("content"), list):
            raise LunaTransportError("invalid_response")
        for part in content:
            if not isinstance(part, dict):
                raise LunaTransportError("invalid_response")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                refusals.append(part["refusal"])
            else:
                raise LunaTransportError("invalid_response")
    text = "".join(texts)
    if status == "completed" and not text.strip() and not refusals:
        raise LunaTransportError("missing_output_text")
    return LunaParsedResponse(status, text, reason, "\n".join(refusals) if refusals else None,
                              parse_luna_usage(response))


def _filename_valid(filename: Any) -> bool:
    return (isinstance(filename, str) and bool(filename.strip())
            and filename.lower().endswith(".pdf") and "/" not in filename and "\\" not in filename
            and all(ord(character) >= 32 and ord(character) != 127 for character in filename))


def luna_pdf_part(pdf_bytes: bytes, filename: str = "analysis.pdf") -> dict:
    """Build one complete inline PDF input, without filesystem or Files API writes."""
    if (not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-")
            or not _filename_valid(filename)):
        raise LunaTransportError("invalid_pdf_input")
    if len(pdf_bytes) >= MAX_PDF_BYTES:
        raise LunaTransportError("pdf_file_too_large")
    return {"type": "input_file", "filename": filename,
            "file_data": PDF_DATA_PREFIX + base64.b64encode(pdf_bytes).decode("ascii"),
            "detail": "high"}


def _inline_pdf_size(data: Any) -> int:
    if not isinstance(data, str):
        raise LunaTransportError("invalid_pdf_input")
    encoded = data[len(PDF_DATA_PREFIX):] if data.startswith(PDF_DATA_PREFIX) else data
    # Reject oversize encodings before allocating a decoded copy.
    if len(encoded) > ((MAX_PDF_BYTES + 2) // 3) * 4:
        raise LunaTransportError("pdf_file_too_large")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise LunaTransportError("invalid_pdf_input") from None
    if len(decoded) >= MAX_PDF_BYTES:
        raise LunaTransportError("pdf_file_too_large")
    if not decoded.startswith(b"%PDF-"):
        raise LunaTransportError("invalid_pdf_input")
    return len(decoded)


def _contents(contents: list[dict]) -> list[dict]:
    if not isinstance(contents, list) or not contents:
        raise LunaTransportError("invalid_request")
    total_file_bytes, normalized = 0, []
    for part in contents:
        if not isinstance(part, dict):
            raise LunaTransportError("invalid_request")
        if part.get("type") == "input_text":
            if set(part) != {"type", "text"} or not isinstance(part["text"], str) or not part["text"].strip():
                raise LunaTransportError("invalid_request")
            normalized.append(dict(part))
        elif part.get("type") == "input_file":
            if (not {"type", "filename", "file_data"} <= set(part)
                    or set(part) - {"type", "filename", "file_data", "detail"}
                    or not _filename_valid(part["filename"])
                    or part.get("detail", "high") != "high"):
                raise LunaTransportError("invalid_pdf_input")
            total_file_bytes += _inline_pdf_size(part["file_data"])
            if total_file_bytes > MAX_PDF_BYTES:
                raise LunaTransportError("pdf_request_too_large")
            normalized.append({**part, "detail": "high"})
        else:
            raise LunaTransportError("invalid_request")
    return normalized


class LunaResponsesClient:
    """One synchronous request per generate call; caller authorizes every paid attempt.

    An injected client is caller-owned and must itself have a transport without
    automatic retries. Redirects and timeouts are overridden on every request.
    The audit callback should persist only status/headers/body, never request
    headers, which contain the API credential.
    """

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if (not isinstance(api_key, str) or not api_key
                or any(ord(character) <= 32 or ord(character) >= 127 for character in api_key)):
            raise LunaTransportError("invalid_api_key")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(
            timeout=REQUEST_TIMEOUT, follow_redirects=False, trust_env=False,
            transport=httpx.HTTPTransport(retries=0))
        self._closed = False

    def generate(self, *, model: str = LUNA_MODEL, instructions: str,
                 contents: list[dict], max_output_tokens: int = 65_536,
                 on_response: Callable[[httpx.Response], None] | None = None) -> dict:
        if self._closed:
            raise LunaTransportError("client_closed")
        if (model != LUNA_MODEL or not isinstance(instructions, str) or not instructions.strip()
                or type(max_output_tokens) is not int or not 0 < max_output_tokens <= MAX_OUTPUT_TOKENS
                or on_response is not None and not callable(on_response)):
            raise LunaTransportError("invalid_request")
        parts = _contents(contents)
        if "json" not in instructions.lower() and not any(
                "json" in part.get("text", "").lower() for part in parts):
            raise LunaTransportError("json_instruction_required")
        payload = {"model": LUNA_MODEL, "instructions": instructions,
                   "input": [{"role": "user", "content": parts}],
                   "reasoning": {"effort": "low"}, "store": False,
                   # The API calls Standard pricing/performance "default".
                   "service_tier": "default", "max_output_tokens": max_output_tokens,
                   "text": {"format": {"type": "json_object"}}}
        try:
            response = self._client.post(RESPONSES_URL, json=payload,
                headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT, follow_redirects=False)
        except httpx.HTTPStatusError as error:
            # Caller-supplied response hooks may already have checked the status.
            response = error.response
        except httpx.TimeoutException:
            raise LunaTransportError("http_timeout", request_dispatched=True) from None
        except httpx.HTTPError:
            raise LunaTransportError("http_transport_error", request_dispatched=True) from None
        if on_response is not None:
            try:
                on_response(response)
            except Exception:
                raise LunaTransportError("response_audit_failed", status_code=response.status_code,
                    request_dispatched=True, response_received=True) from None
        if not 200 <= response.status_code < 300:
            raise LunaTransportError("http_status", status_code=response.status_code,
                request_dispatched=True, response_received=True)
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError):
            raise LunaTransportError("invalid_response_json", status_code=response.status_code,
                request_dispatched=True, response_received=True) from None
        if not isinstance(body, dict):
            raise LunaTransportError("invalid_response_json", status_code=response.status_code,
                request_dispatched=True, response_received=True)
        return body

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_client:
                self._client.close()

    def __enter__(self) -> LunaResponsesClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
