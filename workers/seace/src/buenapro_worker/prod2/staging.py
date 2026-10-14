"""Recoverable, run-scoped Redis staging for the sequential PROD2 pilot.

Reservations remain in ``pending`` until PostgreSQL commits and the caller
acks them. No key expires; Redis persistence/eviction policy remains an
operational requirement. This is not a multi-process lease implementation.
"""
from __future__ import annotations

import json
import re
from typing import Any


class StagingError(ValueError):
    """Safe staging error without payload or connection credentials."""


_STAGE = """-- prod2-stage-v1
for _, key in ipairs(KEYS) do redis.call('PERSIST', key) end
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], 'queued')
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('RPUSH', KEYS[3], ARGV[1])
return 1
"""

_NEXT = """-- prod2-next-v1
local id = redis.call('LINDEX', KEYS[3], 0)
if not id then return false end
local payload = redis.call('HGET', KEYS[2], id)
if not payload then return redis.error_reply('PROD2 staging payload missing') end
if redis.call('HGET', KEYS[1], id) ~= 'queued' then
    return redis.error_reply('PROD2 staging queue state mismatch')
end
for _, key in ipairs(KEYS) do redis.call('PERSIST', key) end
redis.call('LPUSH', KEYS[4], id)
redis.call('LPOP', KEYS[3])
redis.call('HSET', KEYS[1], id, 'pending')
return {id, payload}
"""

_ACK = """-- prod2-ack-v1
local state = redis.call('HGET', KEYS[1], ARGV[1])
if state == 'done' or not state then return 0 end
if state ~= 'pending' then return redis.error_reply('PROD2 staging ack requires a reservation') end
if redis.call('LREM', KEYS[4], 1, ARGV[1]) ~= 1 then
    return redis.error_reply('PROD2 staging pending reservation missing')
end
for _, key in ipairs(KEYS) do redis.call('PERSIST', key) end
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[1], ARGV[1], 'done')
return 1
"""

_RECOVER = """-- prod2-recover-v1
local pending = redis.call('LRANGE', KEYS[4], 0, -1)
for _, id in ipairs(pending) do
    if redis.call('HGET', KEYS[1], id) ~= 'pending' or redis.call('HEXISTS', KEYS[2], id) ~= 1 then
        return redis.error_reply('PROD2 staging pending state mismatch')
    end
end
for _, key in ipairs(KEYS) do redis.call('PERSIST', key) end
-- pending is newest first; prepend each item to recover the original FIFO.
for _, id in ipairs(pending) do
    redis.call('LPUSH', KEYS[3], id)
    redis.call('HSET', KEYS[1], id, 'queued')
end
redis.call('DEL', KEYS[4])
return #pending
"""

_STATS = """-- prod2-stats-v1
return {redis.call('LLEN', KEYS[3]), redis.call('LLEN', KEYS[4]),
        redis.call('HLEN', KEYS[2]), redis.call('HLEN', KEYS[1])}
"""

_CHECK_TYPES = """
for index, key in ipairs(KEYS) do
    local expected = index <= 2 and 'hash' or 'list'
    local actual = redis.call('TYPE', key).ok
    if actual ~= 'none' and actual ~= expected then
        return redis.error_reply('PROD2 staging key type mismatch')
    end
end
"""


def _identifier(value: Any, *, limit: int) -> str:
    if type(value) is int and value > 0:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0," + str(limit - 1) + r"}", value):
        raise StagingError("Expected a bounded staging identifier")
    return value


def _stage_arguments(candidate_id: str | int, payload: dict) -> tuple[str, str]:
    identity = _identifier(candidate_id, limit=160)
    if not isinstance(payload, dict):
        raise StagingError("Expected a structured staging payload")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise StagingError("Expected a finite JSON staging payload") from None
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise StagingError("Staging payload exceeds bounded size")
    return identity, encoded


def _stage_replies(replies, expected: int) -> list[bool]:
    if (not isinstance(replies, (list, tuple)) or len(replies) != expected
            or any(type(value) is not int or value not in (0, 1) for value in replies)):
        raise StagingError("Unexpected staging batch response")
    return [bool(value) for value in replies]


def _command_bytes(arguments: tuple[str, ...]) -> int:
    """RESP framing and UTF-8 bytes, including the repeated EVAL script/keys."""
    total = len(f"*{len(arguments)}\r\n")
    for value in arguments:
        size = len(value.encode("utf-8"))
        total += len(f"${size}\r\n") + size + 2
    return total


class RedisStage:
    def __init__(self, client: Any, run_id: str):
        self.client = client
        self.namespace = "buenapro:prod2:accepted:" + _identifier(run_id, limit=120)
        self.keys = tuple(self.namespace + suffix for suffix in (":dedup", ":payloads", ":queue", ":pending"))

    def _eval(self, script: str, *args):
        return self.client.eval(_CHECK_TYPES + script, len(self.keys), *self.keys, *args)

    def stage(self, candidate_id: str | int, payload: dict) -> bool:
        """Queue the first payload for this ID; duplicate observations do not replace it."""
        identity, encoded = _stage_arguments(candidate_id, payload)
        return bool(self._eval(_STAGE, identity, encoded))

    def stage_many(self, entries: list[tuple[str | int, dict]]) -> list[bool]:
        """Send the existing per-row EVALs in order, without changing staging Lua.

        All inputs are checked before Redis. Pipelines contain at most 100 rows
        and 1 MiB of commands; a valid larger individual payload stays serial.
        A failed response may leave an applied prefix, recoverable by dedup.
        No result vector is returned unless every command has a valid reply.
        """
        if not isinstance(entries, list):
            raise StagingError("Expected a list of staging entries")
        prepared = []
        for entry in entries:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise StagingError("Expected a staging identifier and payload")
            identity, encoded = _stage_arguments(*entry)
            size = _command_bytes(("EVAL", _CHECK_TYPES + _STAGE, str(len(self.keys)),
                                   *self.keys, identity, encoded))
            prepared.append((identity, encoded, size))
        results, batch = [], []
        batch_bytes = 0

        def flush():
            if not batch:
                return
            with self.client.pipeline(transaction=False) as pipeline:
                for identity, encoded, _ in batch:
                    pipeline.eval(_CHECK_TYPES + _STAGE, len(self.keys), *self.keys, identity, encoded)
                replies = pipeline.execute()
            results.extend(_stage_replies(replies, len(batch)))
            batch.clear()

        for identity, encoded, size in prepared:
            if size > 1024 * 1024:
                flush()
                batch_bytes = 0
                results.extend(_stage_replies([self._eval(_STAGE, identity, encoded)], 1))
                continue
            if len(batch) >= 100 or batch_bytes + size > 1024 * 1024:
                flush()
                batch_bytes = 0
            batch.append((identity, encoded, size))
            batch_bytes += size
        flush()
        return results

    def next(self) -> tuple[str, dict] | None:
        """Atomically reserve a candidate, leaving it recoverable until ack()."""
        result = self._eval(_NEXT)
        if result is None or result is False:
            return None
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise StagingError("Unexpected staging reservation response")
        identity, encoded = result
        try:
            identity = identity.decode("utf-8") if isinstance(identity, bytes) else identity
            def invalid_constant(value):
                raise ValueError()
            payload = json.loads(encoded, parse_constant=invalid_constant)
        except (UnicodeDecodeError, TypeError, ValueError):
            raise StagingError("Invalid staged JSON reservation; pending was retained") from None
        identity = _identifier(identity, limit=160)
        if not isinstance(payload, dict):
            raise StagingError("Invalid staged payload; pending was retained")
        return identity, payload

    def ack(self, candidate_id: str | int) -> bool:
        """Call after the durable commit. Delete payload, retaining only run dedup."""
        return bool(self._eval(_ACK, _identifier(candidate_id, limit=160)))

    def recover(self) -> int:
        """Requeue pending candidates before unreserved work; requires one executor."""
        return int(self._eval(_RECOVER))

    def stats(self) -> dict[str, int]:
        result = self._eval(_STATS)
        if not isinstance(result, (list, tuple)) or len(result) != 4:
            raise StagingError("Unexpected staging statistics response")
        try:
            values = list(map(int, result))
            if (any(value < 0 for value in values) or values[2] != values[0] + values[1]
                    or values[3] < values[2]):
                raise ValueError()
        except (ValueError, TypeError):
            raise StagingError("Invalid staging statistics response") from None
        return dict(zip(("queued", "pending", "payloads", "seen"), values))
