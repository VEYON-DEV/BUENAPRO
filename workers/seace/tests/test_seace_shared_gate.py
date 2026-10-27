"""Shared SEACE gate contracts: in-memory twin of the Redis script, plus an opt-in real Redis."""
from datetime import datetime
import json
import os
import threading
import time
from uuid import uuid4

import httpx
import pytest

from buenapro_worker.seace import egress, shared_gate
from buenapro_worker.seace.shared_gate import SharedGateUnavailable, SharedSourceBlocked

SCRATCH_URL = os.environ.get("REDIS_URL", "")  # Read before fake_gate overrides REDIS_URL.
REAL_REDIS = pytest.mark.skipif(
    os.environ.get("BUENAPRO_REDIS_SCRATCH_TEST") != "1" or not SCRATCH_URL,
    reason="Explicit isolated Redis scratch opt-in required (BUENAPRO_REDIS_SCRATCH_TEST=1 and REDIS_URL)")


@pytest.fixture(autouse=True)
def no_ambient_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)


class Untouchable:
    def __getattr__(self, name):
        pytest.fail(f"The disabled gate touched the backend: {name}")


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", " 1 "])
def test_enabled_needs_the_flag_and_a_redis_url(monkeypatch, flag):
    monkeypatch.setenv("SEACE_SHARED_GATE", flag)
    assert not shared_gate.enabled()  # No REDIS_URL.
    monkeypatch.setenv("REDIS_URL", "  ")
    assert not shared_gate.enabled()
    monkeypatch.setenv("REDIS_URL", "redis://gate.invalid:6379/0")
    assert shared_gate.enabled()


@pytest.mark.parametrize("flag", ["", "0", "false", "yes"])
def test_other_flag_values_keep_the_gate_off(monkeypatch, flag):
    monkeypatch.setenv("SEACE_SHARED_GATE", flag)
    monkeypatch.setenv("REDIS_URL", "redis://gate.invalid:6379/0")
    assert not shared_gate.enabled()


def test_disabled_gate_is_a_no_op_that_never_touches_redis(monkeypatch):
    monkeypatch.setattr(shared_gate, "_BACKEND", Untouchable())
    monkeypatch.setenv("REDIS_URL", "redis://gate.invalid:6379/0")  # Flag still off.
    shared_gate.acquire(sleep=lambda seconds: pytest.fail("slept"))
    shared_gate.check()
    assert shared_gate.blocked() is None
    assert shared_gate.block(403, "web") is False
    egress.check_egress()


def test_turns_are_spaced_by_the_interval(fake_gate, monkeypatch):
    monkeypatch.setenv("SEACE_SHARED_MIN_INTERVAL_SECONDS", "2.5")
    waited = []

    def sleep(seconds):
        waited.append(seconds)
        fake_gate.sleep(seconds)

    for _ in range(3):
        shared_gate.acquire(sleep=sleep)
    assert waited == [2.5, 2.5]  # The first turn is free.
    assert fake_gate.now_ms == 1_000_000 + 5000


def test_default_interval_is_six_seconds_and_the_first_turn_is_free(fake_gate):
    shared_gate.acquire()
    shared_gate.acquire()  # Waits through the gate's own sleep, which advances the fake clock.
    assert fake_gate.now_ms == 1_000_000 + 6000
    assert fake_gate.next_slot == 1_000_000 + 12000


def test_a_turn_reserved_by_another_process_is_waited_for(fake_gate):
    fake_gate.next_slot = fake_gate.now_ms + 4200
    waited = []
    shared_gate.acquire(sleep=lambda seconds: (waited.append(seconds), fake_gate.sleep(seconds)))
    assert waited == [4.2]
    assert fake_gate.next_slot == 1_000_000 + 4200 + 6000


def test_zero_interval_never_waits(fake_gate, monkeypatch):
    monkeypatch.setenv("SEACE_SHARED_MIN_INTERVAL_SECONDS", "0")
    for _ in range(5):
        shared_gate.acquire(sleep=lambda seconds: pytest.fail("slept"))


def test_block_is_first_writer_wins_with_json_and_ttl(fake_gate, monkeypatch):
    monkeypatch.setenv("SEACE_SHARED_BLOCK_SECONDS", "120")
    assert shared_gate.block(403, "prod6") is True
    assert shared_gate.block(429, "web") is False  # NX: the running pause is not extended.
    value, ttl = fake_gate.read_block()
    payload = json.loads(value)
    assert payload["status"] == 403 and payload["source"] == "prod6" and set(payload) == {"status", "source", "at"}
    datetime.strptime(payload["at"], "%Y-%m-%dT%H:%M:%S.%fZ")  # ISO-8601 UTC with milliseconds.
    assert ttl == 120_000


def test_block_duration_defaults_to_three_hours(fake_gate):
    shared_gate.block(403, "prod2")
    assert fake_gate.read_block()[1] == 10800 * 1000


def test_blocked_reports_the_remaining_time_and_expires(fake_gate):
    assert shared_gate.blocked() is None
    shared_gate.block(429, "web")
    fake_gate.advance(60_000)
    block = shared_gate.blocked()
    assert (block["status"], block["source"], block["remaining_seconds"]) == (429, "web", 10800 - 60)
    fake_gate.advance(10800 * 1000)
    assert shared_gate.blocked() is None


def test_a_foreign_or_malformed_block_is_still_a_block(fake_gate):
    fake_gate.block = ("not json", fake_gate.now_ms + 5000)
    assert shared_gate.blocked() == {"status": 403, "source": "unknown", "at": "", "remaining_seconds": 5.0}
    fake_gate.block = ('{"status": "x", "source": 7}', fake_gate.now_ms + 5000)
    assert shared_gate.blocked()["source"] == "unknown"


def test_a_block_without_expiry_assumes_a_full_pause(fake_gate):
    fake_gate.read_block = lambda: ('{"status":403,"source":"web","at":"x"}', -1)
    assert shared_gate.blocked()["remaining_seconds"] == 10800


def test_acquire_refuses_while_blocked_and_reserves_nothing(fake_gate):
    shared_gate.block(403, "prod2")
    before = fake_gate.next_slot
    with pytest.raises(SharedSourceBlocked) as raised:
        shared_gate.acquire(sleep=lambda seconds: pytest.fail("slept"))
    assert (raised.value.status, raised.value.source) == (403, "prod2")
    assert raised.value.remaining_seconds == 10800
    assert fake_gate.next_slot == before


def test_a_block_that_appears_while_waiting_stops_the_request(fake_gate):
    shared_gate.acquire()  # The next one waits an interval; the denial lands meanwhile.

    def deny_meanwhile(seconds):
        fake_gate.sleep(seconds)
        shared_gate.block(403, "web")

    with pytest.raises(SharedSourceBlocked) as raised:
        shared_gate.acquire(sleep=deny_meanwhile)
    assert raised.value.source == "web"


def test_a_block_that_ends_between_the_script_and_the_read_takes_a_new_turn(fake_gate):
    replies = iter([-1, 0])
    fake_gate.reserve = lambda interval, max_wait: next(replies)
    shared_gate.acquire()


def test_redis_failure_closes_the_gate_without_leaking_the_url(fake_gate):
    fake_gate.fail = ConnectionError("redis://:hunter2@gate.invalid:6379/0 refused")
    for call in (shared_gate.acquire, shared_gate.check, shared_gate.blocked):
        with pytest.raises(SharedGateUnavailable, match="ConnectionError") as raised:
            call()
        assert "hunter2" not in str(raised.value)
        assert isinstance(raised.value, egress.EgressUnavailable)
    with pytest.raises(SharedGateUnavailable):
        egress.check_egress()


def test_egress_treats_the_gate_failure_as_an_egress_failure():
    assert egress.is_egress_failure(SharedGateUnavailable("down"))
    assert not egress.is_egress_failure(SharedSourceBlocked(403, "web", 1.0))
    assert not egress.is_egress_failure(ValueError("x"))


def test_check_pings_redis_and_check_egress_runs_it_after_the_proxy(fake_gate):
    shared_gate.check()
    egress.check_egress()
    assert fake_gate.calls == ["ping", "ping"]


def test_writing_the_block_is_best_effort_and_never_raises(fake_gate, caplog):
    fake_gate.fail = TimeoutError("redis://:hunter2@gate.invalid:6379/0")
    with caplog.at_level("WARNING"):
        assert shared_gate.block(403, "prod6") is False
    assert "TimeoutError" in caplog.text and "hunter2" not in caplog.text


@pytest.mark.parametrize("value", ["soon", "-1", "nan", "inf"])
def test_invalid_interval_fails_closed(fake_gate, monkeypatch, value):
    monkeypatch.setenv("SEACE_SHARED_MIN_INTERVAL_SECONDS", value)
    with pytest.raises(SharedGateUnavailable, match="SEACE_SHARED_MIN_INTERVAL_SECONDS"):
        shared_gate.acquire()
    assert "reserve" not in fake_gate.calls


@pytest.mark.parametrize("value", ["soon", "0"])
def test_invalid_block_duration_is_not_written_and_does_not_raise(fake_gate, monkeypatch, value):
    monkeypatch.setenv("SEACE_SHARED_BLOCK_SECONDS", value)
    assert shared_gate.block(403, "web") is False
    assert fake_gate.block is None


# --- User-Agent override of the SEACE client factory ---

def user_agent(**kwargs):
    seen = []
    transport = httpx.MockTransport(lambda request: seen.append(request.headers["user-agent"]) or httpx.Response(200))
    with egress.seace_http_client(transport=transport, **kwargs) as client:
        client.get("https://prod2.seace.gob.pe/x")
    return seen[0]


def test_user_agent_override_applies_only_when_the_caller_brings_none(monkeypatch):
    assert user_agent().startswith("python-httpx")
    monkeypatch.setenv("SEACE_USER_AGENT", "  Mozilla/5.0 (test)  ")
    assert user_agent() == "Mozilla/5.0 (test)"
    assert user_agent(headers={"Accept": "*/*"}) == "Mozilla/5.0 (test)"
    assert user_agent(headers={"user-agent": "BuenaPro/0.1"}) == "BuenaPro/0.1"
    assert user_agent(headers=[("USER-AGENT", "BuenaPro/0.1")]) == "BuenaPro/0.1"


def test_blank_user_agent_variable_changes_nothing(monkeypatch):
    monkeypatch.setenv("SEACE_USER_AGENT", "   ")
    assert user_agent().startswith("python-httpx")
    assert user_agent(headers={"User-Agent": "BuenaPro/0.1"}) == "BuenaPro/0.1"


# --- The same contract against the in-memory twin and, opt-in, the real Lua script ---

@pytest.fixture(params=["fake", pytest.param("redis", marks=REAL_REDIS)])
def contract(request):
    """(backend, pause): ``pause(ms)`` lets time pass on the backend's own clock."""
    if request.param == "fake":
        backend = request.getfixturevalue("fake_gate")
        yield backend, backend.advance
        return
    suffix = uuid4().hex
    backend = shared_gate.RedisBackend(SCRATCH_URL,
                                       next_slot_key=f"seace:egress-test:{suffix}:next_slot_ms",
                                       blocked_key=f"seace:egress-test:{suffix}:blocked")
    try:
        yield backend, lambda ms: time.sleep(ms / 1000)
    finally:
        backend._client.delete(*backend._keys)
        backend._client.close()


def test_contract_reservations_queue_one_interval_apart(contract):
    backend, _ = contract
    first, second, third = (backend.reserve(6000, -1) for _ in range(3))
    assert first == 0
    assert 5900 <= second <= 6000 and 11900 <= third <= 12000


def test_contract_busy_reply_reserves_nothing(contract):
    backend, _ = contract
    backend.reserve(6000, -1)
    assert backend.reserve(6000, 1000) == shared_gate.RESERVE_BUSY
    assert 5900 <= backend.reserve(6000, 6500) <= 6000  # Still the second turn: -2 took none.
    assert backend.reserve(6000, 6500) == shared_gate.RESERVE_BUSY


def test_contract_block_is_nx_with_ttl_and_closes_the_gate(contract):
    backend, pause = contract
    assert backend.read_block() == (None, -2)
    assert backend.write_block('{"status":403}', 400) is True
    assert backend.write_block('{"status":429}', 400) is False
    value, ttl = backend.read_block()
    assert value == '{"status":403}' and 0 < ttl <= 400
    assert backend.reserve(6000, -1) == shared_gate.RESERVE_BLOCKED
    assert backend.reserve(6000, 0) == shared_gate.RESERVE_BLOCKED
    pause(600)
    assert backend.read_block() == (None, -2)
    assert backend.reserve(6000, -1) >= 0


def test_contract_ping(contract):
    contract[0].ping()


@REAL_REDIS
def test_real_redis_turns_of_two_threads_stay_one_interval_apart(monkeypatch):
    suffix = uuid4().hex
    backend = shared_gate.RedisBackend(SCRATCH_URL,
                                       next_slot_key=f"seace:egress-test:{suffix}:next_slot_ms",
                                       blocked_key=f"seace:egress-test:{suffix}:blocked")
    monkeypatch.setenv("SEACE_SHARED_GATE", "1")
    monkeypatch.setenv("REDIS_URL", SCRATCH_URL)
    monkeypatch.setenv("SEACE_SHARED_MIN_INTERVAL_SECONDS", "0.25")
    monkeypatch.setattr(shared_gate, "_BACKEND", backend)
    stamps, lock = [], threading.Lock()

    def worker():
        for _ in range(4):
            shared_gate.acquire()
            with lock:
                stamps.append(time.time())

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        stamps.sort()
        assert len(stamps) == 8
        assert all(later - earlier >= 0.25 - 0.03 for earlier, later in zip(stamps, stamps[1:]))
        backend.write_block('{"status":403,"source":"web","at":"x"}', 5000)
        with pytest.raises(SharedSourceBlocked) as raised:
            shared_gate.acquire()
        assert raised.value.source == "web" and 0 < raised.value.remaining_seconds <= 5
    finally:
        backend._client.delete(*backend._keys)
        backend._client.close()
