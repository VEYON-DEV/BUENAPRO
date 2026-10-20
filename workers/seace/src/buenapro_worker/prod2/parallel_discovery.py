"""Opt-in metadata mapping. No models, documents, Redis, or automatic publication.

Preparation is offline. Execution requires the reviewed plan digest and --go;
all searches share one coordinator lock, request budget and stop signal.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from zoneinfo import ZoneInfo

from .batch_plan import configured_keywords, digest
from .keyword_tracking import KeywordTracker, completed_cache_errors, scope_for, seed_queries
from .service import _literal as _service_literal, _lock, _read, _write

VERSION = "prod2_parallel_discovery_v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
CODE_FILES = (
    "scripts/run_prod2_parallel_discovery.py",
    "workers/seace/src/buenapro_worker/prod2/discovery.py",
    "workers/seace/src/buenapro_worker/prod2/empty_scope_probe.py",
    "workers/seace/src/buenapro_worker/prod2/batch_plan.py",
    "workers/seace/src/buenapro_worker/prod2/service.py",
    "workers/seace/src/buenapro_worker/prod2/parallel_discovery.py",
    "workers/seace/src/buenapro_worker/prod2/discovery_registry.py",
    "workers/seace/src/buenapro_worker/prod2/discovery_transport.py",
    "workers/seace/src/buenapro_worker/prod2/client.py",
    "workers/seace/src/buenapro_worker/prod2/keyword_sweep.py",
    "workers/seace/src/buenapro_worker/prod2/keyword_tracking.py",
    "db/migrations/0045_prod2_discovery_registry.sql",
)


class DiscoveryError(ValueError):
    """Only fixed, credential-free reasons may reach operational reports."""


def _literal(path):
    try:
        return _service_literal(path)
    except ValueError:
        raise DiscoveryError('symlinked_path') from None


def code_bindings(root):
    return {name: hashlib.sha256(_literal(Path(root) / name).read_bytes()).hexdigest()
            for name in CODE_FILES}


def queries_for(keywords, years, start, end):
    return [{"keyword": keyword, "year": year, "object_type": obj,
             "published_from": start, "published_to": end, "seace_version": "3"}
            for keyword in keywords for year in years for obj in ("good", "service")]


def build_plan(root, *, run_id, published_from, published_to, years, workers=10,
               request_interval=2, max_http_requests=5000, max_runtime_seconds=14400,
               scope="all", include_industrial_variants=False, today=None):
    """Pure local preparation; dates/years are explicit and never silently roll."""
    root = Path(root)
    today = today or datetime.now(timezone.utc).astimezone(ZoneInfo('America/Lima')).date()
    start, end = date.fromisoformat(published_from), date.fromisoformat(published_to)
    if (not isinstance(run_id, str) or not IDENTIFIER.fullmatch(run_id)
            or start > end or end > today or (end - start).days > 366):
        raise DiscoveryError("invalid_identity_or_window")
    if (not isinstance(years, list) or not years or len(set(years)) != len(years)
            or any(type(y) is not int or not 2000 <= y <= today.year for y in years)):
        raise DiscoveryError("explicit_valid_years_required")
    if (type(workers) is not int or not 1 <= workers <= 10
            or type(request_interval) not in (int, float) or not 1 <= request_interval <= 60
            or type(max_http_requests) is not int or not 1 <= max_http_requests <= 100000
            or type(max_runtime_seconds) is not int or not 60 <= max_runtime_seconds <= 86400
            or scope not in {"all", "industrial", "digital"}
            or type(include_industrial_variants) is not bool):
        raise DiscoveryError("invalid_operational_bounds")
    inventory_path = root / "docs/sources/prod2-convenio-keywords.v3.json"
    inventory = json.loads(inventory_path.read_bytes())
    keywords = configured_keywords(inventory)
    industrial = {kw for lane in inventory["lanes"] if lane["name"] == "industriales" for kw in lane["keywords"]}
    keywords = [kw for kw in keywords if scope == "all" or ((kw in industrial) == (scope == "industrial"))]
    variants_hash = None
    if include_industrial_variants:
        if scope == "digital":
            raise DiscoveryError("industrial_variants_in_digital_scope")
        proposals = root / "docs/sources/prod2-industrial-search-variants.v1.json"
        variant_config = json.loads(proposals.read_bytes())
        if variant_config.get("status") != "pilot_only" or not isinstance(variant_config.get("keywords"), list):
            raise DiscoveryError("invalid_variant_inventory")
        keywords += variant_config["keywords"]
        variants_hash = hashlib.sha256(proposals.read_bytes()).hexdigest()
    if (not keywords or len(set(keywords)) != len(keywords)
            or any(not isinstance(k, str) or not k or len(k) > 120 or k != ' '.join(k.split()).casefold() for k in keywords)):
        raise DiscoveryError("invalid_keyword_inventory")
    return {"version": VERSION, "run_id": run_id, "source": "seace_prod2", "mode": "metadata_only",
            "published_from": start.isoformat(), "published_to": end.isoformat(),
            "zone": "America/Lima", "years": sorted(years, reverse=True), "objects": ["good", "service"],
            "scope": scope, "include_industrial_variants": include_industrial_variants,
            "keyword_inventory_sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
            "variant_inventory_sha256": variants_hash, "keywords": keywords,
            "queries": queries_for(keywords, sorted(years, reverse=True), start.isoformat(), end.isoformat()),
            "max_workers": workers, "request_interval_seconds": request_interval,
            "max_http_requests": max_http_requests, "max_runtime_seconds": max_runtime_seconds,
            "ramp": {"pilot_queries": 1, "warmup_queries": 8, "warmup_workers": min(4, workers)},
            "max_pages_per_query": 1000, "empty_scope_probes": True,
            "comparison_policy": "prod2_id_listing_hash_v1", "batch_fresh_pages": True,
            "source_atomic_snapshot": False, "native_lifecycle_filter": None,
            "coverage": "only_configured_keywords_objects_years_and_publication_window",
            "models": False, "document_downloads": False, "redis": False, "publish_opportunities": False,
            "code_bindings": code_bindings(root)}


def validate_plan(plan, root):
    if not isinstance(plan, dict) or plan.get("version") != VERSION:
        raise DiscoveryError("unsupported_plan")
    try:
        expected = build_plan(root, run_id=plan['run_id'], published_from=plan['published_from'],
            published_to=plan['published_to'], years=plan['years'], workers=plan['max_workers'],
            request_interval=plan['request_interval_seconds'], max_http_requests=plan['max_http_requests'],
            max_runtime_seconds=plan['max_runtime_seconds'], scope=plan['scope'],
            include_industrial_variants=plan['include_industrial_variants'])
    except (KeyError, TypeError, OSError, ValueError):
        raise DiscoveryError("invalid_or_stale_plan") from None
    if plan != expected:
        raise DiscoveryError("plan_or_runtime_changed_prepare_new_plan")
    return plan


def load_plan(path, expected_sha256, root):
    path = _literal(path)
    if (not isinstance(expected_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_sha256)
            or path.stat().st_mode & 0o077):
        raise DiscoveryError("private_reviewed_plan_required")
    plan = _read(path)
    if digest(plan) != expected_sha256:
        raise DiscoveryError("plan_digest_mismatch")
    return validate_plan(plan, root)


def prepare(path, plan):
    path = _literal(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise DiscoveryError("existing_plan_will_not_be_overwritten")
    _write(path, plan)
    return {"status": "prepared_awaiting_go", "plan": str(path), "plan_sha256": digest(plan),
            "queries": len(plan['queries']), "keywords": len(plan['keywords']),
            "maximum_search_workers": plan['max_workers'], "source_calls": 0, "model_calls": 0,
            "database_writes": 0}


class Owner:
    """Never reconnect the session that owns the global selected-executor lock."""
    def __init__(self, conn, stop):
        self.conn, self.stop, self.mutex = conn, stop, threading.Lock()

    def check(self):
        if self.stop.is_set():
            raise DiscoveryError('stop_requested')
        try:
            with self.mutex:
                row = self.conn.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks
                    WHERE locktype='advisory' AND granted AND pid=pg_backend_pid()
                    AND classid::bigint=(hashtext('prod2_accepted_executor')::bigint & 4294967295)
                    AND objid=0 AND objsubid=2) AS owned""").fetchone()
            if not row or row['owned'] is not True:
                raise DiscoveryError('coordinator_lock_lost')
        except Exception:
            self.stop.set()
            raise DiscoveryError('coordinator_lock_lost') from None


@contextmanager
def coordinator(conn, stop):
    locked = conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()
    if not locked or locked['locked'] is not True:
        raise DiscoveryError('another_prod2_coordinator_active')
    try:
        yield Owner(conn, stop)
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")
        except Exception:
            stop.set()


def bounded_batch(queries, workers, execute, stop):
    """At most workers submitted; no unbounded future queue after a stop."""
    results = []
    source = iter(queries)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        def submit():
            if stop.is_set():
                return False
            query = next(source, None)
            if query is None:
                return False
            pending[pool.submit(execute, query)] = query
            return True
        for _ in range(workers):
            if not submit():
                break
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                query = pending.pop(future)
                try:
                    result = future.result()
                except Exception:
                    stop.set()
                    result = {'query': query, 'status': 'blocked', 'coverage_complete': False,
                              'errors': [{'code': 'discovery_worker_failed'}]}
                results.append(result)
                if not result.get('coverage_complete') or result.get('errors'):
                    stop.set()
            for _ in done:
                if not submit():
                    break
    return results


def execute_mapping(plan, folder, connect, gate_factory, *, stop=None):
    """Dependency-injected driver. Called only after the explicit CLI go gate."""
    from . import discovery_registry as registry
    from .empty_scope_probe import probe_empty_scope, scope_is_verified_zero
    from .keyword_sweep import sweep_query
    stop = stop or threading.Event()
    folder = _literal(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.stat().st_mode & 0o077:
        raise DiscoveryError('mapping_directory_not_private')
    cache = _literal(folder / 'keyword-cache')
    tracker = None
    coverage, proofs = [], []
    with connect() as conn, coordinator(conn, stop) as owner:
        try:
            registry.require_schema(conn)
        except Exception:
            raise DiscoveryError('registry_schema_not_ready_apply_reviewed_migration0045') from None
        available = conn.execute("SELECT to_regclass('prod2_keyword_tasks') IS NOT NULL AND to_regclass('prod2_keyword_sweeps') IS NOT NULL AS ready").fetchone()
        if not available or available['ready'] is not True:
            raise DiscoveryError('apply_migration0040_before_go')
        prior = conn.execute('SELECT frozen_scope FROM prod2_keyword_sweeps WHERE sweep_id=%s', (plan['run_id'],)).fetchone()
        if prior and prior['frozen_scope'] != scope_for(plan):
            raise DiscoveryError('existing_epoch_scope_differs')
        cache_errors = completed_cache_errors(conn, plan['run_id'], cache)
        if cache_errors:
            raise DiscoveryError('completed_cache_missing_no_source_replay')
        registry.freeze_run(conn, plan['run_id'], plan)
        seed_queries(conn, plan['run_id'], plan, plan['queries'])
        gate = gate_factory(stop=stop, interval_seconds=plan['request_interval_seconds'],
            max_requests=plan['max_http_requests'], max_seconds=plan['max_runtime_seconds'], check_owner=owner.check)
        tracker = KeywordTracker(connect, plan['run_id'], plan['run_id'], cache)
        try:
            # Proofs apply only to this exact epoch and explicit years/objects.
            for year in plan['years']:
                for obj in plan['objects']:
                    if stop.is_set():
                        break
                    query = {'keyword': '', 'year': year, 'object_type': obj,
                        'published_from': plan['published_from'], 'published_to': plan['published_to'], 'seace_version': '3'}
                    owner.check()
                    proof = probe_empty_scope(query, source_factory=gate.source_factory,
                        folder=cache / plan['run_id'] / 'scope-probes')
                    proofs.append(proof)
                    if proof['errors']:
                        stop.set()
            def execute(query):
                if stop.is_set():
                    return {'query': query, 'status': 'stopped', 'coverage_complete': False,
                            'errors': [{'code': 'stop_requested'}]}
                owner.check()
                def source_query(path):
                    for proof in proofs:
                        if scope_is_verified_zero(proof, query):
                            return {'query': query, 'status': 'complete', 'coverage_complete': True,
                                'coverage_kind': 'verified_empty_scope_observed', 'source_atomic_snapshot': False,
                                'scope_proof_hash': proof['proof_hash'], 'errors': [],
                                'source_observed_at': max(x['observed_at'] for x in proof['observations'])}
                    def persist(items):
                        if stop.is_set():
                            raise DiscoveryError('stop_requested')
                        try:
                            owner.check()
                            return registry.observe_many(tracker.local.conn, plan['run_id'], items)
                        except Exception:
                            stop.set()
                            raise DiscoveryError('metadata_commit_failed') from None
                    return sweep_query(query, source_factory=gate.source_factory, folder=path,
                        emit=lambda item: persist([item])[0], emit_many=persist, batch_fresh_pages=True,
                        stop_requested=stop.is_set, max_pages=plan['max_pages_per_query'])
                try:
                    result = tracker.run(query, source_query)
                except Exception:
                    stop.set()
                    raise
                if not result.get('coverage_complete') or result.get('errors'):
                    stop.set()
                return result
            pilot = plan['ramp']['pilot_queries']
            warmup = pilot + plan['ramp']['warmup_queries']
            stages = [(plan['queries'][:pilot], 1), (plan['queries'][pilot:warmup], plan['ramp']['warmup_workers']),
                      (plan['queries'][warmup:], plan['max_workers'])]
            for queries, workers in stages:
                if stop.is_set():
                    break
                batch = bounded_batch(queries, workers, execute, stop)
                coverage.extend(batch)
                _write(folder/'progress.private.json', {'queries_complete':sum(x.get('coverage_complete') is True for x in coverage),
                    'queries_planned':len(plan['queries']), 'phase_workers':workers, 'http':gate.metrics(), 'model_calls':0})
            metadata = registry.read_report(conn, plan['run_id'])
            complete = (len(coverage) == len(plan['queries']) and not stop.is_set()
                        and all(x.get('coverage_complete') is True and not x.get('errors') for x in coverage))
            report = {'run_id':plan['run_id'], 'plan_sha256':digest(plan), 'mode':'metadata_only',
                'status':'complete' if complete else 'partial_or_blocked', 'source_coverage_complete':complete,
                'queries_planned':len(plan['queries']), 'queries_complete':sum(x.get('coverage_complete') is True for x in coverage),
                'coverage':coverage, 'scope_proofs':proofs, 'metadata':metadata, 'http':gate.metrics(),
                'model_calls':0, 'document_downloads':0, 'redis_mutations':0, 'published_opportunities':0,
                'interpretation':'Listing observations only; dates, source status, document changes and company fit require separate checks.'}
            reports = folder/'reports'
            reports.mkdir(mode=0o700, exist_ok=True)
            _write(reports/('report-'+str(time.time_ns())+'.private.json'), report)
            _write(folder/'report.private.json', report)
            _write(folder/'handoff.private.json', {'run_id':plan['run_id'], 'plan_sha256':digest(plan),
                'source_coverage_complete':complete, 'automatic_paid_authorization':False,
                'candidate_ids_for_review':metadata.get('candidate_ids',[]) if complete else [],
                'held_observed_candidate_ids':metadata.get('candidate_ids',[]) if not complete else [],
                'needs_review_ids':metadata.get('needs_review_ids',[]),
                'reason':'Review new/changed/unknown baselines; PDF changes, lifecycle and JEV cache require separate checks.'})
            return report
        finally:
            if tracker:
                tracker.close()



def _stop_reason(current, prior, *, complete=False):
    if current in {'source_403','source_429'}:
        return current
    if prior in {'source_403','source_429'} and not complete:
        return prior
    return current


def apply_reviewed_schema(conn, root):
    """Only 0045, behind the CLI go/digest gate and shared coordinator lock.

    Never apply other pending migrations or start any source/model work.
    """
    from . import discovery_registry as registry
    stop = threading.Event()
    with coordinator(conn, stop):
        exists = conn.execute("SELECT to_regclass('prod2_map_runs') IS NOT NULL AS ready").fetchone()
        if exists['ready']:
            registry.require_schema(conn)
            return {'status':'already_ready','migration':'0045_prod2_discovery_registry.sql','source_calls':0,'model_calls':0}
        ledger = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS ready").fetchone()
        if not ledger['ready']:
            raise DiscoveryError('existing_migration_ledger_required')
        with conn.transaction():
            conn.execute((Path(root)/'db/migrations/0045_prod2_discovery_registry.sql').read_text())
            registry.require_schema(conn)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
                ('0045_prod2_discovery_registry.sql',))
        return {'status':'schema_prepared','migration':'0045_prod2_discovery_registry.sql','source_calls':0,'model_calls':0}

def _run_locked(plan_path, expected_sha256, folder, root, *, go=False, resume=False,
        access_recovered=False, connect=None, gate_factory=None):
    """No go means no env, database, source, subprocess or schema side effects."""
    if not go:
        raise DiscoveryError('explicit_go_required')
    plan = load_plan(plan_path, expected_sha256, root)
    folder = _literal(folder)
    state_path = folder/'state.private.json'
    state = _read(state_path) if state_path.exists() else None
    if state:
        if state.get('plan_sha256') != digest(plan):
            raise DiscoveryError('existing_state_plan_differs')
        if state.get('status') == 'complete':
            raise DiscoveryError('completed_plan_use_report_or_new_epoch')
        if not resume:
            raise DiscoveryError('interrupted_run_requires_explicit_resume')
        if (state.get('stop_reason') in {'source_403','source_429'} or state.get('status') == 'running') and not access_recovered:
            raise DiscoveryError('source_recovery_evidence_required')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    prior_hold = state.get('stop_reason') if state else None
    active_gate = {}
    def capture_gate(**kwargs):
        gate = gate_factory(**kwargs)
        active_gate['value'] = gate
        return gate
    _write(state_path, {'status':'running','plan_sha256':digest(plan),'stop_reason':prior_hold})
    try:
        if connect is None or gate_factory is None:
            raise DiscoveryError('explicit_runtime_dependencies_required')
        report = execute_mapping(plan, folder, connect, capture_gate, stop=stop)
        _write(state_path, {'status':report['status'], 'plan_sha256':digest(plan),
            'stop_reason':_stop_reason(report['http'].get('stop_reason'), prior_hold, complete=report['status']=='complete'),
            'queries_complete':report['queries_complete']})
        return report
    except Exception as error:
        stop.set()
        stop_reason = prior_hold
        if active_gate.get('value') is not None:
            stop_reason = _stop_reason(active_gate['value'].metrics().get('stop_reason'), prior_hold)
        _write(state_path, {'status':'attention_required','plan_sha256':digest(plan),
            'stop_reason':stop_reason, 'error_type':type(error).__name__,
            'reason':str(error) if isinstance(error, DiscoveryError) else 'mapping_runtime_error'})
        raise DiscoveryError(str(error) if isinstance(error, DiscoveryError) else 'mapping_runtime_error') from None
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run(plan_path, expected_sha256, folder, root, *, go=False, resume=False,
        access_recovered=False, connect=None, gate_factory=None):
    if not go:
        raise DiscoveryError('explicit_go_required')
    plan = load_plan(plan_path, expected_sha256, root)
    folder = _literal(folder)
    if folder.name != plan['run_id']:
        raise DiscoveryError('folder_must_match_new_run_id')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.stat().st_mode & 0o077:
        raise DiscoveryError('mapping_directory_not_private')
    try:
        with _lock(folder):
            return _run_locked(plan_path, expected_sha256, folder, root, go=go, resume=resume,
                access_recovered=access_recovered, connect=connect, gate_factory=gate_factory)
    except BlockingIOError:
        raise DiscoveryError('mapping_folder_owned_by_another_process') from None
