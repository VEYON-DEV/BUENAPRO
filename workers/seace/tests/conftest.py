"""Shared offline defaults: SEACE requests are never paced or counted across tests."""
import pytest

from buenapro_worker.seace import pacing


@pytest.fixture(autouse=True)
def fresh_source_pacer(monkeypatch):
    """No real sleeping, and no breaker/budget state leaking from one test into another."""
    monkeypatch.setenv("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "0")
    for name in ("PROD6_MAX_SOURCE_REQUESTS", "PROD6_BLOCK_BACKOFF_SECONDS",
                 "PROD6_READING_BACKOFF_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(pacing, "PACER", pacing.SourcePacer())
