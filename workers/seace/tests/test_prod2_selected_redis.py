"""Opt-in real Redis threads; only four generated QA keys are ever changed."""
from concurrent.futures import ThreadPoolExecutor
import threading

from test_prod2_staging import real_redis, real_stage


def test_multiple_keyword_producers_and_consumers_reserve_each_convocation_once(real_stage):
    stage = real_stage
    numbers = [number for _ in range(5) for number in range(10000, 10100)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        added = list(pool.map(lambda number: stage.stage(number, {"convocation_id": number}), numbers))
    assert sum(added) == 100 and stage.stats()["seen"] == 100
    completed, lock = [], threading.Lock()
    def consumer(_):
        while True:
            job = stage.next()
            if job is None:
                return
            identity, payload = job
            assert int(identity) == payload["convocation_id"]
            with lock:
                completed.append(identity)
            assert stage.ack(identity)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(consumer, range(8)))
    assert len(completed) == len(set(completed)) == 100
    assert stage.recover() == 0
    assert stage.stats() == {"queued": 0, "pending": 0, "payloads": 0, "seen": 100}


def test_new_bounded_runs_reuse_campaign_queue_and_advance_native_cached_pages(real_stage, tmp_path):
    from test_prod2_selected_script import subject
    from test_prod2_keyword_sweep import Factory, row
    from buenapro_worker.prod2.keyword_sweep import sweep_query
    from buenapro_worker.prod2.staging import RedisStage
    stage, factory = real_stage, Factory([row(index) for index in range(7)])
    query = {"keyword": "software", "object_type": "service", "year": 2026,
        "published_from": "2026-10-01", "published_to": "2026-10-05", "seace_version": "3"}
    reports = []
    for number in range(4):
        run = tmp_path / f"run{number}"
        run.mkdir()
        replayed = RedisStage(stage.client, stage.namespace.rsplit(":", 1)[1])
        replayed.recover()
        seen = subject.staged_this_run(run, stage.namespace, replayed.stats()["seen"], tracked=True)
        after_stage = subject.queue_checkpoint(tmp_path / "queue-history", "qa-queue", replayed.stats()["seen"])
        limits = subject.RunLimits(max_accepted=1, max_candidates=2, max_jev_calls=2, max_llm1_calls=1, seen=seen,
            after_stage=after_stage)
        reports.append(sweep_query(query, source_factory=factory,
            emit=lambda item: limits.stage(replayed, str(item["row"]["convocation_id"]), item),
            folder=tmp_path / "source-cache", stop_requested=limits.sweep_stopped))
        assert replayed.stats()["seen"] == min((number + 1) * 2, 7)
    assert not any(report["coverage_complete"] for report in reports[:3]) and reports[-1]["coverage_complete"]
    completed = []
    while job := stage.next():
        completed.append(job[0])
        assert stage.ack(job[0])
    assert len(completed) == len(set(completed)) == 7
    assert stage.stats() == {"queued": 0, "pending": 0, "payloads": 0, "seen": 7}
    stage.client.delete(*stage.keys)
    import pytest
    with pytest.raises(ValueError, match="history regressed"):
        subject.queue_checkpoint(tmp_path / "queue-history", "qa-queue", stage.stats()["seen"])
