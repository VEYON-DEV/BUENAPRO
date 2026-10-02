from __future__ import annotations

import time
from datetime import datetime, timezone

from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


def enqueue_scheduled_jobs(settings: Settings, repo: JobRepository, *, anio: int | None = None) -> dict[str, int | None]:
    year = anio or datetime.now(timezone.utc).year
    poll_jobs = enqueue_poll_search_jobs(settings, repo, year=year)
    if settings.schedule_refresh_enabled:
        poll_jobs["refresh_schedules"] = repo.enqueue(
            "refresh_schedules", {}, queue_name="io", dedup_key="refresh_schedules", priority=4,
        )
    if settings.prod4_enabled:
        poll_jobs["poll_prod4"] = repo.enqueue(
            "poll_prod4", {}, queue_name="io", dedup_key="poll_prod4", priority=2,
        )
    return poll_jobs | {
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


def enqueue_poll_search_jobs(settings: Settings, repo: JobRepository, *, year: int) -> dict[str, int | None]:
    jobs: dict[str, int | None] = {}
    if settings.prod6_profile_poll_enabled:
        jobs["poll_prod6_profiles"] = repo.enqueue(
            "poll_prod6_profiles", {"anio": year}, queue_name="io",
            dedup_key=f"poll_prod6_profiles:{year}", priority=1,
        )
    for objeto in settings.allowed_codigo_objeto:
        for segment in settings.allowed_segments:
            key = f"poll_search:{year}:{objeto}:{segment}"
            jobs[key] = repo.enqueue(
                "poll_search",
                {"anio": year, "objects": [objeto], "segments": [segment]},
                queue_name="io",
                dedup_key=key,
                priority=1,
            )
    return jobs


def run_scheduler_forever(settings: Settings, repo_factory, *, anio: int | None = None) -> None:
    last_poll = 0.0
    last_lifecycle = 0.0
    last_recent_closures = 0.0
    last_contract_test = 0.0
    last_prod4 = 0.0
    last_schedule_refresh: float | None = None

    while True:
        now = time.monotonic()
        year = anio or datetime.now(timezone.utc).year

        with repo_factory() as repo:
            if settings.schedule_refresh_enabled and (
                last_schedule_refresh is None
                or now - last_schedule_refresh >= settings.schedule_refresh_interval_seconds
            ):
                # The stable key and uq_jobs_dedup index allow only one pending
                # or claimed sweep, even across overlapping scheduler instances.
                repo.enqueue("refresh_schedules", {}, queue_name="io", dedup_key="refresh_schedules", priority=4)
                last_schedule_refresh = now
            if now - last_poll >= settings.seace_poll_interval_minutes * 60:
                enqueue_poll_search_jobs(settings, repo, year=year)
                last_poll = now

            if settings.prod4_enabled and now - last_prod4 >= settings.prod4_poll_interval_minutes * 60:
                repo.enqueue("poll_prod4", {}, queue_name="io", dedup_key="poll_prod4", priority=2)
                last_prod4 = now

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
