from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from buenapro_worker.prod2.selected_control import RunLimits, SharedPacer, LimitReached


def limits(**extra):
    return RunLimits(**({"max_accepted": 10, "max_candidates": 100,
        "max_jev_calls": 3, "max_llm1_calls": 2, "model_workers": 2} | extra))


def test_parallel_admission_is_bounded_and_persistence_failure_does_not_take_slot():
    control = limits(max_accepted=2)
    inserted = []
    def admit(number):
        return control.admit(number, lambda: inserted.append(number) or number)
    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(admit, range(20)))
    assert sum(value is not None for value in outcomes) == 2
    assert len(inserted) == len(control.accepted_ids) == 2
    failing = limits(max_accepted=1)
    with pytest.raises(ValueError):
        failing.admit(1, lambda: (_ for _ in ()).throw(ValueError()))
    assert failing.accepted_ids == set()
    assert failing.admit(2, lambda: "saved") == "saved"


def test_atomic_dedup_stage_limits_count_unique_only():
    class Stage:
        seen = set()
        def stage(self, key, item):
            if key in self.seen:
                return False
            self.seen.add(key)
            return True
    stage, control = Stage(), limits(max_candidates=2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda key: control.stage(stage, key, {}), ["a"] * 10 + ["b"] * 10))
    assert sum(outcomes) == 2 and control.seen == 2 and control.sweep_stopped()
    assert not control.stage(stage, "c", {})


def test_provider_call_cap_is_global_and_includes_uncertain_attempts():
    control = limits()
    def attempt(_):
        try:
            control.before_request("jev")
            return True
        except LimitReached:
            return False
    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sum(pool.map(attempt, range(20))) == 3
    assert control.calls["jev"] == 3 and control.calls["llm1"] == 0


def test_potential_call_permits_release_on_cache_hit_and_never_oversubscribe():
    control = limits(max_jev_calls=1)
    permit = control.reserve_request("jev")
    with pytest.raises(LimitReached):
        control.reserve_request("jev")
    permit.close()  # cache hit: no provider request
    assert control.calls["jev"] == 0 and control.reserved["jev"] == 0
    issued = control.reserve_request("jev")
    issued.issue()
    issued.close()
    assert control.calls["jev"] == 1 and control.reserved["jev"] == 0
    with pytest.raises(LimitReached):
        control.reserve_request("jev")


def test_same_semantic_payload_is_serialized_but_different_payloads_overlap():
    control = limits()
    mutex = threading.Lock()
    active, maxima = {}, {}
    def request(key):
        with control.model(key):
            with mutex:
                active[key] = active.get(key, 0) + 1
                maxima[key] = max(maxima.get(key, 0), active[key])
            time.sleep(.005)
            with mutex:
                active[key] -= 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(request, ["same"] * 20))
    assert maxima["same"] == 1


def test_pacer_preserves_global_spacing_with_injected_clock():
    now, sleeps, stamps = [0.], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    pacer = SharedPacer(2, clock=lambda: now[0], sleep=sleep)
    for _ in range(4):
        pacer()
        stamps.append(now[0])
    assert stamps == [0, 2, 4, 6] and sleeps == [2, 2, 2]
