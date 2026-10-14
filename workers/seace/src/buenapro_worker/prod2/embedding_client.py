"""One audited embeddings request; no SDK retries or secret-bearing exceptions.

The durable owner lives in embedding_store. This client validates and reports a
partial response rather than turning a known paid response into a retry.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Callable

import httpx

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
MAX_INPUT_TOKENS = 8192
MAX_BATCH_INPUTS = 64
MAX_BATCH_TOKENS = 64000

# Exact machine codes only. Current billing/rate labels and legacy API labels
# are diagnostics, never permission to retry or evidence of zero cost. Do not
# retain provider messages, bodies, error.type or arbitrary code strings.
# Reference: https://developers.openai.com/api/docs/guides/error-codes#api-errors
SAFE_PROVIDER_ERROR_CODES = frozenset({
    "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
    "insufficient_quota", "rate_limit_exceeded", "invalid_api_key",
    "slow_down", "server_is_overloaded", "server_error", "invalid_request_error",
})


def _safe_http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _safe_provider_error_code(value):
    return value if type(value) is str and value in SAFE_PROVIDER_ERROR_CODES else None


def safe_request_error_facts(error: EmbeddingRequestError) -> dict[str, int | str | None]:
    """Revalidate optional fields at the private receipt boundary, including fakes."""
    return {"http_status": _safe_http_status(getattr(error, "http_status", None)),
            "provider_error_code": _safe_provider_error_code(getattr(error, "provider_error_code", None))}


class EmbeddingRequestError(RuntimeError):
    def __init__(self, code: str, *, request_sent: bool, http_status: int | None = None,
                 provider_error_code: str | None = None):
        super().__init__(code)
        self.code, self.request_sent = code, request_sent
        self.http_status = _safe_http_status(http_status)
        self.provider_error_code = _safe_provider_error_code(provider_error_code)


def token_count(text: str) -> int:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Embedding input must be nonempty text")
    import tiktoken
    return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=()))


def validate_vector(vector, dimensions: int):
    if (not isinstance(vector, list) or len(vector) != dimensions
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)
            or not any(x != 0 for x in vector)):
        raise ValueError("Invalid embedding vector")
    return [float(x) for x in vector]


class EmbeddingClient:
    def __init__(self, *, api_key: str, model: str = MODEL, dimensions: int = DIMENSIONS,
                 price_per_million: float = .02, http_client=None, timeout: float = 90):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Embedding API credential missing")
        if not isinstance(model, str) or not model or type(dimensions) is not int or not 1 <= dimensions <= 3072:
            raise ValueError("Invalid embedding model contract")
        if type(price_per_million) not in (int, float) or not math.isfinite(price_per_million) or price_per_million < 0:
            raise ValueError("Invalid embedding price")
        self.api_key, self.model, self.dimensions = api_key, model, dimensions
        self.price_per_million = float(price_per_million)
        # httpx defaults to retries=0; specify it so future defaults cannot repay.
        self.http_client = http_client or httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=0))
        self.owned = http_client is None

    def embed(self, texts: list[str], *, before_request: Callable | None = None) -> dict:
        if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_BATCH_INPUTS:
            raise EmbeddingRequestError("embedding_batch_size", request_sent=False)
        try:
            counts = [token_count(text) for text in texts]
        except Exception:
            raise EmbeddingRequestError("embedding_tokenization", request_sent=False) from None
        if max(counts) > MAX_INPUT_TOKENS or sum(counts) > MAX_BATCH_TOKENS:
            raise EmbeddingRequestError("embedding_token_limit", request_sent=False)
        body = {"model": self.model, "input": texts, "dimensions": self.dimensions, "encoding_format": "float"}
        try:
            if before_request:
                before_request()
        except Exception:
            raise EmbeddingRequestError("embedding_pre_send_guard", request_sent=False) from None
        # Once post starts, an exception cannot prove the remote did not bill.
        try:
            response = self.http_client.post("https://api.openai.com/v1/embeddings", json=body,
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        except Exception:
            raise EmbeddingRequestError("embedding_transport_unknown", request_sent=True) from None
        digest = sha256(response.content).hexdigest()
        try:
            data = response.json()
        except Exception:
            raise EmbeddingRequestError("embedding_response_unreadable", request_sent=True,
                                        http_status=response.status_code) from None
        if response.status_code != 200 or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else None
            raise EmbeddingRequestError("embedding_http_error", request_sent=True,
                                        http_status=response.status_code,
                                        provider_error_code=error.get("code") if isinstance(error, dict) else None) from None
        usage = data.get("usage", {})
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        usage_known = type(tokens) is int and tokens >= 0 and type(total) is int and total == tokens
        vectors, invalid, duplicates = {}, [], set()
        items = data.get("data")
        if not isinstance(items, list):
            items = []
        for item in items:
            index = item.get("index") if isinstance(item, dict) else None
            if type(index) is not int or not 0 <= index < len(texts):
                invalid.append("unexpected_index")
                continue
            if index in vectors or index in duplicates:
                duplicates.add(index)
                vectors.pop(index, None)
                invalid.append("duplicate_index")
                continue
            try:
                vectors[index] = validate_vector(item.get("embedding"), self.dimensions)
            except ValueError:
                invalid.append("invalid_vector")
        response_model = data.get("model")
        if response_model != self.model:
            invalid.append("model_mismatch")
            vectors = {}
        return {"version": "prod2_embedding_response_v1", "model": self.model,
                "dimensions": self.dimensions, "request_sent": True, "http_status": 200,
                "vectors_by_index": vectors, "complete": len(vectors) == len(texts) and not invalid,
                "validation_errors": sorted(set(invalid)), "provider_usage_known": usage_known,
                "input_tokens": tokens if usage_known else None,
                "total_tokens": total if usage_known else None,
                "cost_usd": tokens * self.price_per_million / 1_000_000 if usage_known else None,
                "price_per_million": self.price_per_million,
                "local_token_count": sum(counts), "response_sha256": digest,
                "provider_response_id": data.get("id") if isinstance(data.get("id"), str) else None}

    def close(self):
        if self.owned:
            self.http_client.close()
