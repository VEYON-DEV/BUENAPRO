"""Evaluate open PROD2 readings (licitaciones mayores) for VEYON and DIN (LLM2).

``--plan`` reads PostgreSQL only and lists the pairs a run would evaluate: no reservation, no
receipt, no provider call. ``--execute`` evaluates them in order, at most ``--max-calls``, with
durable receipts in ``--receipts`` (a private directory kept between runs: an open guard there
blocks every later send until an operator reviews it). Selection, limits and payment rules are in
``buenapro_worker.prod2.evaluation``.

Exit codes: 0 done (or nothing to do), 2 a payment doubt or an open guard stopped the run,
3 the provider rejected a request (nothing billed; the next run retries).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

import psycopg
from psycopg.rows import dict_row

from buenapro_worker.settings import Settings
from buenapro_worker.prod2 import evaluation

COMPANIES = {evaluation.VEYON: "VEYON", evaluation.DIN: "DIN"}


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def parse_ids(values):
    if not values:
        return None
    ids = [int(part) for value in values for part in str(value).split(",") if part.strip()]
    if not ids or any(item <= 0 for item in ids):
        raise SystemExit("--ids must be positive convocation ids")
    return sorted(set(ids))


def private_dir(path: Path) -> Path:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SystemExit("--receipts must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise SystemExit("--receipts must be private (700)")
    return path


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="PostgreSQL only: list the pairs to evaluate")
    mode.add_argument("--execute", action="store_true", help="Evaluate the planned pairs (paid)")
    p.add_argument("--max-calls", type=int, required=True, help="Ceiling of evaluations in this run (1-200)")
    p.add_argument("--receipts", type=Path, help="Private receipt directory, kept between runs (--execute)")
    p.add_argument("--ids", nargs="+", help="Only these convocation ids (spaces or commas)")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not 1 <= args.max_calls <= 200:
        raise SystemExit("--max-calls must be between 1 and 200")
    if args.execute and args.receipts is None:
        raise SystemExit("--execute requires --receipts")
    ids = parse_ids(args.ids)
    settings = Settings(_env_file=ROOT / ".env.local")
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        if args.plan:
            planned = evaluation.plan(conn, convocations=ids, max_calls=args.max_calls)
            for row in planned["planned"]:
                emit({"status": "planned", "id_convocatoria": row["id_convocatoria"],
                      "company": COMPANIES[row["profile_id"]], "jev_domain": row["domain"],
                      "hours_left": round(float(row["hours_left"]), 1),
                      "reading_review": row["requires_human_review"], "facets": row["facet_count"]})
            emit({"status": "plan", "planned": len(planned["planned"]), "skipped": planned["skipped"],
                  "provider_calls": 0})
            return 0
        report = evaluation.run(conn, settings, private_dir(args.receipts), max_calls=args.max_calls,
                                convocations=ids)
    for item in report["outcomes"]:
        emit({**item, "company": COMPANIES[item["profile_id"]]})
    emit({key: report[key] for key in ("policy_version", "planned", "skipped", "stopped")}
         | {"evaluated": sum(item["outcome"] == "evaluated" for item in report["outcomes"]),
            "cost_usd": round(report["cost_usd"], 6)})
    if report["stopped"] in ("payment_uncertain", "receipts_blocked"):
        return 2
    return 3 if report["stopped"] == "provider_rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
