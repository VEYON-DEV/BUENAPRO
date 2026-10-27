"""PROD6 under the shared SEACE gate: shared turn, shared block, and the runtime's egress route."""
import json

import httpx
import pytest

from buenapro_worker.prod6.runtime import SERVICE_STOP_ERRORS
from buenapro_worker.seace import egress, pacing
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.seace.pacing import SourceBlocked, SourcePacer, source_stop
from buenapro_worker.seace.shared_gate import SharedGateUnavailable
from buenapro_worker.settings import Settings
from test_seace_pacing import FakeClock, build_client


def gated_pacer(gate, interval=0):
    """A pacer whose sleeping moves both its own clock and the gate's."""
    clock = FakeClock()

    def sleep(seconds):
        clock.sleep(seconds)
        gate.sleep(seconds)

    return SourcePacer(interval, clock=clock.time, sleep=sleep), clock


def pacer_without_gate():
    clock = FakeClock()
    return SourcePacer(6, clock=clock.time, sleep=clock.sleep), clock


def test_without_the_gate_the_pacer_behaves_as_before():
    p, clock = pacer_without_gate()
    for _ in range(3):
        p.acquire()
    assert clock.sleeps == [6, 6]


def test_the_shared_turn_waits_for_what_other_processes_reserved(fake_gate):
    p, clock = gated_pacer(fake_gate, interval=2)
    fake_gate.next_slot = fake_gate.now_ms + 10_000  # PROD2 or the web already hold the next turns.
    p.acquire()
    p.acquire()
    assert clock.sleeps == [10, 6]  # The second request's local gap had already passed.
    assert fake_gate.next_slot == 1_000_000 + 10_000 + 2 * 6000


def test_local_gap_and_shared_gap_overlap_instead_of_adding_up(fake_gate):
    p, clock = gated_pacer(fake_gate, interval=6)
    for _ in range(3):
        p.acquire()
    assert clock.sleeps == [6, 6]  # Not 12 each: the shared turn is free once the local gap passed.


def test_a_shared_block_trips_the_breaker_and_stops_the_request(fake_gate):
    p, _ = gated_pacer(fake_gate)
    p.begin_cycle(40)
    p.acquire()
    fake_gate.write_block(json.dumps({"status": 429, "source": "web", "at": "x"}), 60_000)
    with pytest.raises(SourceBlocked) as raised:
        p.acquire()
    assert raised.value.status == 429 and p.blocked and p.blocked_status == 429
    assert p.used == 1  # The refused request neither left the machine nor spent the budget.
    calls = len(fake_gate.calls)
    with pytest.raises(SourceBlocked):
        p.acquire()
    assert len(fake_gate.calls) == calls  # The open breaker answers without asking Redis again.


def test_a_shared_block_outside_the_runtime_still_refuses_without_the_breaker(fake_gate):
    p, _ = gated_pacer(fake_gate)
    fake_gate.write_block(json.dumps({"status": 403, "source": "prod2", "at": "x"}), 60_000)
    with pytest.raises(SourceBlocked) as raised:
        p.acquire()
    assert raised.value.status == 403 and not p.blocked


def test_a_denial_arriving_while_waiting_for_the_shared_turn_stops_the_request(fake_gate):
    p, clock = gated_pacer(fake_gate)
    p.begin_cycle(40)
    fake_gate.next_slot = fake_gate.now_ms + 5000
    original = p._sleep

    def sleep(seconds):
        original(seconds)
        fake_gate.write_block(json.dumps({"status": 403, "source": "prod2", "at": "x"}), 60_000)

    p._sleep = sleep
    with pytest.raises(SourceBlocked):
        p.acquire()
    assert p.blocked and p.used == 0


def test_redis_down_refuses_without_sending_and_refunds_the_budget(fake_gate):
    p, _ = gated_pacer(fake_gate)
    p.begin_cycle(40)
    fake_gate.fail = ConnectionError("refused")
    with pytest.raises(SharedGateUnavailable):
        p.acquire()
    assert p.used == 0 and not p.blocked  # An outage is not a block.


def test_the_pacer_lock_is_free_while_sleeping_and_calling_redis(fake_gate):
    p, _ = gated_pacer(fake_gate)
    held = []
    original_reserve = fake_gate.reserve

    def reserve(interval, max_wait):
        held.append(p._lock.locked())
        return original_reserve(interval, max_wait)

    fake_gate.reserve = reserve
    fake_gate.next_slot = fake_gate.now_ms + 3000
    original = p._sleep
    p._sleep = lambda seconds: (held.append(p._lock.locked()), original(seconds))
    p.acquire()
    assert held and not any(held)


def test_client_denial_writes_the_shared_block_as_prod6(fake_gate):
    p, _ = gated_pacer(fake_gate)
    p.begin_cycle(40)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    with build_client(handler, p) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client._get_json("/a")
    payload = json.loads(fake_gate.read_block()[0])
    assert (payload["status"], payload["source"]) == (403, "prod6")
    assert len(calls) == 1


def test_client_429_is_shared_too_and_other_statuses_are_not(fake_gate):
    p, _ = gated_pacer(fake_gate)
    for status in (404, 200):
        with build_client(lambda request, status=status: httpx.Response(status, json={}), p) as client:
            try:
                client._get_json("/a")
            except httpx.HTTPStatusError:
                pass
    assert fake_gate.block is None
    with build_client(lambda request: httpx.Response(429), p) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client._get_json("/a")
    assert json.loads(fake_gate.read_block()[0])["status"] == 429


def test_client_does_not_send_when_the_block_comes_from_another_process(fake_gate):
    p, _ = gated_pacer(fake_gate)
    p.begin_cycle(40)
    fake_gate.write_block(json.dumps({"status": 403, "source": "web", "at": "x"}), 60_000)
    calls = []
    with build_client(lambda request: calls.append(request) or httpx.Response(200, json={}), p) as client:
        with pytest.raises(SourceBlocked) as raised:
            client.search_contracts(anio=2026)
        with pytest.raises(SourceBlocked):
            client.download_file(7)
    assert calls == [] and raised.value.status == 403 and source_stop(raised.value) == "blocked"


def test_client_with_the_gate_off_writes_nothing_and_a_denial_still_trips(monkeypatch):
    p, _ = pacer_without_gate()
    p.begin_cycle(40)
    with build_client(lambda request: httpx.Response(403), p) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client._get_json("/a")
    assert p.blocked and p.blocked_status == 403


def test_a_gate_outage_mid_cycle_follows_the_existing_egress_route():
    error = SharedGateUnavailable("SEACE shared gate unavailable: ConnectionError")
    assert egress.is_egress_failure(error)  # Discovery and IO jobs defer through egress_backoff.
    assert isinstance(error, egress.EgressUnavailable)  # Serial jobs: the same deferral by class.
    assert source_stop(error) is None  # It is neither a source block nor a spent budget.
    assert not isinstance(error, SERVICE_STOP_ERRORS)  # So it never ends in attention_required.


def test_prod6_keeps_its_own_user_agent_even_with_the_override(monkeypatch):
    monkeypatch.setenv("SEACE_USER_AGENT", "Mozilla/5.0 (override)")
    seen = []
    settings = Settings(database_url="postgresql://offline:offline@localhost/offline",
                        gemini_api_key="offline", _env_file=None)
    with SeaceClient(settings) as client:
        assert client._client.headers["User-Agent"] == settings.seace_user_agent
        client._client._transport = httpx.MockTransport(
            lambda request: seen.append(request.headers["user-agent"]) or httpx.Response(200, json={}))
        client._get_json("/a")
    assert seen == [settings.seace_user_agent]
