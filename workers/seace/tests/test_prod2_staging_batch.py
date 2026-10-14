"""Batch transport contracts, without production I/O unless explicitly enabled."""
import json

import pytest
from redis.connection import Connection

from buenapro_worker.prod2.staging import RedisStage, StagingError, _CHECK_TYPES, _STAGE
from test_prod2_staging import real_redis, real_stage


class MemoryRedis:
    def __init__(self):
        self.states, self.payloads, self.queue = {}, {}, []
        self.events, self.pipelines = [], []
        self.override = None
        self.fail_after = None
        self.applied = 0

    def apply(self, command):
        script, count, *arguments = command
        assert script == _CHECK_TYPES + _STAGE and count == 4
        identity, payload = arguments[-2:]
        self.applied += 1
        if identity in self.states:
            return 0
        self.states[identity] = "queued"
        self.payloads[identity] = payload
        self.queue.append(identity)
        return 1

    def eval(self, *command):
        self.events.append(("serial", [command]))
        reply = self.apply(command)
        return reply if self.override is None else self.override[0]

    def pipeline(self, *, transaction):
        assert transaction is False
        owner = self

        class Pipeline:
            def __init__(self):
                self.commands = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def eval(self, *command):
                self.commands.append(command)
                return self

            def execute(self):
                owner.events.append(("pipeline", self.commands))
                replies = []
                for command in self.commands:
                    replies.append(owner.apply(command))
                    if owner.fail_after == owner.applied:
                        owner.fail_after = None
                        raise ConnectionError("synthetic lost response")
                return replies if owner.override is None else owner.override

        pipeline = Pipeline()
        self.pipelines.append(pipeline)
        return pipeline


def entry(number, **payload):
    return number, {"id": number, **payload}


def test_batch_matches_serial_fifo_original_payload_and_queued_pending_done_dedup():
    serial, batch = MemoryRedis(), MemoryRedis()
    for client in (serial, batch):
        client.states.update({"1": "queued", "2": "pending", "3": "done"})
        client.payloads.update({"1": '{"origin":"first"}', "2": '{"origin":"pending"}'})
        client.queue.append("1")
    entries = [entry(1), entry(2), entry(3), entry(4, origin="first"),
               entry(4, origin="second"), entry(5)]
    serial_stage, batch_stage = RedisStage(serial, "synthetic"), RedisStage(batch, "synthetic")
    expected = [serial_stage.stage(*value) for value in entries]
    assert batch_stage.stage_many(entries) == expected == [False, False, False, True, False, True]
    assert (batch.states, batch.payloads, batch.queue) == (serial.states, serial.payloads, serial.queue)
    assert json.loads(batch.payloads["4"])["origin"] == "first"
    assert batch.payloads["2"] == '{"origin":"pending"}' and "3" not in batch.payloads
    assert batch.queue == ["1", "4", "5"]


def test_empty_batch_never_opens_redis_pipeline():
    client = MemoryRedis()
    assert RedisStage(client, "synthetic").stage_many([]) == []
    assert client.events == client.pipelines == []


def test_pipeline_count_bound_keeps_exact_scripts_keys_and_order():
    client, entries = MemoryRedis(), [entry(number) for number in range(1, 252)]
    stage = RedisStage(client, "synthetic")
    assert stage.stage_many(entries) == [True] * 251
    assert [len(commands) for _, commands in client.events] == [100, 100, 51]
    commands = [command for _, group in client.events for command in group]
    assert [command[-2] for command in commands] == [str(number) for number in range(1, 252)]
    assert all(command[2:6] == stage.keys for command in commands)
    assert all(command[0] == _CHECK_TYPES + _STAGE for command in commands)


def test_pipeline_byte_bound_counts_utf8_and_actual_resp_framing():
    client = MemoryRedis()
    entries = [entry(number, description="á" * 180000) for number in range(1, 8)]
    assert RedisStage(client, "synthetic").stage_many(entries) == [True] * 7
    wire = Connection()
    assert len(client.events) == 4
    for kind, commands in client.events:
        assert kind == "pipeline"
        packed_bytes = sum(sum(map(len, wire.pack_command("EVAL", *command))) for command in commands)
        assert packed_bytes <= 1024 * 1024


def test_valid_large_individual_payload_uses_serial_fallback_in_order():
    client = MemoryRedis()
    entries = [entry(1), entry(2, description="x" * (1024 * 1024 + 128)), entry(3)]
    assert RedisStage(client, "synthetic").stage_many(entries) == [True] * 3
    assert [kind for kind, _ in client.events] == ["pipeline", "serial", "pipeline"]
    assert client.queue == ["1", "2", "3"]


@pytest.mark.parametrize("bad", [
    (None, {}), (True, {}), ("bad/id", {}), (2, []), (2, {"value": float("nan")}),
    (2, {"value": object()}), (2, {"description": "x" * (2 * 1024 * 1024)}), (2,), "bad",
])
def test_invalid_later_input_prevents_every_redis_command(bad):
    client = MemoryRedis()
    with pytest.raises(StagingError):
        RedisStage(client, "synthetic").stage_many([entry(1), bad])
    assert client.events == client.pipelines == []


@pytest.mark.parametrize("reply", [[1], [1, 0, 1], [1, True], [1, 2], [1, -1],
                                   [1, "0"], [1, 0.0], [1, None], "invalid"])
def test_invalid_pipeline_replies_fail_without_returning_a_result_vector(reply):
    client = MemoryRedis()
    client.override = reply
    with pytest.raises(StagingError, match="batch response"):
        RedisStage(client, "synthetic").stage_many([entry(1), entry(2)])


def test_large_serial_fallback_also_requires_strict_integer_reply():
    client = MemoryRedis()
    client.override = [True]
    with pytest.raises(StagingError, match="batch response"):
        RedisStage(client, "synthetic").stage_many([entry(1, description="x" * (1024 * 1024))])


def test_lost_reply_after_applied_prefix_resumes_without_duplicate_or_payload_replacement():
    client = MemoryRedis()
    client.fail_after = 37
    stage = RedisStage(client, "synthetic")
    entries = [entry(number, origin="first") for number in range(1, 103)]
    with pytest.raises(ConnectionError):
        stage.stage_many(entries)
    assert client.queue == [str(number) for number in range(1, 38)]
    replay = [entry(number, origin="later") for number in range(1, 103)]
    assert stage.stage_many(replay) == [False] * 37 + [True] * 65
    assert client.queue == [str(number) for number in range(1, 103)]
    assert all(json.loads(client.payloads[str(number)])["origin"] == "first" for number in range(1, 38))


def test_real_batch_preserves_fifo_pending_done_and_first_payload(real_stage):
    stage = real_stage
    assert stage.stage_many([entry(2000, origin="first"), entry(2001, origin="first"),
                             entry(2000, origin="duplicate"), entry(2002)]) == [True, True, False, True]
    assert stage.next() == ("2000", {"id": 2000, "origin": "first"})
    assert stage.ack(2000)
    assert stage.next() == ("2001", {"id": 2001, "origin": "first"})
    assert stage.stage_many([entry(2000), entry(2001), entry(2002),
                             entry(2003, origin="first"), entry(2003, origin="duplicate")]) == [False, False, False, True, False]
    assert stage.stats() == {"queued": 2, "pending": 1, "payloads": 3, "seen": 4}
    assert not stage.client.hexists(stage.keys[1], "2000")
    assert stage.client.hget(stage.keys[0], "2000") == b"done"
    assert stage.recover() == 1
    for identity, payload in [("2001", {"id": 2001, "origin": "first"}),
                              ("2002", {"id": 2002}), ("2003", {"id": 2003, "origin": "first"})]:
        assert stage.next() == (identity, payload)
        assert stage.ack(identity)
    assert stage.stats() == {"queued": 0, "pending": 0, "payloads": 0, "seen": 4}
    assert all(stage.client.ttl(key) == -1 for key in stage.keys if stage.client.exists(key))
