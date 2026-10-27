"""Token pace for the paid Gemini calls of the PROD6 runtime (readings and evaluations).

On 26/10 nine evaluations went out in 30 seconds (about 248 000 tokens, four of them ~58 000
each) and Gemini answered 429 for the minute. Every call that the provider answered delays the
next send by the time its tokens take at ``PROD6_GEMINI_TOKENS_PER_MINUTE`` (default 150 000,
``0`` disables): a 58 000-token evaluation waits ~23 s, a 4 000-token one ~1.6 s. Replays of a
saved answer send nothing and never wait.

The wait happens before the ``started`` receipt is written, so an interrupted wait never leaves
an uncertain request behind.

When the main model answers that it is saturated (500/502/503/504/529), the same request goes once
to ``PROD6_GEMINI_SATURATION_FALLBACK_MODEL`` (default ``gemini-3.5-flash-lite``, another model
family; empty disables). Each model has its own receipts, so a fallback answer is replayed later
without paying again.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from buenapro_worker.extraction.gemini import MODEL_PRICES_USD_PER_MILLION
from buenapro_worker.seace.pacing import _env_number

DEFAULT_TOKENS_PER_MINUTE = 150_000
DEFAULT_SATURATION_FALLBACK_MODEL = "gemini-3.5-flash-lite"
# Gemini overloaded, not the account nor the request.
SATURATION_CODES = frozenset({500, 502, 503, 504, 529})


def tokens_per_minute() -> float:
    return _env_number("PROD6_GEMINI_TOKENS_PER_MINUTE", DEFAULT_TOKENS_PER_MINUTE, minimum=0)


def saturation_fallback_model() -> str | None:
    raw = os.environ.get("PROD6_GEMINI_SATURATION_FALLBACK_MODEL")
    model = DEFAULT_SATURATION_FALLBACK_MODEL if raw is None else raw.strip()
    if not model:
        return None
    if model not in MODEL_PRICES_USD_PER_MILLION:
        raise ValueError("PROD6_GEMINI_SATURATION_FALLBACK_MODEL must be a model with known local prices")
    return model


class GeminiPacer:
    def __init__(self, *, rate: Callable[[], float] = tokens_per_minute,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._rate, self._clock, self._sleep = rate, clock, sleep
        self._lock = threading.Lock()
        self._next_send_at = 0.0

    def wait(self) -> float:
        """Block until the next send is within the token pace; returns the seconds waited."""
        with self._lock:
            delay = max(0.0, self._next_send_at - self._clock())
        if delay:
            self._sleep(delay)
        return delay

    def spent(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Record what one answered call consumed."""
        rate = self._rate()
        tokens = (input_tokens or 0) + (output_tokens or 0)
        if not rate or tokens <= 0:
            return
        with self._lock:
            start = max(self._clock(), self._next_send_at)
            self._next_send_at = start + tokens * 60.0 / rate


GEMINI_PACER = GeminiPacer()
