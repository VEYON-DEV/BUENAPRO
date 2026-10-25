"""Slow, fixed pace for every PROD6 request made by SeaceClient.

SEACE's WAF answers 403 to our whole IP after a burst of requests, and production
leaves through the same Peruvian IP. One process-wide pacer keeps a minimum gap
between consecutive requests no matter how many threads or clients exist:

``PROD6_MIN_REQUEST_INTERVAL_SECONDS``  minimum gap in seconds (default 6, ``0`` disables).
``PROD6_MAX_SOURCE_REQUESTS``           default of the runtime's ``--max-source-requests`` (40).
``PROD6_DISCOVERY_INTERVAL_SECONDS``    default of the runtime's ``--discovery-interval-seconds`` (7200, ``0`` = every cycle).
``PROD6_BLOCK_BACKOFF_SECONDS``         pause after a 403/429 from the source (default 10800).
``PROD6_READING_BACKOFF_SECONDS``       pause of paid readings after Gemini answers 402/429 (default 3600).

The pace is fixed and low on purpose: there is no jitter, rotation or evasion.

Outside the PROD6 runtime the pacer only paces. The runtime calls ``begin_cycle``,
which also turns on the request budget of the cycle and the circuit breaker: once the
source answered 403/429 (``trip``), or the budget is spent, ``acquire`` refuses before
anything reaches the network.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

import httpx

DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 6.0
DEFAULT_MAX_SOURCE_REQUESTS = 40
DEFAULT_DISCOVERY_INTERVAL_SECONDS = 7200
DEFAULT_BLOCK_BACKOFF_SECONDS = 10800
DEFAULT_READING_BACKOFF_SECONDS = 3600
# A denial from the WAF, never retried: more requests only prolong the block.
BLOCK_STATUSES = frozenset({403, 429})


class SourceStop(RuntimeError):
    """The pacer refused a request; nothing was sent to the source."""


class SourceBlocked(SourceStop):
    """The circuit breaker is open: the source answered 403/429 earlier."""

    def __init__(self, status: int | None = None) -> None:
        super().__init__("PROD6 source is blocked; request not sent")
        self.status = status


class SourceBudgetReached(SourceStop):
    """The cycle already used its request budget; the rest waits for the next cycle."""

    def __init__(self) -> None:
        super().__init__("PROD6 request budget of the cycle is spent; request not sent")


def _env_number(name: str, default: float, *, minimum: float, kind: type = float):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def min_request_interval() -> float:
    return _env_number("PROD6_MIN_REQUEST_INTERVAL_SECONDS", DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
                       minimum=0)


def default_max_source_requests() -> int:
    return _env_number("PROD6_MAX_SOURCE_REQUESTS", DEFAULT_MAX_SOURCE_REQUESTS, minimum=1, kind=int)


def default_discovery_interval_seconds() -> int:
    return _env_number("PROD6_DISCOVERY_INTERVAL_SECONDS", DEFAULT_DISCOVERY_INTERVAL_SECONDS,
                       minimum=0, kind=int)


def block_backoff_seconds() -> int:
    return _env_number("PROD6_BLOCK_BACKOFF_SECONDS", DEFAULT_BLOCK_BACKOFF_SECONDS, minimum=1, kind=int)


def reading_backoff_seconds() -> int:
    return _env_number("PROD6_READING_BACKOFF_SECONDS", DEFAULT_READING_BACKOFF_SECONDS, minimum=1, kind=int)


def blocking_status(error: BaseException) -> int | None:
    """HTTP status that must open the breaker (403/429), else None."""
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in BLOCK_STATUSES:
        return error.response.status_code
    return None


def source_stop(error: BaseException) -> str | None:
    """``"blocked"`` for a 403/429 or a refusal by the open breaker, ``"budget"`` when
    the cycle budget is spent, otherwise None."""
    if isinstance(error, SourceBlocked) or blocking_status(error) is not None:
        return "blocked"
    if isinstance(error, SourceBudgetReached):
        return "budget"
    return None


class SourcePacer:
    """Thread-safe fixed-gap pacer with an optional per-cycle budget and breaker.

    Each caller reserves the next free slot under the lock and sleeps outside it, so
    any number of threads still produce requests at least ``interval`` apart, in order.
    """

    def __init__(self, interval: float | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval = interval  # None: read the environment at each request
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._next_slot = float("-inf")
        self._guard = False
        self._max: int | None = None
        self._used = 0
        self._blocked: int | None = None
        self._is_blocked = False
        self._budget_reached = False

    @property
    def interval(self) -> float:
        return min_request_interval() if self._interval is None else self._interval

    def begin_cycle(self, max_requests: int | None, *, blocked_status: int | None = None,
                    blocked: bool = False) -> None:
        """Start a runtime cycle: reset the budget and enable the budget and breaker.

        ``blocked=True`` keeps the breaker open from the start (a block that is still
        running from an earlier cycle or process).
        """
        with self._lock:
            self._guard, self._max, self._used = True, max_requests, 0
            self._budget_reached = False
            self._is_blocked, self._blocked = blocked, blocked_status if blocked else None

    def trip(self, status: int | None = None) -> None:
        """Open the breaker; a no-op outside the runtime."""
        with self._lock:
            if self._guard:
                self._is_blocked = True
                self._blocked = status if self._blocked is None else self._blocked

    @property
    def blocked(self) -> bool:
        return self._guard and self._is_blocked

    @property
    def blocked_status(self) -> int | None:
        return self._blocked

    @property
    def budget_reached(self) -> bool:
        return self._budget_reached

    @property
    def used(self) -> int:
        return self._used

    @property
    def max_requests(self) -> int | None:
        return self._max

    @property
    def remaining(self) -> int | None:
        return None if self._max is None else max(0, self._max - self._used)

    def acquire(self) -> None:
        """Wait for this request's slot, or raise ``SourceStop`` without sending anything."""
        interval = self.interval
        with self._lock:
            if self._guard:
                if self._is_blocked:
                    raise SourceBlocked(self._blocked)
                if self._max is not None and self._used >= self._max:
                    self._budget_reached = True
                    raise SourceBudgetReached()
                self._used += 1
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + interval
        if slot > now:
            self._sleep(slot - now)
        with self._lock:
            if self._guard and self._is_blocked:
                # Another thread was denied while this one waited: do not send.
                self._used -= 1
                raise SourceBlocked(self._blocked)


PACER = SourcePacer()
