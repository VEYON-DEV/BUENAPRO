"""One SEACE turn and one pause shared by every process that leaves through the Peruvian IP.

SEACE cuts the whole IP after a burst, and PROD6, PROD2 and the web all share it, so each
process pacing itself is not enough. This gate keeps two Redis keys that all of them read:

``seace:egress:next_slot_ms``  the next free request slot (Redis clock, ms), reserved atomically.
``seace:egress:blocked``       JSON ``{"status", "source", "at"}``, set with ``NX`` and a TTL when
                               any process receives 403/429; while it exists nobody sends.

``SEACE_SHARED_GATE``                   ``1``/``true`` turns the gate on (needs ``REDIS_URL``).
``REDIS_URL``                           the Redis shared by every process; empty keeps the gate off.
``SEACE_SHARED_MIN_INTERVAL_SECONDS``   gap between requests of all processes (default 6).
``SEACE_SHARED_BLOCK_SECONDS``          shared pause after a 403/429, in seconds (default 10800).

Off, every function here is a no-op (kill switch). On, Redis failing closes the gate:
``acquire`` and ``check`` raise ``SharedGateUnavailable`` and nothing is sent. Writing the
block is the exception: it is best effort, because the 403 was already received.

This module reads ``os.environ`` instead of ``Settings`` for the same reasons as ``egress``,
and the web implements the same protocol (keys, Lua and JSON) in TypeScript.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from buenapro_worker.seace.egress import EgressUnavailable

LOGGER = logging.getLogger(__name__)
NEXT_SLOT_KEY = "seace:egress:next_slot_ms"
BLOCKED_KEY = "seace:egress:blocked"
DEFAULT_MIN_INTERVAL_SECONDS = 6.0
DEFAULT_BLOCK_SECONDS = 10800
# Replies of the reservation script: a delay in ms (>= 0), or one of these.
RESERVE_BLOCKED = -1
RESERVE_BUSY = -2
NO_MAX_WAIT = -1
SOCKET_TIMEOUT_SECONDS = 5
# Atomic reservation; must stay identical to the one in the web implementation.
RESERVE_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, nxt)
local delay = slot - now
local max_wait = tonumber(ARGV[2])
if max_wait >= 0 and delay > max_wait then return -2 end
redis.call('SET', KEYS[1], slot + tonumber(ARGV[1]), 'PX', 86400000)
return delay
"""


class SharedGateUnavailable(EgressUnavailable):
    """The shared gate is on but Redis failed (or is misconfigured): nothing may be sent."""


class SharedSourceBlocked(RuntimeError):
    """Another process (or this one) received 403/429 and the shared pause is running."""

    def __init__(self, status: int, source: str, remaining_seconds: float) -> None:
        super().__init__("SEACE source is blocked by the shared gate; request not sent")
        self.status, self.source, self.remaining_seconds = status, source, remaining_seconds


def enabled() -> bool:
    on = os.environ.get("SEACE_SHARED_GATE", "").strip().lower() in {"1", "true"}
    return on and bool(os.environ.get("REDIS_URL", "").strip())


def _env_seconds(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SharedGateUnavailable(f"{name} must be a number") from None
    if not math.isfinite(value) or value < minimum:
        raise SharedGateUnavailable(f"{name} must be a finite number of at least {minimum}")
    return value


def interval_ms() -> int:
    return round(_env_seconds("SEACE_SHARED_MIN_INTERVAL_SECONDS", DEFAULT_MIN_INTERVAL_SECONDS,
                              minimum=0) * 1000)


def block_ms() -> int:
    seconds = _env_seconds("SEACE_SHARED_BLOCK_SECONDS", DEFAULT_BLOCK_SECONDS, minimum=1)
    return round(seconds * 1000)


class RedisBackend:
    """The Redis side of the gate. Tests inject an in-memory object with these four methods."""

    def __init__(self, url: str, *, next_slot_key: str = NEXT_SLOT_KEY,
                 blocked_key: str = BLOCKED_KEY) -> None:
        import redis  # Lazy: only a process with the gate on needs it.

        self._client = redis.Redis.from_url(url, decode_responses=True,
                                            socket_timeout=SOCKET_TIMEOUT_SECONDS,
                                            socket_connect_timeout=SOCKET_TIMEOUT_SECONDS)
        self._reserve = self._client.register_script(RESERVE_SCRIPT)
        self._keys = [next_slot_key, blocked_key]
        self._blocked_key = blocked_key

    def reserve(self, interval: int, max_wait: int) -> int:
        return int(self._reserve(keys=self._keys, args=[interval, max_wait]))

    def read_block(self) -> tuple[str | None, int]:
        """The block's JSON and its remaining ms (-2: no key, -1: no expiry)."""
        pipeline = self._client.pipeline(transaction=False)
        pipeline.get(self._blocked_key)
        pipeline.pttl(self._blocked_key)
        value, ttl = pipeline.execute()
        return value, int(ttl)

    def write_block(self, value: str, ttl_ms: int) -> bool:
        return bool(self._client.set(self._blocked_key, value, nx=True, px=ttl_ms))

    def ping(self) -> None:
        self._client.ping()


_BACKEND: Any = None
_BACKEND_LOCK = threading.Lock()


def reset() -> None:
    """Drop the process-wide backend (tests)."""
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = None


def _backend() -> Any:
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = RedisBackend(os.environ["REDIS_URL"].strip())
        return _BACKEND


def _call(operation: Callable[[Any], Any]) -> Any:
    """Run one backend operation; any failure means the gate cannot vouch for the request."""
    try:
        return operation(_backend())
    except SharedGateUnavailable:
        raise
    except Exception as error:
        # The Redis URL may carry a password: report only the exception type.
        kind = type(error).__name__
        raise SharedGateUnavailable(f"SEACE shared gate unavailable: {kind}") from None


def blocked() -> dict | None:
    """The running shared block as ``{status, source, at, remaining_seconds}``, else None."""
    if not enabled():
        return None
    value, ttl = _call(lambda backend: backend.read_block())
    if value is None or ttl == -2:
        return None
    try:
        payload = json.loads(value)
        status, source = payload["status"], payload["source"]
        at = payload.get("at", "")
        if type(status) is not int or not 100 <= status <= 599 or not isinstance(source, str):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        status, source, at = 403, "unknown", ""
    # A key without expiry is not ours; assume a full pause rather than none.
    remaining = ttl / 1000 if ttl >= 0 else block_ms() / 1000
    return {"status": status, "source": source[:32], "at": str(at)[:40],
            "remaining_seconds": remaining}


def _refuse() -> SharedSourceBlocked | None:
    block = blocked()
    if block is None:
        return None
    return SharedSourceBlocked(block["status"], block["source"], block["remaining_seconds"])


def acquire(*, sleep: Callable[[float], None] | None = None) -> None:
    """Wait for this request's shared turn, or raise without sending anything.

    ``SharedSourceBlocked`` while the shared pause runs; ``SharedGateUnavailable`` if Redis
    fails. A turn reserved before a block appeared is not used: the block is checked again
    after waiting.
    """
    if not enabled():
        return
    gap = interval_ms()
    for _ in range(3):
        delay = _call(lambda backend: backend.reserve(gap, NO_MAX_WAIT))
        if delay == RESERVE_BLOCKED:
            refusal = _refuse()
            if refusal is not None:
                raise refusal
            continue  # The block ended between the script and the read: take a turn again.
        if delay < 0:
            raise SharedGateUnavailable("SEACE shared gate unavailable: unexpected reply")
        if delay > 0:
            (sleep or time.sleep)(delay / 1000)
            refusal = _refuse()
            if refusal is not None:
                raise refusal
        return
    raise SharedGateUnavailable("SEACE shared gate unavailable: inconsistent block")


def block(status: int, source: str) -> bool:
    """Open the shared pause (the first writer wins). Best effort: never raises."""
    if not enabled():
        return False
    try:
        at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        value = json.dumps({"status": status, "source": source, "at": at}, separators=(",", ":"))
        return bool(_call(lambda backend: backend.write_block(value, block_ms())))
    except Exception as error:
        LOGGER.warning("Shared SEACE block not written: %s",
                       error if isinstance(error, SharedGateUnavailable) else type(error).__name__)
        return False


def check() -> None:
    """PING Redis when the gate is on; nothing is reserved and nothing reaches SEACE."""
    if enabled():
        _call(lambda backend: backend.ping())
