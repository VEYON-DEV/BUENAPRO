"""Queue the automatic evaluation (LLM2) of contracts JEV accepted before it existed.

``python -m buenapro_worker.prod6.backfill_evaluations --profile-id A --profile-id B``

Dry-run by default: lists what would be queued and why the rest is left out. ``--apply`` queues
``analyze_match`` jobs (cola ``llm``) for the PROD6 coordinator, and nothing else: no request is
made here to SEACE, JEV or Gemini; the paid calls happen later, one at a time, in the runtime.

The selection is the one the runtime applies after each ``derive_summary``
(``prod6/evaluation.py``): the company's current JEV decision for the contract is ``accepted``;
the contract is open; the reading is current, valid and not under review; the company's
``automation_rules`` allow it (enabled, ``max_daily_evaluations``, ``min_hours_before_close``);
and no match exists yet (``--include-stale`` also re-evaluates a match made from an older
reading of the document).

The first ``--profile-id`` is the queue scope and must be the one the runtime starts with,
in the same order, or the runtime would not claim these jobs.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from buenapro_worker.prod6.evaluation import enqueue_evaluations
from buenapro_worker.prod6.runtime import ScopedRepository


def backfill(conn, *, profile_ids: list[str], apply: bool = False, include_stale: bool = False,
             limit: int | None = None, batch_id: str | None = None) -> dict:
    profile_ids = list(dict.fromkeys(profile_ids))
    if not profile_ids:
        raise ValueError("At least one profile is required")
    if limit is not None and limit < 1:
        raise ValueError("The limit must be positive")
    batch = batch_id or f"prod6-backfill-evaluations-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    repo = ScopedRepository(conn, profile_id=profile_ids[0], batch_id=batch, profile_ids=profile_ids)
    stats = enqueue_evaluations(repo, profile_ids=profile_ids, only_accepted=True,
                                include_stale=include_stale, apply=apply, limit=limit)
    pairs = stats["planned"]
    return {"mode": "apply" if apply else "dry-run", "batch_id": batch, "profile_ids": profile_ids,
            **{key: stats[key] for key in ("candidates", "enqueued", "already_queued", "daily_limited",
                                           "skipped")},
            "would_enqueue": 0 if apply else len(pairs),
            "contracts": len({item["id_contrato"] for item in pairs}),
            "by_profile": {profile: sum(item["profile_id"] == profile for item in pairs)
                           for profile in profile_ids} if not apply else {},
            "planned": pairs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile-id", action="append", dest="profile_ids", required=True,
                        help="Repeat for each company, in the runtime's order (the first scopes the queue)")
    parser.add_argument("--apply", action="store_true", help="Queue the jobs (default: dry-run, writes nothing)")
    parser.add_argument("--include-stale", action="store_true",
                        help="Also re-evaluate matches made from an older reading of the document")
    parser.add_argument("--limit", type=int, help="Queue at most this many pairs, soonest closing first")
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL is not set")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        conn.execute("SET TIME ZONE 'UTC'")  # The daily limit counts UTC days, as in the runtime.
        result = backfill(conn, profile_ids=args.profile_ids, apply=args.apply,
                          include_stale=args.include_stale, limit=args.limit)
        # One transaction: the dry-run writes nothing and an apply is all or nothing.
        conn.commit() if args.apply else conn.rollback()
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
