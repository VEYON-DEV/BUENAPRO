"""Batch control keeps limits, shared ownership and durable checkpoints explicit."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from buenapro_worker.prod2.selected_control import RunLimits


def limits(**extra):
    return RunLimits(**({"max_accepted": None, "max_candidates": None,
                        "max_jev_calls": None, "max_llm1_calls": None} | extra))


class Stage:
    def __init__(self, *, seen=(), origin=0):
        self.seen, self.origin = set(seen), origin
        self.serial_calls, self.batch_calls, self.stats_calls = [], [], 0

    def stage(self, identity, payload):
        self.serial_calls.append(identity)
        new = identity not in self.seen
        self.seen.add(identity)
        return new

    def stage_many(self, entries):
        self.batch_calls.append(entries)
        return [self.stage(*entry) for entry in entries]

    def stats(self):
        self.stats_calls += 1
        return {"seen": self.origin + len(self.seen)}


def test_unlimited_batch_counts_only_new_ids_and_checkpoints_absolute_high_water_once():
    checkpoints = []
    control = limits(seen=5, after_stage=checkpoints.append)
    stage = Stage(seen=("old",), origin=900)
    entries = [("old", {}), ("new", {}), ("new", {}), ("other", {})]
    assert control.stage_many(stage, entries) == [False, True, False, True]
    assert control.seen == 7 and checkpoints == [903] and stage.stats_calls == 1
    assert control.calls == {"jev": 0, "llm1": 0} and control.reserved == {"jev": 0, "llm1": 0}


def test_all_duplicates_and_empty_batch_need_no_checkpoint_or_stats():
    checkpoints = []
    control, stage = limits(after_stage=checkpoints.append), Stage(seen=("old",))
    assert control.stage_many(stage, [("old", {})]) == [False]
    assert control.stage_many(stage, []) == []
    assert control.seen == 0 and stage.stats_calls == 0 and checkpoints == []


def test_bounded_batch_retains_serial_caps_unique_count_and_per_row_checkpoints():
    checkpoints = []
    control, stage = limits(max_candidates=2, after_stage=checkpoints.append), Stage()
    assert control.stage_many(stage, [("a", {}), ("a", {}), ("b", {}), ("c", {})]) == [True, False, True, False]
    assert control.seen == 2 and control.sweep_stopped()
    assert stage.serial_calls == ["a", "a", "b"] and stage.batch_calls == []
    assert checkpoints == [1, 2]


@pytest.mark.parametrize("results", [[True], [True, False, True], [1, 0], [True, None], (True, False)])
def test_invalid_batch_vector_never_confirms_seen_or_checkpoint(results):
    checkpoints = []
    control = limits(seen=9, after_stage=checkpoints.append)
    stage = Stage()
    stage.stage_many = lambda entries: results
    with pytest.raises(ValueError, match="batch result vector"):
        control.stage_many(stage, [("a", {}), ("b", {})])
    assert control.seen == 9 and checkpoints == [] and stage.stats_calls == 0


def test_transport_failure_with_applied_prefix_never_confirms_seen_or_checkpoint():
    checkpoints = []
    control, stage = limits(seen=9, after_stage=checkpoints.append), Stage()
    def fail(entries):
        stage.seen.add(entries[0][0])
        raise ConnectionError("synthetic lost response")
    stage.stage_many = fail
    with pytest.raises(ConnectionError):
        control.stage_many(stage, [("a", {}), ("b", {})])
    assert stage.seen == {"a"} and control.seen == 9 and checkpoints == [] and stage.stats_calls == 0


def test_parallel_batches_hold_shared_lock_through_redis_stats_and_checkpoint():
    control = limits()
    state_lock = threading.Lock()
    active, maximum, checkpoints = 0, 0, []

    class ConcurrentStage(Stage):
        def stage_many(self, entries):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.001)
            result = super().stage_many(entries)
            with state_lock:
                active -= 1
            return result

        def stats(self):
            assert control.lock._is_owned()
            return super().stats()

    def checkpoint(seen):
        assert control.lock._is_owned()
        checkpoints.append(seen)

    control.after_stage = checkpoint
    stage = ConcurrentStage()
    entries = [(str(number), {}) for number in range(10)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: control.stage_many(stage, entries), range(8)))
    assert maximum == 1 and sum(sum(result) for result in results) == control.seen == 10
    assert checkpoints == [10] and stage.stats_calls == 1
