"""Shared offline defaults: SEACE requests are never paced or counted across tests."""
import threading
from types import SimpleNamespace

import pytest

from buenapro_worker.prod6 import gemini_pacing
from buenapro_worker.seace import pacing, shared_gate


@pytest.fixture(autouse=True)
def fresh_source_pacer(monkeypatch):
    """No real sleeping, and no breaker/budget state leaking from one test into another."""
    monkeypatch.setenv("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "0")
    for name in ("PROD6_MAX_SOURCE_REQUESTS", "PROD6_BLOCK_BACKOFF_SECONDS",
                 "PROD6_READING_BACKOFF_SECONDS", "PROD6_READING_TRANSIENT_BACKOFF_SECONDS",
                 "PROD6_GEMINI_RATE_BACKOFF_SECONDS", "PROD6_EGRESS_RETRY_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(pacing, "PACER", pacing.SourcePacer())
    # Gemini token pace off unless a test turns it on; no wait state shared between tests.
    monkeypatch.setenv("PROD6_GEMINI_TOKENS_PER_MINUTE", "0")
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "")  # Tests opt in to the fallback.
    monkeypatch.setattr(gemini_pacing.GEMINI_PACER, "_next_send_at", 0.0)


@pytest.fixture(autouse=True)
def fresh_shared_gate(monkeypatch):
    """The shared SEACE gate is off, the User-Agent override unset, and no alert can leave."""
    for name in ("SEACE_SHARED_GATE", "SEACE_SHARED_MIN_INTERVAL_SECONDS", "SEACE_SHARED_BLOCK_SECONDS",
                 "SEACE_USER_AGENT", "TELEGRAM_BOT_TOKEN", "OPS_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    shared_gate.reset()
    yield
    shared_gate.reset()


class FakeGateBackend:
    """In-memory twin of ``shared_gate.RedisBackend``: same replies as the Redis script and TTLs.

    The clock is manual (``advance``), so a test can sleep a whole block away without waiting.
    """

    def __init__(self):
        self.now_ms, self.next_slot, self.block = 1_000_000, None, None  # block: (value, expires_ms)
        self.fail, self.calls = None, []
        self.lock = threading.Lock()

    def advance(self, milliseconds):
        with self.lock:
            self.now_ms += round(milliseconds)

    def sleep(self, seconds):
        self.advance(seconds * 1000)

    def _enter(self, name):
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail

    def _live_block(self):
        if self.block is not None and self.block[1] <= self.now_ms:
            self.block = None
        return self.block

    def reserve(self, interval, max_wait):
        with self.lock:
            self._enter("reserve")
            if self._live_block() is not None:
                return -1
            slot = max(self.now_ms, self.next_slot or 0)
            delay = slot - self.now_ms
            if max_wait >= 0 and delay > max_wait:
                return -2
            self.next_slot = slot + interval
            return delay

    def read_block(self):
        with self.lock:
            self._enter("read_block")
            live = self._live_block()
            return (None, -2) if live is None else (live[0], live[1] - self.now_ms)

    def write_block(self, value, ttl_ms):
        with self.lock:
            self._enter("write_block")
            if self._live_block() is not None:
                return False
            self.block = (value, self.now_ms + ttl_ms)
            return True

    def ping(self):
        with self.lock:
            self._enter("ping")


@pytest.fixture
def fake_gate(monkeypatch):
    """The shared gate on and backed by ``FakeGateBackend``; the gate's own sleeping advances its clock."""
    backend = FakeGateBackend()
    monkeypatch.setenv("SEACE_SHARED_GATE", "1")
    monkeypatch.setenv("REDIS_URL", "redis://gate.invalid:6379/0")
    monkeypatch.setattr(shared_gate, "_BACKEND", backend)
    monkeypatch.setattr(shared_gate, "time", SimpleNamespace(sleep=backend.sleep))
    return backend
