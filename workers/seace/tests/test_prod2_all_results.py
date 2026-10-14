"""Offline all-results replay through discovery, dedup and accepted processing."""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpcore
import psycopg

from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION
from test_prod2_keyword_sweep import Factory, row

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("prod2_all_results_script", ROOT / "scripts/run_prod2_selected.py")
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


@pytest.fixture(autouse=True)
def stopped_flag(monkeypatch):
    monkeypatch.setattr(subject, "STOP", threading.Event())
    from buenapro_worker.prod2 import keyword_sweep
    monkeypatch.setattr(keyword_sweep, "_now", lambda: datetime(2026, 10, 7, 17, tzinfo=timezone.utc))


def options(folder, *extra):
    return subject.parser().parse_args(["--folder", str(folder), "--keyword", "software",
        "--from-date", "2026-10-01", "--to-date", "2026-10-05", "--years", "2026", *extra])


def unlimited(folder):
    policy = subject.execution_policy(options(folder, "--all-results"))
    return subject.RunLimits(**policy["effective_limits"])


def manifest(folder):
    requested = subject.plan(options(folder, "--all-results", "--skip-empty-scope-probe"))
    return {**requested, "requested": requested, "run_id": "synthetic-all-results",
        "published_from": "2026-10-01", "published_to": "2026-10-05", "resolved_years": [2026],
        "profile_id": "00000000-0000-4000-8000-000000000001",
        "commercial_profiles": [{"descripcion": "Industrial software", "lineas": ["PLC"]}],
        "gemini_model": "synthetic-model", "objects": ["service"]}


class Stage:
    """First-observation queue with the same dedup/ack contract as RedisStage."""
    def __init__(self):
        self.seen, self.queue, self.pending = set(), deque(), {}
        self.batch_calls = []

    def stage(self, candidate_id, payload):
        if candidate_id in self.seen:
            return False
        self.seen.add(candidate_id)
        self.queue.append((candidate_id, deepcopy(payload)))
        return True

    def stage_many(self, entries):
        # RedisStage batches the same ordered per-row dedup operations when
        # replaying complete source pages; first observations still win.
        self.batch_calls.append(deepcopy(entries))
        return [self.stage(candidate_id, payload) for candidate_id, payload in entries]

    def next(self):
        if not self.queue:
            return None
        key, payload = self.queue.popleft()
        self.pending[key] = payload
        return key, payload

    def ack(self, candidate_id):
        return self.pending.pop(candidate_id, None) is not None

    def stats(self):
        return {"seen": len(self.seen), "queued": len(self.queue), "pending": len(self.pending),
            "payloads": len(self.queue) + len(self.pending)}


def test_all_results_resume_keeps_manifest_and_payment_identity_immutable(tmp_path):
    original_args = options(tmp_path, "--sweep-id", "synthetic-epoch")
    original = subject.plan(original_args)
    frozen = {**original, "requested": original, "run_id": "synthetic-original",
        "commercial_profiles": [{"descripcion": "Industrial", "lineas": ["PLC"]}],
        "gemini_model": "synthetic-model"}
    path = tmp_path / "manifest.private.json"
    subject.private_write(path, frozen)
    before = path.read_bytes()
    replay_args = options(tmp_path, "--sweep-id", "synthetic-epoch", "--all-results")
    replay = subject.plan(replay_args)
    assert replay == frozen["requested"]
    assert "all_results" not in replay and replay["max_candidates"] == 100
    assert subject.execution_policy(replay_args)["effective_limits"] == {
        "max_accepted": None, "max_candidates": None, "max_jev_calls": None, "max_llm1_calls": None}
    assert subject.staging_identity(frozen) == subject.staging_identity({**frozen, "requested": replay})
    assert subject.jev_request_hash("PLC supply", commercial_profiles=frozen["commercial_profiles"]) == (
        subject.jev_request_hash("PLC supply", commercial_profiles=deepcopy(frozen["commercial_profiles"])))
    assert path.read_bytes() == before


def patch_processing(monkeypatch, limits):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            raise AssertionError("Offline run cannot issue SQL")

    conn = Connection()
    monkeypatch.setattr(subject.psycopg, "connect", lambda *args, **kwargs: conn)
    state = SimpleNamespace(cache={}, classified=[], claimed=[], promoted=[], extracted=[], rejected=[])

    class Client:
        def __init__(self, folder, control, *, before_request, **kwargs):
            self.before_request = before_request

        def close(self):
            pass

    class Analyzer:
        def __init__(self, key, *, client):
            self.client = client

        def classify(self, description, *, commercial_profiles):
            self.client.before_request()
            state.classified.append(description)
            digest = subject.jev_request_hash(description, commercial_profiles=commercial_profiles)
            # Include the exact cutoff, and many unique rejected candidates.
            score = 60 if int(description.rsplit(" ", 1)[-1]) < 12 else 59
            return {"affinity_index": score, "cost_usd": .00001, "cache_key": "prod2:jev:" + digest,
                "model": JEV_MODEL, "policy_version": JEV_POLICY_VERSION}

    def claim(conn, *, request_hash, **kwargs):
        state.claimed.append(request_hash)
        if request_hash in state.cache:
            return {"state": "cached", "result": deepcopy(state.cache[request_hash])}
        return {"state": "claimed", "claim_token": request_hash, "request_hash": request_hash}

    def complete(conn, *, request_hash, result, **kwargs):
        state.cache[request_hash] = deepcopy(result)

    def promote(conn, *, row, **kwargs):
        state.promoted.append(row["convocation_id"])
        return {"created": True}

    def enrich(conn, settings, private, item, current_manifest, pacer, control, key):
        with subject.provider_permission(conn, private, control, provider="llm1", kind="llm1",
                request_hash="f" * 64, evaluation_key="e" * 64) as issue:
            issue()
        state.extracted.append(item["row"]["convocation_id"])
        return {"status": "llm1_complete", "requires_human_review": True}

    monkeypatch.setattr(subject, "AuditedJevClient", Client)
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", Analyzer)
    monkeypatch.setattr(subject.repo, "claim_jev", claim)
    monkeypatch.setattr(subject.repo, "complete_jev", complete)
    monkeypatch.setattr(subject.repo, "promote_accepted", promote)
    monkeypatch.setattr(subject.repo, "record_run_member", lambda *a, **k: state.rejected.append(k["convocation_id"]))
    monkeypatch.setattr(subject, "enrich", enrich)
    monkeypatch.setattr(subject, "cleanup_finished_case", lambda *a, **k: None)
    return state


def test_full_sweep_processes_more_than_100_candidates_and_10_accepted_once(tmp_path, monkeypatch):
    current = manifest(tmp_path)
    # Two keywords return the same convocations: replay must not pay/promote twice.
    current["keywords"] = ["software", "licencias"]
    factory = Factory([row(index, description=f"SYNTHETIC SOFTWARE {index}") for index in range(151)], size=17)
    monkeypatch.setattr(subject, "source_factory", lambda pacer: factory())
    stage, limits = Stage(), unlimited(tmp_path)
    coverage, proofs = subject.run_sweeps(current, tmp_path, None, stage, limits)
    assert len(coverage) == 2 and all(query["coverage_complete"] for query in coverage)
    assert not proofs and limits.seen == len(stage.seen) == 151 and not limits.sweep_stopped()
    state = patch_processing(monkeypatch, limits)
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, current, tmp_path, None, limits, "synthetic")
    outcomes = [subject.read_private(path) for path in tmp_path.glob("*/outcome.private.json")]
    assert len(outcomes) == 151
    assert len(state.classified) == limits.calls["jev"] == 151  # Beyond default120.
    assert len(state.promoted) == len(set(state.promoted)) == len(state.extracted) == 12
    assert limits.calls["llm1"] == 12 and len(limits.accepted_ids) == 12
    assert len(state.rejected) == 139
    assert all(record["affinity_index"] >= 60 for record in outcomes if record["accepted"])
    assert all(record["status"] == "rejected_below_60" for record in outcomes if not record["accepted"])
    assert limits.reserved == {"jev": 0, "llm1": 0} and stage.stats()["pending"] == 0
    before_calls = dict(limits.calls)
    before_promoted = list(state.promoted)

    def forbidden_source(pacer):
        raise AssertionError("Completed source cache must replay without HTTP")

    monkeypatch.setattr(subject, "source_factory", forbidden_source)
    before_batches = len(stage.batch_calls)
    replay, _ = subject.run_sweeps(current, tmp_path, None, stage, limits)
    assert all(query["coverage_complete"] for query in replay)
    assert all(query["http_searches"] == 0 for query in replay)
    assert len(stage.batch_calls) > before_batches
    assert sum(len(entries) for entries in stage.batch_calls[before_batches:]) == 302
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, current, tmp_path, None, limits, "synthetic")
    assert limits.calls == before_calls and state.promoted == before_promoted
    assert stage.stats() == {"seen": 151, "queued": 0, "pending": 0, "payloads": 0}


def test_exact_cached_jev_receipt_uses_no_unlimited_provider_permit(tmp_path, monkeypatch):
    current, limits = manifest(tmp_path), unlimited(tmp_path)
    state = patch_processing(monkeypatch, limits)
    description = "SYNTHETIC SOFTWARE 0"
    digest = subject.jev_request_hash(description, commercial_profiles=current["commercial_profiles"])
    state.cache[digest] = {"affinity_index": 60, "cost_usd": .00001, "cache_key": "prod2:jev:" + digest,
        "model": JEV_MODEL, "policy_version": JEV_POLICY_VERSION}
    private = tmp_path / "2000"
    private.mkdir()
    # Exercise the real payment/cache gate, rather than replacing paid_jev.
    with subject.provider_permission(None, private, limits, provider="jev", kind="affinity", request_hash=digest) as issue:
        analyzer = Mock()
        _, result, hit = subject.paid_jev(None, private, {"convocation_id": 2000, "description": description},
            current["commercial_profiles"], current["profile_id"], analyzer, subject.repo)
        analyzer.classify.assert_not_called()
    assert hit and result["affinity_index"] == 60
    assert limits.calls["jev"] == limits.reserved["jev"] == 0


def test_unlimited_resume_still_counts_historical_calls_and_one_use_permits(tmp_path):
    policy = subject.execution_policy(options(tmp_path, "--all-results"))
    limits = subject.RunLimits(**policy["effective_limits"], jev_calls=120, llm1_calls=10)
    for provider in ("jev", "llm1"):
        limits.before_request(provider)
        unused = limits.reserve_request(provider)
        unused.close()  # A cache hit releases capacity without recording a request.
        issued = limits.reserve_request(provider)
        issued.issue()
        with pytest.raises(subject.LimitReached, match="already consumed"):
            issued.issue()
        issued.close()
    assert limits.calls == {"jev": 122, "llm1": 12}
    assert limits.reserved == {"jev": 0, "llm1": 0}


def test_existing_uncertain_outcome_remains_unchanged_and_never_pays_again(tmp_path, monkeypatch):
    current, limits, stage = manifest(tmp_path), unlimited(tmp_path), Stage()
    private = tmp_path / "2000"
    private.mkdir()
    outcome = {"convocation_id": 2000, "accepted": True, "status": "needs_review", "error_type": "TimeoutError"}
    path = private / "outcome.private.json"
    subject.private_write(path, outcome)
    before = path.read_bytes()
    stage.stage("2000", {"row": {"convocation_id": 2000}})
    patch_processing(monkeypatch, limits)
    forbidden = Mock(side_effect=AssertionError("Uncertain outcome must not retry a paid stage"))
    monkeypatch.setattr(subject, "paid_jev", forbidden)
    monkeypatch.setattr(subject, "provider_permission", forbidden)
    monkeypatch.setattr(subject, "enrich", forbidden)
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, current, tmp_path, None, limits, "synthetic")
    forbidden.assert_not_called()
    assert path.read_bytes() == before and limits.calls == {"jev": 0, "llm1": 0}
    assert stage.stats()["queued"] == stage.stats()["pending"] == 0


def completed_report():
    return {"source_coverage_complete": True, "redis": {"queued": 0, "pending": 0},
        "accepted": 12, "llm1_complete": 12, "costs": {"usage_unknown_attempts": 0},
        "records": [{"accepted": True, "status": "llm1_complete", "requires_human_review": True}] * 12 +
            [{"accepted": False, "status": "rejected_below_60"}] * 139}


def test_completion_allows_successful_technical_extractions_pending_semantic_qa(tmp_path):
    report = completed_report()
    assert subject.completion_exit_code(report, subject.execution_policy(options(tmp_path, "--all-results"))) == 0


@pytest.mark.parametrize("gap", ["source_partial", "queued", "pending", "accepted_unread",
    "unknown_payment", "unclassified_candidate", "selection_review"])
def test_completion_never_claims_finished_with_source_queue_or_paid_gaps(tmp_path, gap):
    report = completed_report()
    if gap == "source_partial":
        report["source_coverage_complete"] = False
    elif gap in {"queued", "pending"}:
        report["redis"][gap] = 1
    elif gap == "accepted_unread":
        report["llm1_complete"] -= 1
    elif gap == "unknown_payment":
        report["costs"]["usage_unknown_attempts"] = 1
    elif gap == "unclassified_candidate":
        report["records"].append({"accepted": False, "status": "needs_review", "error_type": "RuntimeError"})
    elif gap == "selection_review":
        report["records"].append({"accepted": True, "status": "selection_needs_review"})
        report["accepted"] += 1
    policy = subject.execution_policy(options(tmp_path, "--all-results"))
    assert subject.completion_exit_code(report, policy) == 2


def test_no_affinity_accepted_results_are_a_valid_fully_processed_sweep(tmp_path):
    report = completed_report()
    report.update(accepted=0, llm1_complete=0, records=[{"accepted": False, "status": "rejected_below_60"}])
    assert subject.completion_exit_code(report, subject.execution_policy(options(tmp_path, "--all-results"))) == 0


def test_safe_error_chain_preserves_diagnostic_classes_without_private_messages():
    private = "SECRET_KEY https://private.invalid/?token=SECRET_KEY user@example.test"
    cause = httpcore.ReadError(private)
    database = psycopg.OperationalError("Connection " + private)
    database.__cause__ = cause
    # A concrete SQLSTATE is useful; messages and connection info are not.
    outer = psycopg.errors.ConnectionFailure(private)
    outer.__cause__ = database
    facts = subject.safe_error_facts(outer)
    assert facts == {"error_type": "ConnectionFailure", "error_chain": [
        {"error_type": "ConnectionFailure", "sqlstate": "08006"},
        {"error_type": "OperationalError"}, {"error_type": "ReadError"}]}
    encoded = json.dumps(facts)
    assert all(value not in encoded for value in (private, "SECRET_KEY", "private.invalid", "user@example.test"))


def test_safe_error_chain_is_bounded_cycle_safe_and_rejects_arbitrary_sqlstate():
    class InvalidStateError(RuntimeError):
        sqlstate = "SECRET_KEY"

    first = InvalidStateError("secret message")
    previous = first
    for _ in range(8):
        current = RuntimeError("another private message")
        previous.__context__ = current
        previous = current
    previous.__context__ = first
    facts = subject.safe_error_facts(first)
    assert len(facts["error_chain"]) == 5
    assert all(set(entry) == {"error_type"} for entry in facts["error_chain"])
    first.__cause__ = first
    assert subject.safe_error_facts(first) == {
        "error_type": "InvalidStateError", "error_chain": [{"error_type": "InvalidStateError"}]}
