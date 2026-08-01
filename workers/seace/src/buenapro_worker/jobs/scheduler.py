from __future__ import annotations

import time
from datetime import datetime, timezone

from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


def enqueue_scheduled_jobs(settings: Settings, repo: JobRepository, *, anio: int | None = None) -> dict[str, int | None]:
    year = anio or datetime.now(timezone.utc).year
    return {
        "poll_search": repo.enqueue(
            "poll_search",
            {"anio": year},
            queue_name="io",
            dedup_key=f"poll_search:{year}",
            priority=1,
        ),
        "poll_lifecycle": repo.enqueue(
            "poll_lifecycle",
            {"anio": year},
            queue_name="io",
            dedup_key=f"poll_lifecycle:{year}",
            priority=4,
        ),
        "recent_closures": repo.enqueue(
            "recent_closures",
            {"days": settings.seace_recent_closures_days},
            queue_name="io",
            dedup_key="recent_closures",
            priority=4,
        ),
        "contract_test": repo.enqueue(
            "contract_test",
            {"anio": year},
            queue_name="io",
            dedup_key=f"contract_test:{year}",
            priority=3,
        ),
    }


def run_scheduler_forever(settings: Settings, repo_factory, *, anio: int | None = None) -> None:
    last_poll = 0.0
    last_lifecycle = 0.0
    last_recent_closures = 0.0
    last_contract_test = 0.0

    while True:
        now = time.monotonic()
        year = anio or datetime.now(timezone.utc).year

        with repo_factory() as repo:
            if now - last_poll >= settings.seace_poll_interval_minutes * 60:
                repo.enqueue(
                    "poll_search",
                    {"anio": year},
                    queue_name="io",
                    dedup_key=f"poll_search:{year}",
                    priority=1,
                )
                last_poll = now

            if now - last_lifecycle >= settings.seace_lifecycle_interval_hours * 3600:
                repo.enqueue(
                    "poll_lifecycle",
                    {"anio": year},
                    queue_name="io",
                    dedup_key=f"poll_lifecycle:{year}",
                    priority=4,
                )
                last_lifecycle = now

            if now - last_recent_closures >= settings.seace_recent_closures_interval_hours * 3600:
                repo.enqueue(
                    "recent_closures",
                    {"days": settings.seace_recent_closures_days},
                    queue_name="io",
                    dedup_key="recent_closures",
                    priority=4,
                )
                last_recent_closures = now

            if now - last_contract_test >= settings.seace_contract_test_interval_minutes * 60:
                repo.enqueue(
                    "contract_test",
                    {"anio": year},
                    queue_name="io",
                    dedup_key=f"contract_test:{year}",
                    priority=3,
                )
                last_contract_test = now

        time.sleep(30)
