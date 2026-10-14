"""Staging boundary tests plus opt-in isolated production Redis contracts."""
import json
import os
from uuid import uuid4

import pytest

from buenapro_worker.prod2.staging import RedisStage, StagingError


class RecordingRedis:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def eval(self, script, count, *arguments):
        self.calls.append((script, count, arguments))
        return next(self.replies)


def test_operations_use_only_their_run_keys_and_keep_pending_protocol():
    client = RecordingRedis([1, 0, [b"2000", b'{"description":"software"}'], 1, 0, [0, 0, 0, 1], None])
    stage = RedisStage(client, "synthetic-run")
    assert stage.stage(2000, {"description": "software"}) is True
    assert stage.stage(2000, {"description": "different keyword observation"}) is False
    assert stage.next() == ("2000", {"description": "software"})
    assert stage.ack(2000) is True
    assert stage.recover() == 0
    assert stage.stats() == {"queued": 0, "pending": 0, "payloads": 0, "seen": 1}
    assert stage.next() is None
    assert len(stage.keys) == 4
    for script, count, arguments in client.calls:
        assert count == 4 and arguments[:4] == stage.keys
        assert all(key.startswith("buenapro:prod2:accepted:synthetic-run:") for key in arguments[:4])
        assert "TYPE" in script and "EXPIRE" not in script and "FLUSH" not in script
    assert json.loads(client.calls[0][2][-1]) == {"description": "software"}
    assert "LPUSH" in client.calls[2][0] and "LPOP" in client.calls[2][0]
    assert "HDEL" in client.calls[3][0] and "'done'" in client.calls[3][0]


@pytest.mark.parametrize("identifier", ["", None, True, -1, 0, "a b", "a/../b", "x" * 161])
def test_invalid_candidate_never_touches_redis(identifier):
    client = RecordingRedis([])
    with pytest.raises(StagingError):
        RedisStage(client, "synthetic-run").stage(identifier, {"description": "software"})
    assert client.calls == []


@pytest.mark.parametrize("payload", [[], None, {"score": float("nan")}, {"score": float("inf")},
                                     {"value": object()}, {"description": "x" * (2 * 1024 * 1024)}])
def test_invalid_or_oversized_payload_never_touches_redis(payload):
    client = RecordingRedis([])
    with pytest.raises(StagingError):
        RedisStage(client, "synthetic-run").stage(2000, payload)
    assert client.calls == []


@pytest.mark.parametrize("result", [[b"2000", b'not-json'], [b"2000", b'[]'],
                                    [b"2000", b'{"score":NaN}'], [b"\xff", b'{}'], [b"2000"]])
def test_corrupt_reserved_payload_is_not_acked_or_discarded(result):
    client = RecordingRedis([result])
    with pytest.raises(StagingError):
        RedisStage(client, "synthetic-run").next()
    assert len(client.calls) == 1 and "prod2-next-v1" in client.calls[0][0]


@pytest.fixture(scope="module")
def real_redis():
    if os.environ.get("PROD2_RUN_REDIS_INTEGRATION") != "1":
        pytest.skip("Opt-in isolated Redis integration; offline contracts run by default")
    from redis import Redis
    from buenapro_worker.prod2.runtime import production_staging_settings
    with production_staging_settings(ssh_host="ubuntu@168.129.177.199",
                                    ssh_key="credentials/ssh-key-2026-07-14.key") as (_, redis_url):
        client = Redis.from_url(redis_url, socket_connect_timeout=10, socket_timeout=10)
        try:
            assert client.ping() is True
            yield client
        finally:
            client.close()


@pytest.fixture
def real_stage(real_redis):
    stage = RedisStage(real_redis, "qa-" + uuid4().hex)
    try:
        yield stage
    finally:
        # These four generated QA keys are owned exclusively by this test.
        # Never scan/delete a shared namespace or flush the Redis database.
        real_redis.delete(*stage.keys)


def test_real_fifo_duplicate_payload_preserved_and_ack_retains_minimum_dedup(real_stage):
    stage = real_stage
    assert stage.stage("2000", {"description": "software", "origin": "first"})
    assert not stage.stage("2000", {"description": "software", "origin": "second"})
    assert stage.stage("2001", {"description": "cloud"})
    assert stage.stats() == {"queued": 2, "pending": 0, "payloads": 2, "seen": 2}
    assert stage.next() == ("2000", {"description": "software", "origin": "first"})
    assert stage.stats() == {"queued": 1, "pending": 1, "payloads": 2, "seen": 2}
    assert stage.ack("2000") and not stage.ack("2000")
    assert not stage.client.hexists(stage.keys[1], "2000")
    assert stage.client.hget(stage.keys[0], "2000") == b"done"
    assert not stage.stage("2000", {"description": "repeated after PostgreSQL commit"})
    assert stage.next() == ("2001", {"description": "cloud"})
    assert stage.ack("2001") and stage.next() is None
    assert stage.stats() == {"queued": 0, "pending": 0, "payloads": 0, "seen": 2}
    assert all(stage.client.ttl(key) == -1 for key in stage.keys if stage.client.exists(key))


def test_real_recover_restores_original_fifo_after_interrupted_executor(real_stage):
    stage = real_stage
    for identity in (2000, 2001, 2002):
        assert stage.stage(identity, {"id": identity})
    assert stage.next() == ("2000", {"id": 2000})
    assert stage.next() == ("2001", {"id": 2001})
    resumed = RedisStage(stage.client, stage.namespace.rsplit(":", 1)[1])
    assert resumed.recover() == 2 and resumed.recover() == 0
    assert resumed.stats() == {"queued": 3, "pending": 0, "payloads": 3, "seen": 3}
    for identity in (2000, 2001, 2002):
        assert resumed.next() == (str(identity), {"id": identity})
        assert resumed.ack(identity)
    assert resumed.stats() == {"queued": 0, "pending": 0, "payloads": 0, "seen": 3}


def test_real_ack_requires_pending_and_corruption_does_not_pop_queued_work(real_stage):
    from redis.exceptions import ResponseError
    stage = real_stage
    assert stage.stage(2000, {"description": "software"})
    with pytest.raises(ResponseError, match="requires a reservation"):
        stage.ack(2000)
    assert stage.stats() == {"queued": 1, "pending": 0, "payloads": 1, "seen": 1}
    stage.client.hdel(stage.keys[1], "2000")
    with pytest.raises(ResponseError, match="payload missing"):
        stage.next()
    assert stage.client.lrange(stage.keys[2], 0, -1) == [b"2000"]
    assert stage.client.llen(stage.keys[3]) == 0


def test_real_unexpected_key_type_fails_before_creating_dedup_or_queue(real_stage):
    from redis.exceptions import ResponseError
    stage = real_stage
    stage.client.set(stage.keys[1], "synthetic-wrong-type")
    with pytest.raises(ResponseError, match="key type mismatch"):
        stage.stage(2000, {"description": "software"})
    assert not stage.client.exists(stage.keys[0]) and not stage.client.exists(stage.keys[2])


def test_real_existing_ttl_is_cleared_for_pending_work_and_other_runs_are_untouched(real_stage):
    stage = real_stage
    assert stage.stage(2000, {"description": "software"})
    for key in stage.keys:
        if stage.client.exists(key):
            stage.client.expire(key, 60)
    assert not stage.stage(2000, {"description": "duplicate"})
    assert all(stage.client.ttl(key) == -1 for key in stage.keys if stage.client.exists(key))
    other = RedisStage(stage.client, "qa-" + uuid4().hex)
    try:
        assert other.stage(2000, {"description": "other run"})
        assert stage.next()[0] == "2000"
        assert stage.ack(2000)
        assert other.stats() == {"queued": 1, "pending": 0, "payloads": 1, "seen": 1}
    finally:
        stage.client.delete(*other.keys)
