"""Planning of the national daily listing: PROD2 discovery by publication day, without a keyword.

One query per publication day and object (good, service), each with an empty keyword and the
day's year. Long windows without a keyword fail because SEACE changes the first page while the
pass paginates, so a day is the unit. The policy is frozen into the run manifest only when this
mode is used; keyword runs keep their manifests, hashes and identities. Pure: no IO.
"""
from __future__ import annotations

from datetime import date, timedelta

POLICY = "prod2_national_listing_v1"
OBJECTS = ("good", "service")
MAX_DAYS = 31
# The current day keeps receiving publications while it is paginated, so its first page or counter can change
# under the pass. Those two codes (and only those) are retried and, if they persist, tolerated for that day.
CURRENT_DAY_CODES = frozenset({"first_page_reconciliation_changed", "counter_changed_during_pass"})
CURRENT_DAY_RETRIES = 2
CURRENT_DAY_PAUSE_SECONDS = 60


def listing_days(published_from: str, published_to: str) -> list[date]:
    """Every publication day of the inclusive window, oldest first, within ``MAX_DAYS``."""
    first, last = date.fromisoformat(published_from), date.fromisoformat(published_to)
    if first > last:
        raise ValueError("National listing window is reversed")
    count = (last - first).days + 1
    if count > MAX_DAYS:
        raise ValueError(f"National listing window exceeds {MAX_DAYS} days")
    return [first + timedelta(days=offset) for offset in range(count)]


def listing_queries(published_from: str, published_to: str, objects=OBJECTS, *, resolved_years=None) -> list[dict]:
    """One empty-keyword query per day and object; ``resolved_years`` (if given) must contain each day's year."""
    objects = list(objects)
    if not objects or len(set(objects)) != len(objects) or any(value not in OBJECTS for value in objects):
        raise ValueError("National listing covers goods and services")
    queries = []
    for day in listing_days(published_from, published_to):
        if resolved_years is not None and day.year not in resolved_years:
            raise ValueError("National listing day belongs to a year absent from the resolved years")
        queries.extend({"keyword": "", "object_type": object_type, "year": day.year,
                        "published_from": day.isoformat(), "published_to": day.isoformat(),
                        "seace_version": "3"} for object_type in objects)
    return queries


def is_current_day(query: dict, published_to: str, today: date) -> bool:
    """The last day of the cycle's window, while it is today in Lima (or yesterday if the cycle crossed midnight)."""
    day = date.fromisoformat(query["published_to"])
    return (query["published_from"] == query["published_to"] == published_to
            and timedelta(0) <= today - day <= timedelta(days=1))


def current_day_unstable(result: dict, query: dict, published_to: str, today: date) -> bool:
    """A swept day left partial only by the pagination changing under it, on the current day."""
    errors = result.get("errors")
    return (result.get("status") == "partial" and isinstance(errors, list) and bool(errors)
            and all(isinstance(error, dict) and error.get("code") in CURRENT_DAY_CODES for error in errors)
            and is_current_day(query, published_to, today))
