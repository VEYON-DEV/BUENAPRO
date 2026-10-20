#!/usr/bin/env python3
"""Freeze/dry-run/execute finite DIN commercial title affinity for exact 157 IDs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from dotenv import dotenv_values
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2 import din_title_affinity as affinity


@contextmanager
def database(args):
    if args.database_url_env:
        value = os.environ.get(args.database_url_env)
        if not value:
            raise affinity.AffinityStop("Explicit database environment variable is empty")
        yield value
    else:
        with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            yield settings.database_url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "dry-run", "execute"))
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--selection", type=Path, default=ROOT / "tmp/ia-industrial-execution-20261020/selection.private.json")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--run-id", default="din-selected-title-affinity-20261020")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    parser.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    parser.add_argument("--database-url-env")
    parser.add_argument("--go", action="store_true")
    args = parser.parse_args(argv)
    folder = args.folder.resolve()
    path = folder / "plan.private.json"
    if args.command == "prepare":
        if path.exists():
            plan = affinity.read_bound(path)
            affinity.validate_plan(plan, args.expected_plan_sha256 or affinity.digest(plan), root=ROOT)
        else:
            with database(args) as dsn, affinity.connection(dsn) as conn:
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    plan = affinity.freeze(conn, args.selection, root=ROOT, run_id=args.run_id)
            affinity.immutable_write(path, plan)
        print(json.dumps({"plan_sha256": affinity.digest(plan), "selected": len(plan["cases"]),
                          "unique_provider_requests": len({case["provider_request_hash"] for case in plan["cases"]}),
                          "source_calls": 0, "model_calls": 0, "pg_writes": 0}))
        return 0
    if not args.expected_plan_sha256:
        raise affinity.AffinityStop("Explicit --expected-plan-sha256 is required")
    plan = affinity.read_bound(path, args.expected_plan_sha256)
    affinity.validate_plan(plan, args.expected_plan_sha256, root=ROOT)
    if args.command == "dry-run":
        import tiktoken
        tokenizer = tiktoken.get_encoding("cl100k_base")
        requests = {case["provider_request_hash"]: affinity._jev_payload(case["description"], plan["profile"]["commercial_profiles"], affinity.JEV_MODEL)
                    for case in plan["cases"]}
        tokens = sum(len(tokenizer.encode(affinity.encoded(payload).decode())) for payload in requests.values())
        print(json.dumps({"selected": len(plan["cases"]), "unique_requests": len(requests), "proxy_input_tokens": tokens,
            "proxy_cost_usd": str(Decimal(tokens)*Decimal("0.042")/Decimal(1000000)), "tokenizer_is_provider_tokenizer": False,
            "budget_usd": plan["budget_usd"], "eligibility_analysis": False, "source_calls": 0, "model_calls": 0}))
        return 0
    if not args.go:
        raise affinity.AffinityStop("Paid DIN commercial title affinity requires explicit --go")
    key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY")
    with database(args) as dsn:
        report = affinity.execute(dsn, plan, folder, args.expected_plan_sha256, key, root=ROOT,
                                  limit=args.limit, concurrency=args.concurrency)
    print(json.dumps({key: value for key, value in report.items() if key != "outcomes"}))
    return 2 if report["status"] == "attention_required" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except affinity.AffinityStop as error:
        print(json.dumps({"status": "attention_required", "reason": str(error), "no_automatic_retry": True}))
        raise SystemExit(2)
