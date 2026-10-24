"""Offline contract of the PROD6 pacer, request budget and 403/429 breaker."""
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from tenacity import wait_none

from buenapro_worker.jobs.process_contract import process_contract
from buenapro_worker.seace import pacing
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.seace.pacing import (
    SourceBlocked, SourceBudgetReached, SourcePacer, blocking_status, source_stop,
)
from buenapro_worker.settings import Settings


class FakeClock:
    """Reloj y sleep inyectables: dormir adelanta el reloj, sin espera real."""

    def __init__(self, now=1000.0):
        self.now, self.sleeps = now, []
        self.lock = threading.Lock()

    def time(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.now += seconds


def pacer(interval=6, clock=None):
    clock = clock or FakeClock()
    return SourcePacer(interval, clock=clock.time, sleep=clock.sleep), clock


def test_requests_are_spaced_by_the_minimum_interval():
    p, clock = pacer(6)
    stamps = []
    for _ in range(4):
        p.acquire()
        stamps.append(clock.now)
    assert stamps == [1000, 1006, 1012, 1018]
    assert clock.sleeps == [6, 6, 6]  # The first request does not wait.


def test_time_already_elapsed_counts_toward_the_interval():
    p, clock = pacer(6)
    p.acquire()
    clock.now += 4
    p.acquire()
    assert clock.sleeps == [2]
    clock.now += 60
    p.acquire()
    assert clock.sleeps == [2]


def test_threads_share_one_global_slot_sequence():
    # With a frozen clock every thread must be handed a distinct slot 6 s apart.
    clock = FakeClock()
    p = SourcePacer(6, clock=clock.time, sleep=clock.sleeps.append)
    ready = threading.Barrier(8)

    def worker():
        ready.wait(timeout=5)
        p.acquire()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(clock.sleeps) == [6, 12, 18, 24, 30, 36, 42]


def test_real_threads_never_get_closer_than_the_interval():
    interval, stamps, lock = 0.05, [], threading.Lock()
    p = SourcePacer(interval)

    def worker():
        for _ in range(2):
            p.acquire()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    stamps.sort()
    assert len(stamps) == 10
    assert min(b - a for a, b in zip(stamps, stamps[1:])) >= interval * 0.8


def test_interval_comes_from_the_environment_with_a_default_of_six_seconds(monkeypatch):
    clock = FakeClock()
    p = SourcePacer(clock=clock.time, sleep=clock.sleep)
    monkeypatch.delenv("PROD6_MIN_REQUEST_INTERVAL_SECONDS")
    assert p.interval == 6
    monkeypatch.setenv("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "2.5")
    p.acquire()
    p.acquire()
    assert clock.sleeps == [2.5]
    monkeypatch.setenv("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "0")
    p.acquire()  # Its slot was reserved under the previous interval.
    p.acquire()
    assert clock.sleeps == [2.5, 2.5]


@pytest.mark.parametrize("name,value", [
    ("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "-1"), ("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "fast"),
    ("PROD6_MAX_SOURCE_REQUESTS", "0"), ("PROD6_MAX_SOURCE_REQUESTS", "1.5"),
    ("PROD6_BLOCK_BACKOFF_SECONDS", "0"), ("PROD6_BLOCK_BACKOFF_SECONDS", "3h")])
def test_invalid_environment_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        {"PROD6_MIN_REQUEST_INTERVAL_SECONDS": pacing.min_request_interval,
         "PROD6_MAX_SOURCE_REQUESTS": pacing.default_max_source_requests,
         "PROD6_BLOCK_BACKOFF_SECONDS": pacing.block_backoff_seconds}[name]()


def test_environment_defaults(monkeypatch):
    for name in ("PROD6_MIN_REQUEST_INTERVAL_SECONDS", "PROD6_MAX_SOURCE_REQUESTS",
                 "PROD6_BLOCK_BACKOFF_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    assert (pacing.min_request_interval(), pacing.default_max_source_requests(),
            pacing.block_backoff_seconds()) == (6, 40, 10800)


def test_budget_stops_requests_before_they_are_made_and_resets_each_cycle():
    p, clock = pacer(0)
    p.begin_cycle(3)
    for _ in range(3):
        p.acquire()
    assert (p.used, p.remaining, p.budget_reached) == (3, 0, False)
    with pytest.raises(SourceBudgetReached):
        p.acquire()
    assert p.budget_reached and p.used == 3
    p.begin_cycle(3)
    assert (p.used, p.remaining, p.budget_reached) == (0, 3, False)
    p.acquire()
    assert p.used == 1


def test_without_a_cycle_the_pacer_only_paces():
    p, _ = pacer(0)
    for _ in range(100):
        p.acquire()
    p.trip(403)
    p.acquire()  # No breaker outside the runtime.
    assert not p.blocked and p.remaining is None


def test_breaker_refuses_every_request_after_a_trip():
    p, clock = pacer(6)
    p.begin_cycle(40)
    p.acquire()
    p.trip(403)
    with pytest.raises(SourceBlocked) as raised:
        p.acquire()
    assert raised.value.status == 403 and p.blocked and p.blocked_status == 403
    assert clock.sleeps == []  # A refused request neither waits nor counts.
    assert p.used == 1


def test_a_pause_still_running_starts_the_cycle_blocked():
    p, _ = pacer(0)
    p.begin_cycle(40, blocked=True, blocked_status=429)
    with pytest.raises(SourceBlocked) as raised:
        p.acquire()
    assert raised.value.status == 429 and p.used == 0
    p.begin_cycle(40)  # The next cycle after the pause is open again.
    p.acquire()


def test_a_thread_waiting_for_its_slot_does_not_send_after_another_was_denied():
    clock = FakeClock()
    p = SourcePacer(6, clock=clock.time, sleep=lambda seconds: p.trip(403))
    p.begin_cycle(40)
    p.acquire()
    with pytest.raises(SourceBlocked):
        p.acquire()  # Its sleep is where the denial arrives.
    assert p.used == 1


def test_status_helpers():
    def failure(status):
        request = httpx.Request("GET", "https://example.invalid/x")
        return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))

    assert [blocking_status(failure(status)) for status in (403, 429, 404, 503)] == [403, 429, None, None]
    assert source_stop(failure(403)) == source_stop(failure(429)) == source_stop(SourceBlocked(403)) == "blocked"
    assert source_stop(SourceBudgetReached()) == "budget"
    assert source_stop(failure(503)) is None and source_stop(ValueError()) is None


# --- SeaceClient: every request goes through the pacer and 403/429 opens the breaker ---

def build_client(handler, p):
    settings = Settings(database_url="postgresql://offline:offline@localhost/offline",
                        gemini_api_key="offline", _env_file=None)
    client = SeaceClient(settings, pacer=p)
    client._client.close()
    client._client = httpx.Client(base_url=settings.seace_base_url,
                                  transport=httpx.MockTransport(handler))
    return client


def test_every_client_request_and_retry_is_paced_and_counted():
    p, clock = pacer(6)
    p.begin_cycle(40)
    calls = []

    def handler(request):
        calls.append(clock.now)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"ok": True})

    with build_client(handler, p) as client:
        client._get_json.retry_with(wait=wait_none())(client, "/a")
        client._get_bytes.retry_with(wait=wait_none())(client, "/b")
    assert calls == [1000, 1006, 1012]
    assert p.used == 3


@pytest.mark.parametrize("status", [403, 429])
def test_a_denial_is_one_attempt_and_stops_all_later_requests(status):
    p, _ = pacer(0)
    p.begin_cycle(40)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "denied"})

    with build_client(handler, p) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client._get_json.retry_with(wait=wait_none())(client, "/a")
        assert p.blocked and p.blocked_status == status
        with pytest.raises(SourceBlocked):
            client.download_file(1)
        with pytest.raises(SourceBlocked):
            client.search_contracts(anio=2026)
        with pytest.raises(SourceBlocked):
            client.list_files(1)
    assert len(calls) == 1  # Nothing reached the network after the denial.


def test_transient_errors_do_not_open_the_breaker():
    p, _ = pacer(0)
    p.begin_cycle(40)

    def handler(request):
        return httpx.Response(404)

    with build_client(handler, p) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client._get_bytes.retry_with(wait=wait_none())(client, "/a")
    assert not p.blocked


def test_budget_refuses_client_requests_without_sending():
    p, _ = pacer(0)
    p.begin_cycle(1)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    with build_client(handler, p) as client:
        client._get_json("/a")
        with pytest.raises(SourceBudgetReached):
            client._get_json("/b")
    assert len(calls) == 1


def test_clients_share_the_process_wide_pacer_by_default(monkeypatch):
    p, _ = pacer(0)
    monkeypatch.setattr(pacing, "PACER", p)
    settings = Settings(database_url="postgresql://offline:offline@localhost/offline",
                        gemini_api_key="offline", _env_file=None)
    with SeaceClient(settings) as first, SeaceClient(settings) as second:
        assert first._pacer is second._pacer is p


# --- process_contract must not turn a PROD6 stop into "no attachments" ---

def failure(status):
    request = httpx.Request("GET", "https://example.invalid/files")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))


@pytest.mark.parametrize("error", [failure(403), failure(429), SourceBlocked(403), SourceBudgetReached()])
def test_attachment_listing_stopped_by_the_source_is_not_stored_as_empty(error):
    with patch("buenapro_worker.jobs.process_contract.SeaceClient") as client_class, \
            patch("buenapro_worker.jobs.process_contract.update_contract_detail") as update, \
            patch("buenapro_worker.jobs.process_contract.upsert_files") as upsert:
        client = client_class.return_value.__enter__.return_value
        client.contract_detail.return_value = {}
        client.list_files.side_effect = [[{"idContratoArchivo": 1}], error]
        with pytest.raises(type(error)):
            process_contract(MagicMock(), MagicMock(), 42, force_detail=True)
    update.assert_not_called()  # Nothing is saved for a contract whose file list is incomplete.
    upsert.assert_not_called()


def test_other_attachment_listing_errors_still_mean_no_attachments():
    with patch("buenapro_worker.jobs.process_contract.SeaceClient") as client_class, \
            patch("buenapro_worker.jobs.process_contract.update_contract_detail", return_value=False), \
            patch("buenapro_worker.jobs.process_contract.upsert_files", return_value=False) as upsert:
        client = client_class.return_value.__enter__.return_value
        client.contract_detail.return_value = {}
        client.list_files.side_effect = [[{"idContratoArchivo": 1}], failure(404)]
        process_contract(MagicMock(), MagicMock(), 42, force_detail=True)
    assert len(upsert.call_args.args[3]) == 1
