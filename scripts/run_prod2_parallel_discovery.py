#!/usr/bin/env python3
"""Prepare/review an offline plan; --go is mandatory for source execution."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'workers/seace/src'))
from buenapro_worker.prod2 import parallel_discovery as discovery
from buenapro_worker.prod2.batch_plan import digest


def parser():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = p.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare', allow_abbrev=False, help='Offline only: no database, source, model or Redis')
    prepare.add_argument('--plan', type=Path, required=True)
    prepare.add_argument('--run-id', required=True)
    prepare.add_argument('--from-date', required=True)
    prepare.add_argument('--to-date', default=datetime.now(ZoneInfo('America/Lima')).date().isoformat())
    prepare.add_argument('--years', required=True, help='Explicit comma-separated convocatoria years; does not mean publication year')
    prepare.add_argument('--workers', type=int, default=10)
    prepare.add_argument('--request-interval', type=float, default=2)
    prepare.add_argument('--max-http-requests', type=int, default=5000)
    prepare.add_argument('--max-runtime-seconds', type=int, default=14400)
    prepare.add_argument('--scope', choices=['all','industrial','digital'], default='all')
    prepare.add_argument('--include-industrial-variants', action='store_true')
    for name in ('validate','check','apply-schema','run'):
        cmd = sub.add_parser(name, allow_abbrev=False, help={'validate':'Offline plan/runtime validation', 'check':'Explicit PG read-only readiness; no source or migrations', 'apply-schema':'Explicit go: only reviewed migration0045, no source', 'run':'Explicit metadata-only execution after human go'}[name])
        cmd.add_argument('--plan', type=Path, required=True)
        cmd.add_argument('--expected-sha256', required=True)
        if name in ('check','apply-schema','run'):
            cmd.add_argument('--ssh-host', help='Optional private PG tunnel; omit for DATABASE_URL/.env.local')
            cmd.add_argument('--ssh-key', type=Path)
        if name == 'apply-schema':
            cmd.add_argument('--go', action='store_true')
        if name == 'run':
            cmd.add_argument('--folder', type=Path, required=True)
            cmd.add_argument('--go', action='store_true')
            cmd.add_argument('--resume', action='store_true')
            cmd.add_argument('--access-recovered', action='store_true', help='Only with new access-recovery evidence after a deliberate source stop')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    if args.command == 'prepare':
        try:
            years = [int(value) for value in args.years.split(',')]
        except ValueError:
            raise discovery.DiscoveryError('explicit_valid_years_required') from None
        plan = discovery.build_plan(ROOT, run_id=args.run_id, published_from=args.from_date,
            published_to=args.to_date, years=years, workers=args.workers,
            request_interval=args.request_interval, max_http_requests=args.max_http_requests,
            max_runtime_seconds=args.max_runtime_seconds, scope=args.scope,
            include_industrial_variants=args.include_industrial_variants)
        print(json.dumps(discovery.prepare(args.plan, plan), ensure_ascii=False))
        return 0
    if args.command in ('run','apply-schema') and not args.go:
        raise discovery.DiscoveryError('explicit_go_required')
    plan = discovery.load_plan(args.plan, args.expected_sha256, ROOT)
    if args.command == 'validate':
        print(json.dumps({'status':'valid_awaiting_go','plan_sha256':digest(plan),
            'keywords':len(plan['keywords']), 'queries':len(plan['queries']), 'source_calls':0,
            'model_calls':0,'database_writes':0}, ensure_ascii=False))
        return 0
    # The imports and configuration below are unreachable in offline modes.
    import psycopg
    from psycopg.rows import dict_row
    from buenapro_worker.prod2.discovery_transport import database_url, SourceGate
    from buenapro_worker.prod2.discovery_registry import require_schema
    with database_url(project_root=ROOT, ssh_host=args.ssh_host, ssh_key=args.ssh_key) as url:
        def connect():
            return psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=15,
                options='-c statement_timeout=15000', application_name='buenapro-prod2-discovery-only')
        if args.command == 'apply-schema':
            with connect() as conn:
                result = discovery.apply_reviewed_schema(conn, ROOT)
            print(json.dumps(result))
            return 0
        if args.command == 'check':
            with connect() as conn:
                conn.execute('BEGIN READ ONLY')
                try:
                    require_schema(conn)
                    tracking=conn.execute("SELECT to_regclass('prod2_keyword_tasks') IS NOT NULL AND to_regclass('prod2_keyword_sweeps') IS NOT NULL AS ready").fetchone()
                    locks=conn.execute("""SELECT count(*) AS count FROM pg_locks WHERE locktype='advisory' AND granted
                        AND classid::bigint=(hashtext('prod2_accepted_executor')::bigint & 4294967295) AND objid=0 AND objsubid=2""").fetchone()
                    ready=tracking['ready'] and locks['count']==0
                finally:
                    conn.execute('ROLLBACK')
            print(json.dumps({'status':'pg_ready_source_not_tested' if ready else 'blocked',
                'active_prod2_coordinators':locks['count'], 'source_access':'not_checked',
                'source_calls':0,'model_calls':0,'database_writes':0}))
            return 0 if ready else 2
        report = discovery.run(args.plan, args.expected_sha256, args.folder, ROOT, go=args.go,
            resume=args.resume, access_recovered=args.access_recovered, connect=connect, gate_factory=SourceGate)
        print(json.dumps({'status':report['status'],'queries_complete':report['queries_complete'],
            'queries_planned':report['queries_planned'],'metadata':report['metadata']['counts'],
            'http':report['http'],'model_calls':0}, ensure_ascii=False))
        return 0 if report['source_coverage_complete'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except discovery.DiscoveryError as error:
        print(json.dumps({'status':'blocked','reason':str(error),'model_calls':0}), file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print(json.dumps({'status':'blocked','reason':'discovery_command_failed','model_calls':0}), file=sys.stderr)
        raise SystemExit(2)
