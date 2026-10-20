#!/usr/bin/env python3
"""Explicit offline-evidence/PG metadata publication for the authorized 157 IDs.

No SEACE, PDF, embeddings, LLM, Redis, matching or worker activation operations.
prepare uses a READ ONLY PG snapshot and writes a frozen local plan. Root must
review its hash before separate apply-schema/publish commands with --go.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

import psycopg
from psycopg.rows import dict_row

from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2 import selected_metadata_publication as publication


@contextmanager
def database(args):
    if args.database_url_env:
        value = os.environ.get(args.database_url_env)
        if not value:
            raise publication.PublicationError("Explicit scratch database URL variable is empty")
        yield value
    else:
        with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            yield settings.database_url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "review", "apply-schema", "publish"))
    parser.add_argument("--folder", required=True)
    parser.add_argument("--publication-run-id", default="prod2-din-selected-metadata-20261020")
    parser.add_argument("--handoff", default=str(ROOT / "tmp/prod2-parallel-discovery-20261020/runs/prod2-map-wide-20261020/handoff.private.json"))
    parser.add_argument("--selection", default=str(ROOT / "tmp/ia-industrial-execution-20261020/selection.private.json"))
    parser.add_argument("--reader-folder", default=str(ROOT / "tmp/ia-industrial-execution-20261020/runs/prod2-din-ia-industrial-20261020"))
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--expected-migration-sha256")
    parser.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    parser.add_argument("--ssh-key", default=str(ROOT / "credentials/ssh-key-2026-07-14.key"))
    parser.add_argument("--database-url-env", help="Explicit isolated scratch PG environment variable")
    parser.add_argument("--go", action="store_true")
    args = parser.parse_args(argv)
    folder = Path(args.folder).resolve()
    plan_path = folder / "publication-plan.private.json"
    if args.command == "prepare":
        if plan_path.exists():
            plan = publication.read_bound(plan_path)
            publication.validate_plan(plan, args.expected_plan_sha256 or publication.digest(plan), root=ROOT)
        else:
            cases, inputs = publication.source_inputs(ROOT, args.handoff, args.selection, args.reader_folder)
            with database(args) as dsn, psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    state = publication.snapshot(conn, cases)
            names = ["scripts/publish_prod2_selected_metadata.py",
                     "workers/seace/src/buenapro_worker/prod2/selected_metadata_publication.py",
                     "db/migrations/0047_prod2_feed_publications.sql"]
            runtime = {name: publication.file_sha(ROOT / name) for name in names}
            plan = publication.build_plan(cases, state, inputs, args.publication_run_id, runtime_bindings=runtime)
            plan["code_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            publication.immutable_write(plan_path, plan)
        print(json.dumps({"plan_sha256": publication.digest(plan), "selected": len(plan["cases"]),
                          "new_discoveries": plan["new_discoveries"], "preserved_discoveries": plan["preserved_discoveries"],
                          "source_calls": 0, "model_calls": 0, "pg_writes": 0}))
        return 0
    if args.command == "apply-schema":
        migration = ROOT / "db/migrations/0047_prod2_feed_publications.sql"
        if not args.go or args.expected_migration_sha256 != publication.file_sha(migration):
            raise publication.PublicationError("Dedicated 0047 requires --go and reviewed --expected-migration-sha256")
        with database(args) as dsn, psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            result = publication.apply_schema(conn, migration)
    else:
        if not args.expected_plan_sha256:
            raise publication.PublicationError("Explicit frozen --expected-plan-sha256 is required")
        plan = publication.read_bound(plan_path, args.expected_plan_sha256)
        publication.validate_plan(plan, args.expected_plan_sha256, root=ROOT)
        if args.command == "review":
            print(json.dumps({"plan_sha256": args.expected_plan_sha256, "selected": len(plan["cases"]),
                "new_discoveries": plan["new_discoveries"], "preserved_discoveries": plan["preserved_discoveries"],
                "reader_statuses": dict(__import__("collections").Counter(case["reader_status"] for case in plan["cases"])),
                "source_calls": 0, "model_calls": 0, "pg_writes": 0}))
            return 0
        if not args.go:
            raise publication.PublicationError("Metadata publication requires explicit --go")
        with database(args) as dsn, psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            result = publication.publish(conn, plan, args.expected_plan_sha256, root=ROOT)
    publication.immutable_write(folder / "receipts" / f"{args.command}-{time.time_ns()}.private.json", result)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except publication.PublicationError as error:
        print(json.dumps({"status": "attention_required", "reason": str(error), "source_calls": 0, "model_calls": 0}))
        raise SystemExit(2)
