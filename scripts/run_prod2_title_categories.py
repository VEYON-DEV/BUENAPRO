#!/usr/bin/env python3
"""Explicit title-only PROD2 backfill. No source/PDF/Redis/other worker operations.

prepare freezes current PostgreSQL titles locally. dry-run is offline. schema is
an explicit, separate migration action. execute never automatically resends any
existing reservation; reconcile only consumes its original durable HTTP reply.
apply performs exact source-title CAS and touches dedicated title labels only.
"""
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
from buenapro_worker.prod2.title_categories import (INPUT_PRICE, build_manifest, digest, load_template,
                                                   validate_manifest)
from buenapro_worker.prod2.title_categories_runtime import (LOCK_KEY, StopDispatch, apply_results, connect,
    execute, immutable_write, prepare_schema, read_json, reconcile, register_run)
from buenapro_worker.prod2.title_categories_runtime import reproject


@contextmanager
def database(args):
    if args.database_url_env:
        dsn = os.environ.get(args.database_url_env)
        if not dsn:
            raise ValueError("Requested database URL environment variable is empty")
        yield dsn
    else:
        with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            yield settings.database_url


def manifest_at(args):
    folder = Path(args.folder).resolve()
    manifest = read_json(folder / "manifest.private.json")
    validate_manifest(manifest, args.expected_manifest_sha256)
    if args.command != "dry-run" and not args.expected_manifest_sha256:
        raise ValueError("Explicit expected manifest SHA256 is required for mutating operations")
    return folder, manifest


def require_go(args):
    if not args.go:
        raise ValueError("This command requires explicit --go")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "dry-run", "schema", "execute", "reconcile", "reproject", "apply"))
    parser.add_argument("--folder", required=True)
    parser.add_argument("--run-id", default="prod2-title-categories-20261020")
    parser.add_argument("--template", default=str(ROOT / "tmp/title-category-plan-20261020/request-template.private.json"))
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--budget-usd", default="0.10")
    parser.add_argument("--limit", type=int, default=20, help="Maximum NEW paid requests during this invocation")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--requests-per-second", type=float, default=1)
    parser.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    parser.add_argument("--ssh-key", default=str(ROOT / "credentials/ssh-key-2026-07-14.key"))
    parser.add_argument("--database-url-env", help="Explicit local/scratch DB URL variable; never print its value")
    parser.add_argument("--go", action="store_true")
    args = parser.parse_args(argv)
    folder = Path(args.folder).resolve()
    if args.command == "prepare":
        template = load_template(Path(args.template))
        # Never refresh an existing frozen folder on rerun.
        if (folder / "manifest.private.json").exists():
            manifest = read_json(folder / "manifest.private.json")
            checksum = validate_manifest(manifest, args.expected_manifest_sha256)
            print(json.dumps({"manifest_sha256": checksum, "cases": len(manifest["cases"]),
                              "unique_titles": len(manifest["requests"]), "reused_frozen_manifest": True}))
            return 0
        with database(args) as dsn, connect(dsn) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                rows = conn.execute("SELECT id_convocatoria,description FROM prod2_discoveries ORDER BY id_convocatoria").fetchall()
        manifest = build_manifest(rows, template, args.run_id, budget_usd=args.budget_usd)
        immutable_write(folder / "manifest.private.json", manifest)
        print(json.dumps({"manifest_sha256": digest(manifest), "cases": len(manifest["cases"]),
                          "unique_titles": len(manifest["requests"]), "paid_requests": 0}))
        return 0
    if args.command == "schema":
        require_go(args)
        migration = ROOT / "db/migrations/0046_prod2_title_categories.sql"
        with database(args) as dsn, connect(dsn) as conn:
            report = prepare_schema(conn, migration)
        from time import time_ns
        immutable_write(folder / "reports" / f"schema-{time_ns()}.private.json", report)
        print(json.dumps({**report, "paid_requests": 0}))
        return 0
    folder, manifest = manifest_at(args)
    if args.command == "dry-run":
        import tiktoken
        from buenapro_worker.prod2.title_categories import encode
        tokenizer = tiktoken.get_encoding("cl100k_base")
        tokens = sum(len(tokenizer.encode(encode(request["payload"]).decode())) for request in manifest["requests"])
        print(json.dumps({"manifest_sha256": digest(manifest), "cases": len(manifest["cases"]),
            "unique_titles": len(manifest["requests"]), "reused_titles": len(manifest["cases"])-len(manifest["requests"]),
            "proxy_input_tokens": tokens, "proxy_cost_usd": str(Decimal(tokens)*INPUT_PRICE),
            "tokenizer_is_provider_tokenizer": False, "budget_usd": manifest["budget_usd"],
            "budget_is_invoice_guarantee": False, "paid_requests": 0}))
        return 0
    require_go(args)
    with database(args) as dsn:
        if args.command == "execute":
            key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY")
            report = execute(dsn, manifest, folder, args.expected_manifest_sha256, key,
                             limit=args.limit, concurrency=args.concurrency,
                             requests_per_second=args.requests_per_second)
        else:
            with connect(dsn) as conn:
                if not conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_KEY,)).fetchone()["locked"]:
                    raise StopDispatch("Title category coordinator already running")
                try:
                    register_run(conn, manifest, args.expected_manifest_sha256)
                    if args.command == "reconcile":
                        report = reconcile(conn, manifest, folder)
                    elif args.command == "reproject":
                        report = reproject(conn, manifest, folder)
                    else:
                        report = apply_results(conn, manifest)
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
            from time import time_ns
            immutable_write(folder / "reports" / f"{args.command}-{time_ns()}.private.json", report)
        # The CLI emits compact counts, never source titles, replies or credentials.
        public = {key: value for key, value in report.items() if key not in {"outcomes", "blocked"}}
        if "blocked" in report:
            public["blocked_count"] = len(report["blocked"])
        print(json.dumps(public))
        return 2 if report.get("status") == "stopped" or report.get("blocked") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (StopDispatch, ValueError) as error:
        print(json.dumps({"status": "attention_required", "reason": str(error), "no_automatic_retry": True}))
        raise SystemExit(2)
