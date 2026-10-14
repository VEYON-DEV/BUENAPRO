"""Offline proof that continuation processes only the frozen original queue."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_pending_continuation", ROOT / "scripts/continue_prod2_pending.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


@pytest.fixture
def context(tmp_path):
    original, folder = tmp_path / "original", tmp_path / "continuation"
    original.mkdir()
    old = {"run_id": "original", "pipeline": "prod2_selected_llm1_v1", "document_scope": "prod2_selected_documents_v1",
        "terminal_stage": "llm1", "gemini_model": "gemini-3.1-flash-lite", "jev_threshold": 60,
        "frozen_at": "2026-10-06T12:00:00+00:00",
        "profile_id": "bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "commercial_profiles": [{"descripcion": "software", "lineas": []}],
        "published_from": "2026-04-06", "published_to": "2026-10-06", "resolved_years": [2026, 2025],
        "keywords": ["software"], "objects": ["good", "service"], "cache_folder": str(tmp_path / "cache"),
        "max_source_mib": 128, "max_expanded_mib": 256, "max_archive_members": 2000,
        "max_candidates": 4, "max_accepted": 1, "selector_high": 80, "selector_low": 20,
        "keyword_tracking": {"sweep_id": "original", "policy": "prod2_keyword_tracking_v1"}, "requested": {"legacy": True}}
    subject.private_write(original / "manifest.private.json", old)
    subject.private_write(original / "10/outcome.private.json", {"convocation_id": 10, "accepted": True, "status": "needs_review"})
    args = subject.parser().parse_args(["--original-folder", str(original), "--folder", str(folder), "--expected-pending", "3"])
    return SimpleNamespace(original=original, folder=folder, old=old, args=args)


def item(conv):
    return {"row": {"procedure_id": 100 + conv, "convocation_id": conv, "nomenclature": "SYNTHETIC",
        "buyer_name": "SYNTHETIC BUYER", "published_at": "2026-05-01T12:00:00-05:00", "description": "SOPORTE DE SOFTWARE",
        "object_type": "service", "object_label": "Servicio", "reference_amount": "100.00", "currency": "Soles", "seace_version": "3"},
        "query": {"keyword": "software", "object_type": "service", "year": 2026, "published_from": "2026-04-06",
            "published_to": "2026-10-06", "seace_version": "3"},
        "observed_at": "2026-10-06T22:00:00+00:00", "page_number": 1}


def queue(ids=(20, 21, 22)):
    return {"states": {"10": "done", **{str(i): "queued" for i in ids}},
        "payloads": {str(i): item(i) for i in ids}, "queue": list(map(str, ids)), "pending": []}


def bind(context, value=None):
    base, old = subject.plan(context.args)
    manifest = subject.bind_snapshot(base, old, value or queue())
    return base, manifest


def outcome(folder, conv, *, accepted=False, status="rejected_below_60"):
    value = {"convocation_id": conv, "accepted": accepted, "status": status}
    subject.private_write(folder / str(conv) / "outcome.private.json", value)
    return value


def test_new_run_keeps_original_queue_context_but_routes_new_enrichment(context):
    before = (context.original / "manifest.private.json").read_bytes()
    base, frozen = bind(context)
    assert base["original_staging_identity"] == subject.selected.staging_identity(context.old)
    assert base["run_id"] == "continuation" and base["original_run_id"] == "original"
    assert base["commercial_profiles"] == context.old["commercial_profiles"]
    assert base["published_from"] == context.old["published_from"]
    assert base["document_previews"] is True and base["selector_policy_version"] == subject.DOCUMENT_SELECTION_POLICY_V3
    assert base["llm1_ficha_context_policy"] == subject.LLM1_FICHA_CONTEXT_POLICY
    assert subject.resolve_llm1_model(base, 20, context.old["gemini_model"])[0] == "gemini-3.1-flash-lite"
    assert subject.resolve_llm1_model(base, 21, context.old["gemini_model"])[0] == "gemini-2.5-flash-lite"
    assert "requested" not in base and frozen["pending_ids"] == [20, 21, 22]
    assert frozen["queue_before"] == {"queued": 3, "pending": 0, "payloads": 3, "seen": 4}
    assert (context.original / "manifest.private.json").read_bytes() == before


def test_dry_run_has_no_services_or_writes(context, monkeypatch, capsys):
    def forbidden(*a, **k):
        pytest.fail("Offline plan must not reach services")
    monkeypatch.setattr(subject.selected, "runtime", forbidden)
    monkeypatch.setattr(subject.psycopg, "connect", forbidden)
    monkeypatch.setattr(subject.redis.Redis, "from_url", forbidden)
    monkeypatch.setattr(sys, "argv", ["continue", "--original-folder", str(context.original), "--folder", str(context.folder),
        "--expected-pending", "3", "--dry-run"])
    assert subject.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider_calls"] == report["redis_calls"] == report["database_writes"] == 0
    assert report["discovery_sweep_calls"] == report["new_candidates"] == 0
    assert not context.folder.exists()


@pytest.mark.parametrize("field,value", [("expected_pending", 0), ("workers", 9), ("model_workers", 5),
    ("request_interval", 1), ("max_jev_calls", 0), ("max_llm1_calls", 0), ("expected_pending", 2)])
def test_plan_rejects_bad_bounds_or_wrong_exact_count(context, field, value):
    setattr(context.args, field, value)
    with pytest.raises(ValueError):
        subject.plan(context.args)


def test_separate_safe_folder_and_exact_resume_flags(context):
    context.args.folder = context.original / "child"
    with pytest.raises(ValueError, match="separate"):
        subject.plan(context.args)
    context.args.folder = context.folder
    _, manifest = bind(context)
    subject.private_write(context.folder / "manifest.private.json", manifest)
    assert subject.plan(context.args)[0]["run_id"] == manifest["run_id"]
    context.args.max_llm1_calls += 1
    with pytest.raises(ValueError, match="inputs changed"):
        subject.plan(context.args)


def test_resume_compares_frozen_snapshot_time_symmetrically_without_mutating_captures(context):
    _, manifest = bind(context)
    assert manifest["frozen_at"] != context.old["frozen_at"]
    subject.private_write(context.folder / "manifest.private.json", manifest)
    paths = [context.original / "manifest.private.json", context.folder / "manifest.private.json"]
    before = [path.read_bytes() for path in paths]
    resumed, old = subject.plan(context.args)
    assert old["frozen_at"] == resumed["frozen_at"] == context.old["frozen_at"]
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize("field,value", [("max_jev_calls", 179), ("workers", 1), ("request_interval", 3)])
def test_resume_still_rejects_operational_flag_changes_with_inherited_frozen_time(context, field, value):
    _, manifest = bind(context)
    subject.private_write(context.folder / "manifest.private.json", manifest)
    setattr(context.args, field, value)
    with pytest.raises(ValueError, match="inputs changed"):
        subject.plan(context.args)


def test_resume_still_revalidates_payload_after_excluding_snapshot_fields(context):
    _, manifest = bind(context)
    manifest["pending_snapshot"][0]["payload"]["row"]["description"] = "ALTERED"
    manifest["snapshot_hash"] = subject.hash_request(manifest["pending_snapshot"])
    subject.private_write(context.folder / "manifest.private.json", manifest)
    with pytest.raises(ValueError, match="payload binding changed"):
        subject.plan(context.args)


def test_payload_accepts_native_child_window_and_matching_business_context(context):
    value = item(20)
    value["query"].update(published_from="2026-05-01", published_to="2026-05-30")
    assert subject.validate_payload("20", value, context.old) == subject.hash_request(value)
    business = {"search_batch_id": "development", "literal_shared": True}
    old = {**context.old, "business_plan": {"keyword_index": {"software": business}}}
    value["discovery_business_context"] = business
    subject.validate_payload("20", value, old)
    value["discovery_business_context"] = {"search_batch_id": "changed"}
    with pytest.raises(ValueError, match="business context"):
        subject.validate_payload("20", value, old)


@pytest.mark.parametrize("mutate", [
    lambda p: p["row"].update(convocation_id=999),
    lambda p: p["query"].update(keyword="unrelated"),
    lambda p: p["query"].update(year=2024),
    lambda p: p["query"].update(published_from="2026-01-01"),
    lambda p: p["query"].update(published_to="2026-12-01"),
    lambda p: p["query"].update(seace_version="4"),
    lambda p: p["row"].update(object_type="good"),
    lambda p: p["row"].update(description="  SOPORTE DE SOFTWARE  "),
    lambda p: p.update(observed_at="2026-10-06T22:00:00"),
    lambda p: p.update(page_number=True),
    lambda p: p.update(token="secret"),
])
def test_every_payload_scope_and_normalized_row_checked_before_payment(context, mutate):
    value = item(20)
    mutate(value)
    with pytest.raises(ValueError):
        subject.validate_payload("20", value, context.old)


@pytest.mark.parametrize("mutate", [
    lambda v: v["states"].update({"10": "queued"}),
    lambda v: v["queue"].append("20"),
    lambda v: v["payloads"].pop("20"),
    lambda v: v["states"].update({"999": "done"}),
    lambda v: v["pending"].append("20"),
    lambda v: v["states"].update({"20": "done"}),
])
def test_initial_queue_requires_previous_done_exact_count_and_consistent_redis(context, mutate):
    base, _ = subject.plan(context.args)
    value = queue()
    mutate(value)
    with pytest.raises(ValueError):
        subject.bind_snapshot(base, context.old, value)


def test_atomic_snapshot_decodes_lua_empty_maps_and_rejects_duplicates_or_nan():
    class Client:
        raw = json.dumps({"states": ["10", "done"], "payloads": {}, "queue": {}, "pending": {}})
        def eval(self, script, count, *keys):
            assert "HGETALL" in script and "LRANGE" in script and "HSET" not in script
            assert count == 4 and keys == ("a", "b", "c", "d")
            return self.raw
    client = Client()
    stage = SimpleNamespace(client=client, keys=("a", "b", "c", "d"))
    assert subject.snapshot_queue(stage) == {"states": {"10": "done"}, "payloads": {}, "queue": [], "pending": []}
    client.raw = json.dumps({"states": ["10", "done", "10", "done"], "payloads": {}, "queue": {}, "pending": {}})
    with pytest.raises(ValueError, match="malformed"):
        subject.snapshot_queue(stage)
    client.raw = json.dumps({"states": [], "payloads": ["20", '{"x":NaN}'], "queue": ["20"], "pending": {}})
    with pytest.raises(ValueError, match="malformed"):
        subject.snapshot_queue(stage)


class Stage:
    def __init__(self, value=None):
        self.value = value or queue()
        self.lock = threading.Lock()
        self.acks = []
        self.recovers = 0

    def next(self):
        with self.lock:
            if not self.value["queue"]:
                return None
            identity = self.value["queue"].pop(0)
            self.value["states"][identity] = "pending"
            self.value["pending"].insert(0, identity)
            return identity, deepcopy(self.value["payloads"][identity])

    def ack(self, identity):
        with self.lock:
            assert self.value["states"][identity] == "pending"
            self.value["pending"].remove(identity)
            self.value["payloads"].pop(identity)
            self.value["states"][identity] = "done"
            self.acks.append(identity)
            return True

    def recover(self):
        with self.lock:
            self.recovers += 1
            for identity in self.value["pending"]:
                self.value["queue"].insert(0, identity)
                self.value["states"][identity] = "queued"
            self.value["pending"] = []

    def stats(self):
        return subject.snapshot_stats(self.value)


def test_wrapper_requires_durable_outcome_and_retains_unexpected_reservation(context, monkeypatch):
    _, manifest = bind(context)
    monkeypatch.setattr(subject.selected, "STOP", threading.Event())
    stage = Stage()
    bound = subject.BoundStage(stage, manifest, context.old, context.folder)
    identity, _ = bound.next()
    with pytest.raises(ValueError, match="durable"):
        bound.ack(identity)
    assert stage.value["pending"] == ["20"] and not stage.acks
    outcome(context.folder, 20)
    assert bound.ack(identity) and stage.acks == ["20"]
    subject.selected.STOP.clear()
    stage.value["payloads"]["21"]["row"]["description"] = "CHANGED SOFTWARE"
    with pytest.raises(ValueError, match="allowlist"):
        bound.next()
    assert subject.selected.STOP.is_set() and stage.value["pending"] == ["21"]
    assert stage.acks == ["20"]
    stage.recover()
    assert stage.value["queue"][0] == "21"


def test_resume_allows_crash_before_ack_but_not_done_without_local_outcome(context):
    _, manifest = bind(context)
    value = queue()
    outcome(context.folder, 20)
    subject.validate_resume_queue(value, manifest, context.old, subject.read_outcomes(context.folder, manifest))
    value["queue"].remove("20")
    value["payloads"].pop("20")
    value["states"]["20"] = "done"
    subject.validate_resume_queue(value, manifest, context.old, subject.read_outcomes(context.folder, manifest))
    with pytest.raises(ValueError, match="no durable"):
        subject.validate_resume_queue(value, manifest, context.old, {})


@pytest.mark.parametrize("field", ["pending_ids", "pending_payload_hashes", "snapshot_hash", "queue_before"])
def test_snapshot_binding_detects_immutable_evidence_changes(context, field):
    _, manifest = bind(context)
    if field == "pending_ids":
        manifest[field][0] = 10
    elif field == "pending_payload_hashes":
        manifest[field]["20"] = "0" * 64
    elif field == "snapshot_hash":
        manifest[field] = "0" * 64
    else:
        manifest[field]["seen"] = 100
    with pytest.raises(ValueError):
        subject.validate_frozen(manifest, context.old)


def test_report_rejected_are_finished_but_accepted_errors_remain_incomplete(context, monkeypatch):
    _, manifest = bind(context)
    stage = Stage()
    monkeypatch.setattr(subject.selected, "STOP", threading.Event())
    monkeypatch.setattr(subject, "audit_summary", lambda folder: {"usage_unknown_attempts": 0, "known_cost_usd": .01})
    for conv, accepted, status in [(20, False, "rejected_below_60"), (21, True, "llm1_complete"), (22, True, "needs_review")]:
        identity, _ = stage.next()
        outcome(context.folder, conv, accepted=accepted, status=status)
        stage.ack(identity)
    value = subject.report(context.folder, manifest, stage, SimpleNamespace(requests=10), began=0)
    assert value["processed"] == 3 and value["rejected_ids"] == [20] and value["llm1_complete_ids"] == [21]
    assert value["needs_review_ids"] == [22] and not value["complete"]
    assert value["discovery_sweep_calls"] == value["new_candidates"] == value["previous_members_reprocessed"] == 0


def install_main_fakes(context, monkeypatch, *, invalid_payload=False, accepted_error=False):
    stage, queries, consumed = Stage(), [], []
    if invalid_payload:
        stage.value["payloads"]["20"]["query"]["keyword"] = "outside"
    class Settings(SimpleNamespace):
        def model_copy(self, update):
            return Settings(**{**vars(self), **update})
    @contextmanager
    def runtime(args):
        yield Settings(database_url="synthetic", gemini_model="gemini-3.1-flash-lite"), "synthetic"
    class Cursor:
        def __init__(self, rows):
            self.rows = rows
        def fetchone(self):
            return self.rows[0]
        def fetchall(self):
            return self.rows
    class Connection:
        closed = broken = False
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def execute(self, sql, params=()):
            queries.append(sql)
            if "pg_try_advisory_lock" in sql:
                return Cursor([{"locked": True}])
            if "to_regclass" in sql:
                return Cursor([{"relation": params[0]}])
            if "prod2_accepted_run_members" in sql:
                return Cursor([{"convocation_id": 10}])
            return Cursor([{}])
    class Redis:
        def close(self):
            pass
    def consume(settings, bound, manifest, folder, pacer, limits, key):
        # Every worker receives immutable evidence before any provider action.
        frozen = subject.read_private(folder / "manifest.private.json")
        members = subject.read_private(folder / "members.private.json")
        assert members == frozen["pending_snapshot"] == manifest["pending_snapshot"]
        while not subject.selected.STOP.is_set():
            job = bound.next()
            if job is None:
                return
            identity, payload = job
            conv = int(identity)
            if not (folder / identity / "outcome.private.json").exists():
                consumed.append(conv)
                outcome(folder, conv, accepted=accepted_error and conv == 21,
                    status="needs_review" if accepted_error and conv == 21 else "rejected_below_60")
            bound.ack(identity)
    def forbidden(*a, **k):
        pytest.fail("Continuation must not discover candidates or resolve source years")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(subject.selected, "STOP", threading.Event())
    monkeypatch.setattr(subject.selected, "runtime", runtime)
    monkeypatch.setattr(subject.selected, "consume", consume)
    monkeypatch.setattr(subject.selected, "resolve_years", forbidden)
    monkeypatch.setattr(subject.selected, "run_sweeps", forbidden)
    monkeypatch.setattr(subject.psycopg, "connect", lambda *a, **k: Connection())
    monkeypatch.setattr(subject.redis.Redis, "from_url", lambda *a, **k: Redis())
    monkeypatch.setattr(subject, "RedisStage", lambda client, identity: stage)
    monkeypatch.setattr(subject, "snapshot_queue", lambda unused: deepcopy(stage.value))
    monkeypatch.setattr(subject, "audit_summary", lambda folder: {"jev_calls": 0, "llm1_calls": 0, "usage_unknown_attempts": 0,
        "known_cost_usd": 0})
    monkeypatch.setattr(sys, "argv", ["continue", "--original-folder", str(context.original), "--folder", str(context.folder), "--expected-pending", "3"])
    return stage, queries, consumed


def test_main_processes_original_queue_only_and_replay_never_reprocesses(context, monkeypatch, capsys):
    stage, queries, consumed = install_main_fakes(context, monkeypatch)
    assert subject.main() == 0
    assert sorted(consumed) == [20, 21, 22] and "10" not in stage.acks and stage.stats()["seen"] == 4
    assert stage.recovers == 2 and any("pg_try_advisory_lock" in q for q in queries)
    reports = list(context.folder.glob("report-*.private.json"))
    assert subject.read_private(reports[-1])["complete"] is True
    assert subject.main() == 0
    assert sorted(consumed) == [20, 21, 22] and stage.recovers == 4
    assert len(stage.acks) == 3
    capsys.readouterr()


def test_main_rejects_bad_pending_before_consumer_provider_or_manifest_creation(context, monkeypatch):
    stage, queries, consumed = install_main_fakes(context, monkeypatch, invalid_payload=True)
    with pytest.raises(ValueError, match="scope differs"):
        subject.main()
    assert not consumed and not stage.acks and not (context.folder / "manifest.private.json").exists()


def test_main_accepted_errors_return_incomplete_even_when_every_queue_member_acked(context, monkeypatch, capsys):
    stage, queries, consumed = install_main_fakes(context, monkeypatch, accepted_error=True)
    assert subject.main() == 2
    value = json.loads(capsys.readouterr().out)
    assert value["processed"] == 3 and value["queue_after"]["payloads"] == 0 and value["needs_review_ids"] == [21]
    assert not value["complete"]
