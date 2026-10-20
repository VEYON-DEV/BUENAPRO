import importlib.util
from pathlib import Path
import sys
import hashlib
import io
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from contextlib import contextmanager
import threading

import pytest
import httpx
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_selected_script", ROOT / "scripts/run_prod2_selected.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


@pytest.fixture(autouse=True)
def isolated_embedding_dispatch_stop(monkeypatch):
    monkeypatch.setattr(subject, "EMBEDDING_DISPATCH_STOP", threading.Event())


def args(*values):
    return subject.parser().parse_args(["--folder", "tmp/synthetic-selected", *values])


def test_explicit_recovery_generation_limits_preserve_legacy_defaults():
    assert subject.llm1_generation_options({}) == {}
    assert subject.llm1_generation_options({"llm1_generation_limits": {
        "policy_version": "prod2_explicit_extended_output_v1",
        "max_output_tokens": 65536, "timeout_ms": 300000}}) == {
            "max_output_tokens": 65536, "timeout_ms": 300000}


@pytest.mark.parametrize("value", [None, {},
    {"policy_version": "changed", "max_output_tokens": 65536, "timeout_ms": 300000},
    {"policy_version": "prod2_explicit_extended_output_v1", "max_output_tokens": 24576, "timeout_ms": 300000},
    {"policy_version": "prod2_explicit_extended_output_v1", "max_output_tokens": 65536, "timeout_ms": True}])
def test_invalid_generation_limits_fail_before_enrichment_side_effects(value):
    with pytest.raises(ValueError, match="generation limits"):
        subject.enrich(None, None, None, None, {"llm1_generation_limits": value}, None, None, None)


@pytest.mark.parametrize("value", [None, "unknown", True])
def test_invalid_word_policy_fails_before_enrichment_side_effects(value):
    with pytest.raises(ValueError, match="Word conversion policy"):
        subject.enrich(None, None, None, None, {"word_conversion_policy": value}, None, None, None)


def test_default_pilot_and_all_keywords_cover_joint_union_goods_and_services():
    plan = subject.plan(args())
    assert plan["keywords"] == ["software"]
    assert plan["objects"] == ["good", "service"] and plan["jev_threshold"] == 60
    assert plan["max_accepted"] == 10 and plan["processing_workers"] == 2
    union = subject.plan(args("--all-keywords"))
    assert len(union["keywords"]) == union["configured_keywords"] == 166
    assert len({key.casefold() for key in union["keywords"]}) == 166
    assert plan["llm1_model_routing"] == subject.default_model_routing()
    assert plan["document_storage_policy"] == subject.OFFICIAL_LINKS_POLICY


def test_commercial_taxonomy_cli_is_explicit_new_only_and_auto_preserves_exact_frozen_inputs(tmp_path):
    key, version = "llm1_commercial_taxonomy", "commercial_taxonomy_v1"
    options = args("--folder", str(tmp_path))
    legacy = subject.plan(options)
    assert key not in legacy
    options.llm1_commercial_taxonomy = "none"
    assert subject.plan(options) == legacy
    subject.private_write(tmp_path / "manifest.private.json", {**legacy, "requested": legacy})
    options.llm1_commercial_taxonomy = "auto"
    assert subject.plan(options) == legacy
    options.llm1_commercial_taxonomy = version
    with pytest.raises(ValueError, match="new folder"):
        subject.plan(options)
    (tmp_path / "manifest.private.json").unlink()
    opted = subject.plan(options)
    assert opted == {**legacy, key: version}
    subject.private_write(tmp_path / "manifest.private.json", {**opted, "requested": opted})
    options.llm1_commercial_taxonomy = "auto"
    assert subject.plan(options) == opted
    options.llm1_commercial_taxonomy = "none"
    with pytest.raises(ValueError, match="new folder"):
        subject.plan(options)


@pytest.mark.parametrize("value", [None, "unknown", False])
def test_invalid_commercial_taxonomy_fails_before_enrichment_side_effects(tmp_path, value):
    with pytest.raises(ValueError, match="commercial taxonomy"):
        subject.enrich(None, None, tmp_path, None, {"llm1_commercial_taxonomy": value}, None, None, None)


def test_corrupt_commercial_taxonomy_manifest_is_not_rescued_by_auto(tmp_path):
    options = args("--folder", str(tmp_path))
    legacy = subject.plan(options)
    subject.private_write(tmp_path / "manifest.private.json", {
        **legacy, "llm1_commercial_taxonomy": "commercial_taxonomy_v1", "requested": legacy})
    with pytest.raises(ValueError, match="inputs differ"):
        subject.plan(options)


def test_luna_low_default_is_frozen_only_for_new_folders(tmp_path):
    options = args("--folder", str(tmp_path))
    modern = subject.plan(options)
    assert modern["llm1_luna_fallback"]["model"] == "gpt-6-luna"
    assert modern["llm1_luna_fallback"]["reasoning_effort"] == "low"
    assert modern["llm1_luna_fallback"]["max_attempts"] == 1
    old = {k: v for k, v in modern.items() if k != "llm1_luna_fallback"}
    subject.private_write(tmp_path / "manifest.private.json", {**old, "requested": old})
    assert subject.plan(options) == old
    options.llm1_fallback = "luna"
    with pytest.raises(ValueError, match="new folder"):
        subject.plan(options)
    options.llm1_fallback = "none"
    assert subject.plan(options) == old


def test_invalid_luna_policy_stops_before_any_source_or_paid_operation(tmp_path):
    from buenapro_worker.prod2.luna_fallback import default_luna_fallback_config
    config = {**default_luna_fallback_config(), "reasoning_effort": "high"}
    with pytest.raises(ValueError, match="fallback configuration"):
        subject.enrich(None, None, tmp_path, None, {"llm1_luna_fallback": config}, None, None, None)


def test_official_storage_is_frozen_and_auto_preserves_historical_r2(tmp_path):
    options = args("--folder", str(tmp_path), "--document-storage", "r2")
    old = subject.plan(options)
    assert "document_storage_policy" not in old
    subject.private_write(tmp_path / "manifest.private.json", {**old, "requested": old})
    options.document_storage = "auto"
    assert subject.plan(options) == old
    options.document_storage = "official"
    with pytest.raises(ValueError, match="new folder"):
        subject.plan(options)
    (tmp_path / "manifest.private.json").unlink()
    new = subject.plan(options)
    subject.private_write(tmp_path / "manifest.private.json", {**new, "requested": new})
    options.document_storage = "auto"
    assert subject.plan(options) == new
    options.document_storage = "r2"
    with pytest.raises(ValueError, match="new folder"):
        subject.plan(options)


@pytest.mark.parametrize("value", [None, "unknown", False])
def test_storage_policy_corruption_fails_before_sources_or_paid_work(tmp_path, value):
    options = args("--folder", str(tmp_path))
    frozen = subject.plan(options) | {"document_storage_policy": value}
    subject.private_write(tmp_path / "manifest.private.json", {**frozen, "requested": frozen})
    with pytest.raises(ValueError, match="Unsupported document storage"):
        subject.plan(options)
    with pytest.raises(ValueError, match="Unsupported document storage"):
        subject.enrich(None, None, tmp_path, None, frozen, None, None, None)


def test_routing_auto_preserves_frozen_legacy_inputs_and_new_policy(tmp_path):
    options = args("--llm1-model-routing", "fixed")
    options.folder = tmp_path
    legacy = subject.plan(options)
    subject.private_write(tmp_path / "manifest.private.json", {**legacy, "requested": legacy})
    options.llm1_model_routing = "auto"
    assert subject.plan(options) == legacy
    options.llm1_model_routing = "pages20"
    modern = subject.plan(options)
    assert modern["llm1_model_routing"] == subject.default_model_routing()
    assert modern != legacy
    (tmp_path / "manifest.private.json").unlink()
    subject.private_write(tmp_path / "manifest.private.json", {**modern, "requested": modern})
    options.llm1_model_routing = "auto"
    assert subject.plan(options) == modern


def test_reading_defaults_only_apply_to_new_folders_and_preserve_legacy_resume(tmp_path):
    options = args("--folder", str(tmp_path))
    modern = subject.plan(options)
    assert modern["llm1_scope_policy"] == "prod2_qualification_focus_v1"
    assert modern["llm1_input_policy"] == "prod2_native_text_or_pdf_v1"
    legacy = {k: v for k, v in modern.items() if k not in {"llm1_scope_policy", "llm1_input_policy"}}
    subject.private_write(tmp_path / "manifest.private.json", {**legacy, "requested": legacy})
    assert subject.plan(options) == legacy
    options.llm1_scope = "qualification"
    with pytest.raises(ValueError, match="new folder"):
        subject.plan(options)
    (tmp_path / "manifest.private.json").unlink()
    subject.private_write(tmp_path / "manifest.private.json", {**modern, "requested": modern})
    options.llm1_scope = "auto"
    assert subject.plan(options) == modern
    options.llm1_input = "pdf"
    with pytest.raises(ValueError, match="new folder"):
        subject.plan(options)


@pytest.mark.parametrize("key", ["llm1_scope_policy", "llm1_input_policy"])
def test_reading_policy_disagreement_or_unknown_version_blocks_resume(tmp_path, key):
    options = args("--folder", str(tmp_path))
    modern = subject.plan(options)
    broken = {**modern, "requested": modern, key: "unknown"}
    subject.private_write(tmp_path / "manifest.private.json", broken)
    with pytest.raises(ValueError, match="differ"):
        subject.plan(options)
    broken["requested"] = {**modern, key: "unknown"}
    (tmp_path / "manifest.private.json").unlink()
    subject.private_write(tmp_path / "manifest.private.json", broken)
    with pytest.raises(ValueError, match="Unsupported"):
        subject.plan(options)


@pytest.mark.parametrize("frozen_policy", [None, subject.default_model_routing()])
def test_routing_auto_rejects_conflicting_frozen_policy_without_providers(tmp_path, frozen_policy):
    options = args()
    options.folder = tmp_path
    modern = subject.plan(options)
    subject.private_write(tmp_path / "manifest.private.json", {**modern, "llm1_model_routing": frozen_policy,
        "requested": {k:v for k,v in modern.items() if k!='llm1_model_routing'}})
    with pytest.raises(ValueError, match="Frozen model-routing inputs"):
        subject.plan(options)


def test_document_event_preparation_is_exclusive_and_has_no_source_provider_work(monkeypatch):
    from unittest.mock import MagicMock
    from buenapro_worker.prod2 import document_events
    conn = MagicMock()
    connection = MagicMock()
    connection.__enter__.return_value = conn
    monkeypatch.setattr(subject.psycopg, 'connect', lambda *a, **k: connection)
    @contextmanager
    def runtime(options):
        yield SimpleNamespace(database_url='synthetic'), None
    monkeypatch.setattr(subject, 'runtime', runtime)
    prepare = MagicMock(return_value={"status": "document_events_schema_ready"})
    monkeypatch.setattr(document_events, 'prepare_schema', prepare)
    forbidden = MagicMock(side_effect=AssertionError('No source/provider work during preparation'))
    monkeypatch.setattr(subject, 'resolve_years', forbidden)
    monkeypatch.setattr(subject, 'run_sweeps', forbidden)
    monkeypatch.setattr(subject, 'consume', forbidden)
    monkeypatch.setattr(sys, 'argv', ['selected', '--folder', 'tmp/synthetic-selected', '--prepare-document-events'])
    assert subject.main() == 0
    prepare.assert_called_once_with(conn, ROOT / 'db/migrations/0041_prod2_document_events.sql')
    forbidden.assert_not_called()
    with pytest.raises(ValueError, match='one status/preflight/schema'):
        subject.plan(args('--prepare-document-events', '--preflight'))


def test_business_lanes_are_configured_and_combined_without_duplicate_keywords():
    plan = subject.plan(args("--lane", "prioritarias", "--lane", "complementarias"))
    assert len(plan["keywords"]) == 42
    with pytest.raises(ValueError, match="Unknown keyword lane"):
        subject.plan(args("--lane", "invented"))


def test_business_batches_integrate_union_routing_and_keep_existing_pilot_manifest_compatible():
    legacy = subject.plan(args("--keyword", "software"))
    assert "business_plan" not in legacy
    digital = subject.plan(args("--business-scope", "digitales"))
    assert len(digital["keywords"]) == 73
    assert digital["business_plan"]["selected_literal_shared_keyword_count"] == 16
    full = subject.plan(args("--business-scope", "todas"))
    assert len(full["keywords"]) == 166 and len(full["business_plan"]["batches"]) == 12
    individual = subject.plan(args("--business-batch", "licencias_cloud"))
    assert "software" in individual["keywords"]
    assert individual["business_plan"]["keyword_index"]["software"]["literal_shared"]


def test_thirty_keyword_preset_has_exact_subset_and_explicit_tracking_epoch():
    plan = subject.plan(args("--keyword-preset", "digitales30", "--sweep-id", "initial-digital"))
    assert len(plan["keywords"]) == 30 and plan["keyword_preset"]["keyword_count"] == 30
    assert plan["keyword_tracking"]["sweep_id"] == "initial-digital"
    assert [row["search_keyword_count"] for row in plan["business_plan"]["batches"]] == [12, 8, 6, 4]
    assert "keyword_tracking" not in subject.plan(args("--business-scope", "digitales"))


def test_campaign_queue_persists_across_runs_and_keyword_subsets_but_changes_with_semantics():
    manifest = subject.plan(args("--keyword-preset", "digitales30", "--sweep-id", "initial")) | {
        "run_id": "batch1", "commercial_profiles": {"joint": "synthetic"}, "gemini_model": "synthetic"}
    identity = subject.staging_identity(manifest)
    assert subject.staging_identity(manifest | {"run_id": "batch2", "keywords": ["software"]}) == identity
    assert subject.staging_identity(manifest | {"gemini_model": "changed"}) != identity
    assert subject.staging_identity(manifest | {"commercial_profiles": {"joint": "changed"}}) != identity
    assert subject.staging_identity(manifest | {"keyword_tracking": {"sweep_id": "refresh"}}) != identity
    legacy = subject.plan(args()) | {"run_id": "legacy"}
    assert subject.staging_identity(legacy) == "legacy"


def test_prior_reading_preservation_is_explicit_and_separates_queue_policy():
    old = subject.plan(args("--sweep-id", "initial")) | {
        "run_id": "batch1", "commercial_profiles": {}, "gemini_model": "synthetic"}
    new = subject.plan(args("--sweep-id", "initial", "--preserve-prior-readings")) | {
        "run_id": "batch2", "commercial_profiles": {}, "gemini_model": "synthetic"}
    assert "prior_reading_policy" not in old
    assert new["prior_reading_policy"] == "prod2_preserve_prior_readings_v1"
    assert subject.staging_identity(old) != subject.staging_identity(new)


def test_commercial_taxonomy_uses_separate_campaign_queue_only_when_opted_in():
    old = subject.plan(args("--sweep-id", "initial")) | {
        "run_id": "batch1", "commercial_profiles": {}, "gemini_model": "synthetic"}
    opted = {**old, "llm1_commercial_taxonomy": "commercial_taxonomy_v1"}
    assert subject.staging_identity(old) != subject.staging_identity(opted)
    assert subject.staging_identity({**old}) == subject.staging_identity(old)


@pytest.mark.parametrize("prior_status", ["llm1_complete", "needs_review", None])
def test_consumer_preserves_history_before_document_enrichment(tmp_path, monkeypatch, prior_status):
    from buenapro_worker.prod2 import prior_reading_guard
    order = []
    prior = ({"status": prior_status, "llm1_prior_preserved": True, "llm1_cost_usd": 0}
             if prior_status else None)
    class Stage:
        pending = True
        acked = []
        def next(self):
            if not self.pending: return None
            self.pending = False
            return "123", {"row": {"convocation_id": 123, "description": "software", "object_type": "service"},
                "query": {"keyword": "software"}, "observed_at": "2026-10-09T00:00:00+00:00"}
        def ack(self, value): self.acked.append(value)
    @contextmanager
    def connect(*a, **kw): yield None
    @contextmanager
    def permission(*a, **kw): yield lambda: None
    class Limits:
        def can_process(self, conv): return True
        model = permission
        def admit(self, conv, action): return action()
    def promote(*a, **kw):
        order.append("promote")
        return {"created": False}
    def preserve(conn, **kw):
        order.append("preserve")
        assert kw == {"convocation_id": 123, "profile_id": "profile", "policy": "prod2_preserve_prior_readings_v1"}
        return prior
    def enrich(*a):
        assert prior is None, "Preserved paid history must prevent source/selector/embedding/LLM1 calls"
        order.append("enrich")
        return {"status": "llm1_complete"}
    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject, "provider_permission", permission)
    monkeypatch.setattr(subject, "AuditedJevClient", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", lambda *a, **kw: object())
    monkeypatch.setattr(subject, "jev_request_hash", lambda *a, **kw: "a" * 64)
    monkeypatch.setattr(subject, "paid_jev", lambda *a: ("a" * 64, {"affinity_index": 70, "cost_usd": 0}, True))
    monkeypatch.setattr(subject.repo, "promote_accepted", promote)
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", preserve)
    monkeypatch.setattr(subject, "enrich", enrich)
    monkeypatch.setattr(subject, "cleanup_finished_case", lambda *a: None)
    monkeypatch.setattr(subject, "STOP", threading.Event())
    stage = Stage()
    manifest = {"run_id": "synthetic", "commercial_profiles": {}, "profile_id": "profile",
                "prior_reading_policy": "prod2_preserve_prior_readings_v1"}
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, manifest, tmp_path, None, Limits(), "synthetic")
    outcome = subject.read_private(tmp_path / "123/outcome.private.json")
    assert outcome["status"] == (prior_status or "llm1_complete")
    assert outcome["accepted"] is True and outcome["jev_cached"] is True
    assert order == (["promote", "preserve"] if prior else ["promote", "preserve", "enrich"])
    assert stage.acked == ["123"]
    assert (tmp_path / "123/prior-reading-preserved.private.json").exists() == bool(prior)


def test_preserved_reading_cleanup_does_not_touch_historical_inputs(tmp_path, monkeypatch):
    def forbidden(*a): raise AssertionError("No new inputs were downloaded for a preserved read")
    monkeypatch.setattr(subject, "verify_completed_storage", forbidden)
    assert subject.cleanup_finished_case(None, tmp_path, {"status": "llm1_complete", "llm1_prior_preserved": True},
        {"document_storage_policy": subject.OFFICIAL_LINKS_POLICY}) is None


@pytest.mark.parametrize("status,code", [(400, None), (401, "invalid_api_key"),
    (403, None), (429, "credit_balance_exhausted"), (500, "server_error"),
    (503, "server_is_overloaded"), (None, None)])
def test_sent_embedding_failure_stops_dispatch_with_safe_facts(monkeypatch, status, code):
    from buenapro_worker.prod2.embedding_client import EmbeddingRequestError
    from buenapro_worker.prod2.selected_control import LimitReached
    monkeypatch.setattr(subject, "STOP", threading.Event())
    error = EmbeddingRequestError("embedding_http_error", request_sent=True,
                                  http_status=status, provider_error_code=code)
    result = subject.stop_after_uncertain_embedding_error(error)
    assert result == {"policy_version": "prod2_stop_after_sent_embedding_failure_v1",
        "request_sent": True, "http_status": status, "provider_error_code": code}
    called = []
    with pytest.raises(LimitReached):
        subject.guarded_issue(lambda: called.append(True))()
    assert called == [] and subject.STOP.is_set()


def test_embedding_guard_and_diagnostics_handle_wrapped_cycles_and_mutated_fields(monkeypatch):
    from buenapro_worker.prod2.embedding_client import EmbeddingRequestError
    monkeypatch.setattr(subject, "STOP", threading.Event())
    error = EmbeddingRequestError("embedding_http_error", request_sent=True)
    error.http_status = True
    error.provider_error_code = "private prompt or key must not be recorded"
    wrapped = RuntimeError("secret message")
    wrapped.__cause__, error.__context__ = error, wrapped
    assert subject.stop_after_uncertain_embedding_error(wrapped)["http_status"] is None
    facts = subject.safe_error_facts(wrapped)
    assert facts == {"error_type": "RuntimeError", "error_chain": [
        {"error_type": "RuntimeError"}, {"error_type": "EmbeddingRequestError",
            "http_status": None, "provider_error_code": None}]}


def test_embedding_stop_keeps_inflight_paid_response_and_blocks_next_dispatch(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from test_prod2_embedding_client_store import MemoryStore, success_data
    from buenapro_worker.prod2.embedding_client import EmbeddingClient, EmbeddingRequestError
    monkeypatch.setattr(subject, "STOP", threading.Event())
    dispatched, release, requests = threading.Event(), threading.Event(), []
    def handler(request):
        import json
        requests.append(True)
        dispatched.set()
        assert release.wait(5)
        return httpx.Response(200, json=success_data(json.loads(request.content)["input"]))
    transport = httpx.Client(transport=httpx.MockTransport(handler))
    client = EmbeddingClient(api_key="synthetic", http_client=transport)
    store = MemoryStore()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(store.ensure_vectors, ["inflight synthetic"], client=client,
            folder=tmp_path, before_request=subject.guarded_issue(lambda: None))
        try:
            assert dispatched.wait(5)
            subject.stop_after_uncertain_embedding_error(EmbeddingRequestError(
                "embedding_transport_unknown", request_sent=True))
        finally:
            release.set()
        received = result.result(timeout=5)
    assert received["new_token_count"] > 0 and received["known_cost_usd"] > 0
    terminals = [subject.read_private(path) for path in tmp_path.glob("*.outcome.private.json")]
    assert len(terminals) == 1 and terminals[0]["status"] == "done"
    assert len(list(tmp_path.glob("*.response.private.json"))) == 1
    with pytest.raises(EmbeddingRequestError) as blocked:
        store.ensure_vectors(["next synthetic"], client=client, folder=tmp_path,
            before_request=subject.guarded_issue(lambda: None))
    assert blocked.value.request_sent is False and len(requests) == 1
    cached = store.ensure_vectors(["inflight synthetic"], client=client, folder=tmp_path)
    assert cached["known_cost_usd"] == 0 and len(requests) == 1
    transport.close()


def test_sent_failure_stops_even_when_terminal_persistence_failed(tmp_path, monkeypatch):
    from buenapro_worker.prod2.embedding_client import EmbeddingRequestError
    monkeypatch.setattr(subject, "STOP", threading.Event())
    error = OSError("private receipt location")
    error.__context__ = EmbeddingRequestError("embedding_transport_unknown", request_sent=True)
    assert not list(tmp_path.iterdir())
    assert subject.stop_after_uncertain_embedding_error(error)["request_sent"] is True
    assert subject.STOP.is_set() and subject.EMBEDDING_DISPATCH_STOP.is_set()


def test_consumer_stop_during_reservation_leaves_payload_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "STOP", threading.Event())
    class Stage:
        acked = []
        def next(self):
            subject.STOP.set()
            return "123", {"row": {"convocation_id": 123}}
        def ack(self, value): self.acked.append(value)
    @contextmanager
    def connect(*a, **kw): yield None
    def forbidden(*a, **kw): raise AssertionError("No work after a stopped reservation")
    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject, "enrich", forbidden)
    stage = Stage()
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, {}, tmp_path, None, None, None)
    assert stage.acked == [] and not list(tmp_path.iterdir())


def test_embedding_dispatch_stop_forces_nonzero_exit_even_with_drained_queue():
    assert subject.completion_exit_code({"embedding_dispatch_stop_requested": True}, {"all_results": True}) == 2


@pytest.mark.parametrize("failure_kind", ["sent", "pre_send", "ordinary"])
def test_consumer_stops_only_after_uncertain_terminal_and_preserves_queue(tmp_path, monkeypatch, failure_kind):
    from buenapro_worker.prod2 import prior_reading_guard
    from buenapro_worker.prod2.embedding_client import EmbeddingRequestError
    monkeypatch.setattr(subject, "STOP", threading.Event())
    item = lambda conv: {"row": {"convocation_id": conv, "description": "software", "object_type": "service"},
        "query": {"keyword": "software"}, "observed_at": "2026-10-09T00:00:00+00:00"}
    class Stage:
        def __init__(self): self.queued, self.acked, self.calls = [123, 124], [], 0
        def next(self):
            self.calls += 1
            if not self.queued: return None
            conv = self.queued.pop(0)
            return str(conv), item(conv)
        def ack(self, value): self.acked.append(value)
    @contextmanager
    def connect(*a, **kw): yield None
    @contextmanager
    def permission(*a, **kw): yield lambda: None
    class Limits:
        def can_process(self, conv): return True
        model = permission
        def admit(self, conv, action): return action()
    original = tmp_path / "123/input.txt"
    original.parent.mkdir()
    original.write_text("retained local synthetic input")
    terminal = tmp_path / "123/terminal.private.json"
    def enrich(conn, settings, private, item, *a):
        if item["row"]["convocation_id"] == 124:
            return {"status": "llm1_complete"}
        assert not subject.STOP.is_set()
        if failure_kind == "ordinary": raise ValueError("must not leak message")
        sent = failure_kind == "sent"
        subject.private_write(terminal, {"status": "uncertain" if sent else "failed_pre_send",
            "request_sent": sent, "usage_known": not sent, "cost_usd": None if sent else 0})
        raise EmbeddingRequestError("embedding_http_error" if sent else "embedding_token_limit",
            request_sent=sent, http_status=429 if sent else None,
            provider_error_code="credit_balance_exhausted" if sent else None)
    guard = subject.stop_after_uncertain_embedding_error
    def verified_guard(error):
        if failure_kind == "sent":
            assert terminal.exists() and subject.read_private(terminal)["cost_usd"] is None
        return guard(error)
    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject, "provider_permission", permission)
    monkeypatch.setattr(subject, "AuditedJevClient", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", lambda *a, **kw: object())
    monkeypatch.setattr(subject, "jev_request_hash", lambda *a, **kw: "a" * 64)
    monkeypatch.setattr(subject, "paid_jev", lambda *a: ("a" * 64, {"affinity_index": 70, "cost_usd": 0}, True))
    monkeypatch.setattr(subject.repo, "promote_accepted", lambda *a, **kw: {"created": False})
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", lambda *a, **kw: None)
    monkeypatch.setattr(subject, "enrich", enrich)
    monkeypatch.setattr(subject, "stop_after_uncertain_embedding_error", verified_guard)
    monkeypatch.setattr(subject, "cleanup_finished_case", lambda *a: None)
    stage = Stage()
    manifest = {"run_id": "synthetic", "commercial_profiles": {}, "profile_id": "profile"}
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, manifest, tmp_path, None, Limits(), "synthetic")
    outcome = subject.read_private(tmp_path / "123/outcome.private.json")
    assert outcome["status"] == "needs_review" and original.exists()
    if failure_kind == "sent":
        assert stage.acked == ["123"] and stage.queued == [124] and stage.calls == 1
        assert outcome["embedding_dispatch_stop"]["provider_error_code"] == "credit_balance_exhausted"
        # Another consumer awakened after STOP cannot reserve or dispatch work.
        subject.consume(SimpleNamespace(database_url="synthetic"), stage, manifest, tmp_path, None, Limits(), "synthetic")
        assert stage.calls == 1 and stage.queued == [124]
    else:
        assert not subject.STOP.is_set() and "embedding_dispatch_stop" not in outcome
        assert stage.acked == ["123", "124"] and stage.queued == []


def test_bounded_batches_advance_cached_pages_using_campaign_dedup(tmp_path, monkeypatch):
    from test_prod2_keyword_sweep import Factory, row
    from buenapro_worker.prod2.keyword_sweep import sweep_query
    monkeypatch.setattr(subject, "STOP", threading.Event())
    query = {"keyword": "software", "object_type": "service", "year": 2026,
        "published_from": "2026-10-01", "published_to": "2026-10-05", "seace_version": "3"}
    class Stage:
        seen = set()
        def stage(self, key, item):
            if key in self.seen:
                return False
            self.seen.add(key)
            return True
    stage, factory, reports = Stage(), Factory([row(index) for index in range(7)]), []
    for batch in range(4):
        folder = tmp_path / f"batch{batch}"
        folder.mkdir()
        total = len(stage.seen)
        seen = subject.staged_this_run(folder, "epoch-queue", total, tracked=True)
        assert seen == 0
        limits = subject.RunLimits(max_accepted=1, max_candidates=2, max_jev_calls=2, max_llm1_calls=1, seen=seen)
        reports.append(sweep_query(query, source_factory=factory,
            emit=lambda item: limits.stage(stage, str(item["row"]["convocation_id"]), item),
            folder=tmp_path / "source-cache", stop_requested=limits.sweep_stopped))
        assert len(stage.seen) == min((batch + 1) * 2, 7)
        assert subject.staged_this_run(folder, "epoch-queue", len(stage.seen), tracked=True) == limits.seen
    assert all(not report["coverage_complete"] for report in reports[:3])
    assert reports[-1]["coverage_complete"]
    with pytest.raises(ValueError, match="history missing"):
        subject.staged_this_run(tmp_path / "batch3", "epoch-queue", 0, tracked=True)


def test_shared_queue_checkpoint_detects_redis_loss_even_with_first_origin_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "STOP", threading.Event())
    callback = subject.queue_checkpoint(tmp_path, "epoch-queue", 0)
    callback(2)
    assert subject.queue_checkpoint(tmp_path, "epoch-queue", 2)
    with pytest.raises(ValueError, match="history regressed"):
        subject.queue_checkpoint(tmp_path, "epoch-queue", 0)
    assert subject.read_private(tmp_path / "epoch-queue.private.json")["highest_seen"] == 2
    with pytest.raises(ValueError, match="history regressed"):
        callback(1)
    assert subject.STOP.is_set()


def test_source_rate_limit_stops_all_source_requests_and_new_paid_calls(monkeypatch):
    monkeypatch.setattr(subject, "STOP", threading.Event())
    requested, paced, paid = [], [], []
    def respond(request):
        requested.append(request)
        return httpx.Response(429)
    with httpx.Client(transport=httpx.MockTransport(respond), event_hooks=subject.source_hooks(lambda _: paced.append(True))) as client:
        assert client.get("https://source.invalid/detail").status_code == 429
        assert subject.STOP.is_set()
        with pytest.raises(subject.LimitReached):
            client.get("https://source.invalid/document")
    with pytest.raises(subject.LimitReached):
        subject.guarded_issue(lambda: paid.append(True))()
    assert len(requested) == len(paced) == 1 and paid == []


def test_detail_helper_uses_source_hooks_for_search_detail_and_document_pagination(tmp_path, monkeypatch):
    import run_prod2_accepted as helper
    monkeypatch.setattr(subject, "STOP", threading.Event())
    requested = []
    def respond(request):
        requested.append(request)
        return httpx.Response(403)
    original_client = httpx.Client
    monkeypatch.setattr(helper.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(respond), **kw))
    class Source:
        def __init__(self, client):
            self.client = client
        def search(self, *args, **kwargs):
            self.client.get("https://source.invalid/detail-session").raise_for_status()
    monkeypatch.setattr(helper, "Prod2HttpClient", Source)
    item = {"query": {"keyword": "software", "object_type": "service", "year": 2026,
        "published_from": "2026-10-01", "published_to": "2026-10-05", "seace_version": "3"}}
    with pytest.raises(httpx.HTTPStatusError):
        subject.get_detail(item, tmp_path, lambda _: None, subject.source_hooks(lambda _: None))
    assert subject.STOP.is_set() and len(requested) == 1
    with pytest.raises(subject.LimitReached):
        subject.get_detail(item, tmp_path, lambda _: None, subject.source_hooks(lambda _: None))
    assert len(requested) == 1


def test_frozen_native_years_reused_across_processing_runs_without_source(tmp_path, monkeypatch):
    calls = []
    @contextmanager
    def source(pacer):
        class Source:
            def bootstrap(self):
                calls.append("bootstrap")
            def available_convocation_years(self):
                return [2026, 2025]
        yield Source()
    monkeypatch.setattr(subject, "source_factory", source)
    requested, scope = {"years": "auto"}, {"published_from": "2026-04-06", "published_to": "2026-10-06"}
    assert subject.resolve_years(requested, None, cache_folder=tmp_path, scope=scope) == ([2026, 2025], [2026, 2025])
    monkeypatch.setattr(subject, "source_factory", lambda _: (_ for _ in ()).throw(AssertionError("Unexpected source")))
    assert subject.resolve_years(requested, None, cache_folder=tmp_path, scope=scope)[0] == [2026, 2025]
    assert calls == ["bootstrap"]
    with pytest.raises(ValueError, match="scope changed"):
        subject.resolve_years(requested, None, cache_folder=tmp_path, scope={"published_to": "changed"})


def test_rejected_coordinator_never_contacts_source(tmp_path, monkeypatch):
    @contextmanager
    def runtime(args):
        yield SimpleNamespace(database_url="synthetic", gemini_model="gemini-3.1-flash-lite"), "synthetic"
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, sql, *args):
            assert "pg_try_advisory_lock" in sql
            return SimpleNamespace(fetchone=lambda: {"locked": False})
    monkeypatch.setattr(subject, "runtime", runtime)
    def connect(*args, **kwargs):
        assert kwargs["application_name"] == "buenapro-prod2:blocked"
        assert kwargs["connect_timeout"] == 15
        assert (kwargs["keepalives_idle"], kwargs["keepalives_interval"], kwargs["keepalives_count"]) == (30, 10, 3)
        return Connection()
    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject, "source_factory", lambda _: (_ for _ in ()).throw(AssertionError("Source before lock")))
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic")
    monkeypatch.setattr(subject.sys, "argv", ["run_prod2_selected.py", "--folder", str(tmp_path / "blocked")])
    with pytest.raises(RuntimeError, match="owns the executor"):
        subject.main()


def test_unsupported_routed_model_blocks_before_database_or_source(tmp_path, monkeypatch):
    @contextmanager
    def runtime(options):
        yield SimpleNamespace(database_url="synthetic", gemini_model="gemini-2.5-flash"), "synthetic"
    def forbidden(*a, **kw):
        raise AssertionError("Routing must reject before external work")
    monkeypatch.setattr(subject, "runtime", runtime)
    monkeypatch.setattr(subject.psycopg, "connect", forbidden)
    monkeypatch.setattr(subject, "source_factory", forbidden)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic")
    monkeypatch.setattr(subject.sys, "argv", ["run_prod2_selected.py", "--folder", str(tmp_path / "invalid")])
    with pytest.raises(ValueError, match="unsupported by the page-routing policy"):
        subject.main()


@pytest.mark.parametrize("policy", [None, {**subject.default_model_routing(), "page_threshold": True}])
def test_invalid_policy_blocks_enrichment_before_source_or_paid_calls(tmp_path, monkeypatch, policy):
    def forbidden(*a, **kw):
        raise AssertionError("Invalid routing before discovery/paid calls")
    monkeypatch.setattr(subject, "get_detail", forbidden)
    monkeypatch.setattr(subject, "paid_document_selection", forbidden)
    manifest = {"llm1_model_routing": policy}
    with pytest.raises(ValueError):
        subject.enrich(None, SimpleNamespace(gemini_model="gemini-3.1-flash-lite"), tmp_path,
            {"row": {"convocation_id": 1}}, manifest, None, None, "synthetic")


def test_missing_completed_evidence_blocks_sweep_before_zero_probes(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "source_factory", lambda _: (_ for _ in ()).throw(AssertionError("Source before cache guard")))
    manifest = subject.plan(args("--keyword", "software", "--keyword", "licencia")) | {
        "resolved_years": [2026], "published_from": "2026-04-06", "published_to": "2026-10-06"}
    tracker = SimpleNamespace(completed_cache_errors=lambda: [{"query": subject.planned_queries(manifest)[0],
        "status": "blocked", "coverage_complete": False, "source_atomic_snapshot": False,
        "errors": [{"code": "missing"}]}])
    limits = subject.RunLimits(max_accepted=1, max_candidates=10, max_jev_calls=10, max_llm1_calls=1)
    reports, proofs = subject.run_sweeps(manifest, tmp_path, None, None, limits, tracker)
    assert len(reports) == 4 and proofs == []
    assert all(report["status"] == "blocked" for report in reports)


@pytest.mark.parametrize("values", [
    ["--search-workers", "5"], ["--processing-workers", "20"], ["--model-workers", "0"],
    ["--max-accepted", "101"], ["--max-jev-calls", "0"], ["--request-interval", "0"],
    ["--selector-high", "20", "--selector-low", "30"], ["--keyword", "invented"],
    ["--all-keywords", "--keyword", "software"], ["--years", "2026,2026"],
    ["--business-scope", "digitales", "--keyword", "software"],
    ["--business-batch", "ia_datos", "--lane", "prioritarias"],
    ["--business-batch", "unknown"],
    ["--business-batch", "ia_datos", "--business-scope", "digitales"],
    ["--keyword-preset", "digitales30"], ["--keyword-status"],
    ["--keyword-preset", "digitales30", "--sweep-id", "../invalid"],
    ["--keyword-preset", "digitales30", "--sweep-id", "initial", "--keyword", "software"],
    ["--keyword-status", "--sweep-id", "initial", "--prepare-keyword-tracking"],
])
def test_invalid_unbounded_or_ambiguous_plans_rejected(values):
    with pytest.raises(ValueError):
        subject.plan(args(*values))


def test_dry_run_is_offline_and_has_no_runtime_or_source(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("External call from dry run")
    monkeypatch.setattr(subject, "runtime", forbidden)
    monkeypatch.setattr(subject, "source_factory", forbidden)
    monkeypatch.setattr(subject.sys, "argv", ["run_prod2_selected.py", "--folder", "tmp/dry-only", "--dry-run"])
    assert subject.main() == 0
    assert '"dry_run": true' in capsys.readouterr().out


def test_business_dry_run_remains_offline_with_small_pilot_limits(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("External call from business dry run")
    monkeypatch.setattr(subject, "runtime", forbidden)
    monkeypatch.setattr(subject, "source_factory", forbidden)
    monkeypatch.setattr(subject.sys, "argv", ["run_prod2_selected.py", "--folder", "tmp/business-dry",
        "--business-scope", "digitales", "--years", "2026,2025", "--dry-run"])
    assert subject.main() == 0
    import json
    plan = json.loads(capsys.readouterr().out)
    assert plan["base_queries"] == 292 and plan["max_accepted"] == plan["max_llm1_calls"] == 10
    assert plan["business_plan"]["batches"][0]["primary_keyword_count"] == 25


def test_final_plan_stops_at_llm1_and_does_not_schedule_followups():
    plan = subject.plan(args("--all-keywords", "--years", "2026,2025"))
    assert plan["terminal_stage"] == "llm1"
    assert plan["document_scope"] == "prod2_selected_documents_v1"


def test_ficha_reading_context_is_opt_in_and_old_manifests_keep_their_plan():
    legacy = subject.plan(args())
    future = subject.plan(args("--llm1-ficha-context"))
    assert "llm1_ficha_context_policy" not in legacy
    assert future.pop("llm1_ficha_context_policy") == "prod2_llm1_ficha_context_v1"
    assert future == legacy


def test_opt_in_ficha_scope_mismatch_blocks_before_persistence_or_models(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "get_detail", lambda *a: {"detail": {"description": "CIRUGIA"}})
    def forbidden(*a, **kw):
        pytest.fail("Mismatched official scope must stop before writes/providers")
    monkeypatch.setattr(subject, "persist_detail", forbidden)
    monkeypatch.setattr(subject, "paid_document_selection", forbidden)
    monkeypatch.setattr(subject, "paid_llm1", forbidden)
    manifest = {"llm1_ficha_context_policy": subject.LLM1_FICHA_CONTEXT_POLICY}
    item = {"row": {"convocation_id": 1, "description": "SOFTWARE"}}
    with pytest.raises(ValueError, match="differs from the discovery"):
        subject.enrich(None, None, tmp_path, item, manifest, None, None, "synthetic")
    manifest["llm1_ficha_context_policy"] = "unknown-policy"
    with pytest.raises(ValueError, match="Unsupported"):
        subject.enrich(None, None, tmp_path, item, manifest, None, None, "synthetic")


@pytest.mark.parametrize("with_previews", [False, True])
@pytest.mark.parametrize("with_ficha_context", [False, True])
@pytest.mark.parametrize("analysis_pages", [1, 20, 21])
@pytest.mark.parametrize("new_reading", [False, True])
@pytest.mark.parametrize("storage_mode", ["official", "r2"])
def test_real_zip_inventory_selection_pdf_preparation_to_llm1_envelope_offline(
    tmp_path, monkeypatch, with_previews, with_ficha_context, analysis_pages, new_reading, storage_mode,
    archive_format="zip", commercial_taxonomy=False):
    """Exercise the integrated archive/selector/preparation route; mock only I/O/providers."""
    from buenapro_worker.prod2 import selected_extractor, document_previews
    from buenapro_worker.prod2.document_selection import JevDocumentSelector
    import httpx
    code = "00000000-0000-4000-8000-000000000001"
    writer = PdfWriter()
    for _ in range(analysis_pages):
        writer.add_blank_page(width=100, height=200)
    stream = io.BytesIO()
    writer.write(stream)
    pdf = stream.getvalue()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("carpeta/Bases integradas.pdf", pdf)
        z.writestr("Anexo formulario.pdf", pdf)
    if archive_format == "7z":
        from buenapro_worker.prod2 import sevenzip_schema
        monkeypatch.setattr(sevenzip_schema, "require_schema", lambda conn: None)
        import shutil
        import subprocess
        tool = shutil.which("bsdtar")
        if not tool:
            pytest.skip("Native bsdtar unavailable")
        source = tmp_path / "7z-fixture-input"
        (source / "carpeta").mkdir(parents=True)
        (source / "carpeta/Bases integradas.pdf").write_bytes(pdf)
        (source / "Anexo formulario.pdf").write_bytes(pdf)
        target = tmp_path / "7z-fixture.7z"
        subprocess.run([tool, "--format", "7zip", "-cf", str(target), "-C", str(source),
                        "carpeta/Bases integradas.pdf", "Anexo formulario.pdf"], check=True, timeout=10)
        archive = io.BytesIO(target.read_bytes())
    document = {"uuid": code, "filename": "Documentos." + archive_format, "type": "Bases", "stage": "Convocatoria",
                "published_at": "06/10/2026 10:00"}
    detail = {"documents": [document], "schedule": [{"stage": "Presentacion"}], "item_states": [],
              "document_inventory_complete": True, "description": "SOFTWARE"}
    monkeypatch.setattr(subject, "get_detail", lambda *a: {"detail": detail, "observed_at": datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr(subject, "persist_detail", lambda *a, **kw: {"detail_hash": "a" * 64})
    monkeypatch.setattr(subject, "decide", lambda *a: {"state": "unknown"})
    monkeypatch.setattr(subject, "save_participation_assessment", lambda *a, **kw: None)
    monkeypatch.setattr(subject, "record_document_download", lambda *a, **kw: None)
    def download(conn, folder, conv, parent, manifest, pacer):
        path = folder / ("original." + archive_format)
        path.write_bytes(archive.getvalue())
        return path, {"document_uuid": code, "sha256": hashlib.sha256(archive.getvalue()).hexdigest(),
                      "source_url": subject.METADATA_URL + "?id=" + code}
    monkeypatch.setattr(subject, "download", download)
    previews = []
    def preview(path, workdir, **kwargs):
        previews.append(path)
        return {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "preview_text": "SYNTHETIC REQUISITOS", "content_hash": "d" * 64}
    monkeypatch.setattr(document_previews, "document_preview", preview)
    def select(conn, folder, *, description, candidates, **kwargs):
        def response(request):
            answers = {}
            for index, candidate in enumerate(candidates):
                stem = f"d{index:04d}"
                is_principal = candidate["filename"].startswith("Bases")
                role = "integrated_bases" if is_principal else "form"
                answers[stem + "_role"] = {"type": "choice", "choice": role, "confidence": .95, "probabilities": {role: 1}}
                if kwargs["selector"].policy_version == subject.DOCUMENT_SELECTION_POLICY_V3:
                    action = "read" if is_principal else "omit"
                    probabilities = {"read": .98 if is_principal else .01,
                                     "omit": .01 if is_principal else .98, "inspect": .01}
                    answers[stem + "_action"] = {"type": "choice", "choice": action, "confidence": .98,
                                                 "probabilities": probabilities}
                else:
                    answers[stem + "_needed"] = {"type": "noul", "noul": .99 if is_principal else .01}
                    answers[stem + "_ambiguity"] = {"type": "noul", "noul": .01}
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 500, "output_tokens": 20}})
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            result = JevDocumentSelector("synthetic", client=http,
                policy_version=kwargs["selector"].policy_version).select(description, candidates)
        return result, False
    monkeypatch.setattr(subject, "paid_document_selection", select)
    uploads = []
    def upload(*a, **kw):
        uploads.append(kw)
        return {"storage_key": "synthetic"}
    monkeypatch.setattr(subject, "upload_preview", upload)
    monkeypatch.setattr(subject, "reuse_completed_read", lambda *a, **kw: None)
    expected_model = "gemini-2.5-flash-lite" if analysis_pages > 20 else "gemini-3.1-flash-lite"
    model_calls = []
    class Extractor:
        supports_selected_input_policies = True
        def __init__(self, settings, prepared, folder, before_request=None, *, local_text_result=None):
            assert prepared["page_count"] == analysis_pages
            self.prepared = prepared
            from buenapro_worker.prod2.input_policy import schema_for_input
            assert ("llm1_commercial_taxonomy" in prepared["selection_context"]) == commercial_taxonomy
            assert ("commercial_classification" in schema_for_input(
                "bases_service", prepared["selection_context"]).model_fields) == commercial_taxonomy
            assert (local_text_result is not None) == new_reading
            if new_reading:
                assert prepared["llm1_input"]["route"] == "pdf"
            self.settings = settings
            model_calls.append(("transport", settings.gemini_model))
            self.prompts = {"bases_service": "synthetic"}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def extract(self, *a, **kw):
            commercial = ({"commercial_classification": {"taxonomy_version": "commercial_taxonomy_v1",
                "primary_area": "sin_clasificar", "secondary_areas": [], "work_types": [],
                "specialties": [], "support_status": "needs_review", "evidence": []}}
                if commercial_taxonomy else {})
            suffix = "_commercial_taxonomy_v1" if commercial_taxonomy else ""
            return SimpleNamespace(raw_json={"requirements": {"provider": [{"tipo": "specific_experience",
                "descripcion": "SYNTHETIC SUPPORT", "obligatorio": True}]}, "participation": {}, **commercial},
                requires_human_review=False, model=self.settings.gemini_model,
                prompt_version=("prod2_qualification_focus_bases_service_prompt_v1" if new_reading else "bases_extraction_v2") + suffix,
                schema_version=("prod2_qualification_focus_bases_service_schema_v1" if new_reading else "bases_extraction_schema_v2") + suffix, prompt_hash="b" * 64,
                input_tokens=100, output_tokens=10, cost_usd=.00004)
    monkeypatch.setattr(selected_extractor, "SelectedGeminiExtractor", Extractor)
    def paid(conn, folder, conv, request_hash, settings, profile, callback):
        model_calls.append(("reservation", settings.gemini_model))
        bundle = callback()
        assert bundle["model"] == settings.gemini_model
        assert bundle["cache_key"] == "prod2:llm1:" + request_hash
        return 1, bundle, False
    monkeypatch.setattr(subject, "paid_llm1", paid)
    persisted = []
    def persist(conn, **kw):
        persisted.append(kw)
        context = kw["extraction"]
        assert context["model"] == expected_model and context["allowed_models"] == [expected_model]
        assert context["requires_human_review"] is True
        assert context["document_scope"] == "prod2_selected_documents_v1"
        assert context["document_set"][0]["source_kind"] == "archive_member"
        assert context["document_set"][0]["member_path"] == "carpeta/Bases integradas.pdf"
        assert context["original_sha256"] == hashlib.sha256(archive.getvalue()).hexdigest()
        assert context["document_set"][0]["original_sha256"] == hashlib.sha256(pdf).hexdigest()
        assert context["document_set_hash"] == subject.canonical_hash(context["document_set_identity"])
        if commercial_taxonomy:
            assert context["selection_context"]["llm1_commercial_taxonomy"] == "commercial_taxonomy_v1"
            assert context["raw_extraction_json"]["commercial_classification"]["primary_area"] == "sin_clasificar"
            from buenapro_worker.prod2.pdf_preparation import selected_preparation_identity
            old_context = {k: v for k, v in context["selection_context"].items() if k != "llm1_commercial_taxonomy"}
            old_identity = selected_preparation_identity(context["document_set"], old_context,
                max_source_bytes=context["document_set_identity"]["max_source_bytes"])
            assert subject.canonical_hash(old_identity) != context["document_set_hash"]
        if with_ficha_context:
            assert context["selection_context"]["ficha_description"] == detail["description"]
        else:
            assert "ficha_description" not in context["selection_context"]
        return {"extraction_id": 1, "requires_human_review": True}
    monkeypatch.setattr(subject, "persist_completed_llm1", persist)
    class TestSettings(SimpleNamespace):
        def model_copy(self, *, update):
            return TestSettings(**{**vars(self), **update})
    settings = TestSettings(gemini_model="gemini-3.1-flash-lite", gemini_fallback_model="gemini-2.5-flash",
                            openai_api_key="synthetic-offline")
    options = args("--folder", str(tmp_path), "--cache-folder", str(tmp_path / "cache"),
                   "--document-storage", storage_mode,
                   *(["--llm1-commercial-taxonomy", "commercial_taxonomy_v1"] if commercial_taxonomy else []),
                   *(["--archive-7z"] if archive_format == "7z" else []),
                   *([] if new_reading else ["--llm1-scope", "legacy", "--llm1-input", "pdf"]))
    manifest = subject.plan(options) | {"profile_id": "00000000-0000-4000-8000-000000000002"}
    if with_previews:
        manifest.update(document_previews=True, selector_policy_version=subject.DOCUMENT_SELECTION_POLICY_V3)
    if with_ficha_context:
        manifest["llm1_ficha_context_policy"] = subject.LLM1_FICHA_CONTEXT_POLICY
    item = {"row": {"convocation_id": 1, "description": "SOFTWARE"}, "query": {"object_type": "service"}}
    limits = subject.RunLimits(max_accepted=10, max_candidates=100, max_jev_calls=100, max_llm1_calls=10)
    result = subject.enrich(None, settings, tmp_path, item, manifest, subject.SharedPacer(2), limits, "synthetic")
    assert result["status"] == "llm1_complete" and result["analysis_page_count"] == analysis_pages
    assert result["llm1_model"] == expected_model
    assert result["llm1_model_routing"]["page_count"] == analysis_pages
    assert settings.gemini_model == "gemini-3.1-flash-lite"
    assert all(model == expected_model for _, model in model_calls)
    assert len(result["selected_ids"]) == len(result["excluded_ids"]) == 1
    assert result["documents"][0]["source_url"].endswith(code)
    assert len(persisted) == 1
    assert len(uploads) == (1 if storage_mode == "r2" else 0)
    assert (tmp_path / "preparation-cache").exists() == (storage_mode == "official")
    assert len(previews) == (2 if with_previews else 0)
    if with_previews:
        assert result["selector_policy_version"] == subject.DOCUMENT_SELECTION_POLICY_V3
        context = persisted[0]["extraction"]["selection_context"]
        assert all(row["preview_text"] == "SYNTHETIC REQUISITOS" for row in context["candidates"])
        assert all(row["member_sha256"] == row["original_sha256"] for row in context["candidates"])
    replay = subject.enrich(None, settings, tmp_path, item, manifest, subject.SharedPacer(2), limits, "synthetic")
    assert replay["document_set_hash"] == result["document_set_hash"]
    assert replay["status"] == "llm1_complete" and len(persisted) == 2
    assert len(uploads) == (2 if storage_mode == "r2" else 0)
    if storage_mode == "official":
        paid_bundle = {k: v for k, v in persisted[-1]["extraction"].items()
                       if k not in {"allowed_models", "evaluation_id"}}
        monkeypatch.setattr(subject, "reuse_completed_read", lambda *a: {
            "evaluation_id": 1, "bundle": paid_bundle, "request_hash": replay["llm1_request_hash"],
            "model": expected_model, "routing": replay["llm1_model_routing"],
            "page_count": analysis_pages, "document_set_hash": replay["document_set_hash"],
            "source_document_manifest": replay["source_document_manifest"]})
        def forbidden(*a, **kw):
            raise AssertionError("An exact paid read must skip preparation, upload and generation")
        monkeypatch.setattr(subject, "prepare_selected_document_set", forbidden)
        monkeypatch.setattr(subject, "paid_llm1", forbidden)
        monkeypatch.setattr(subject, "upload_preview", forbidden)
        previous_calls = len(model_calls)
        reused = subject.enrich(None, settings, tmp_path, item, manifest, subject.SharedPacer(2), limits, "synthetic")
        assert reused["llm1_reused_before_preparation"] is True and reused["llm1_cost_usd"] == 0
        assert reused["llm1_receipt_hash"] == subject.canonical_hash(paid_bundle)
        assert reused["source_document_manifest"] == replay["source_document_manifest"]
        assert len(model_calls) == previous_calls and len(persisted) == 3


def test_commercial_opt_in_cli_to_preparation_schema_and_paid_identity_offline(tmp_path, monkeypatch):
    test_real_zip_inventory_selection_pdf_preparation_to_llm1_envelope_offline(
        tmp_path, monkeypatch, with_previews=False, with_ficha_context=False,
        analysis_pages=1, new_reading=True, storage_mode="official", commercial_taxonomy=True)


def test_official_cleanup_closes_and_replays_without_paid_document_reentry(tmp_path, monkeypatch):
    # Isolate this synthetic repository from pytest's external "current" aliases.
    # A real external alias must protect its files, as the cleanup helper tests verify.
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    case = tmp_path / "123"
    case.mkdir()
    code = "00000000-0000-4000-8000-000000000001"
    original = case / (code + ".pdf")
    original.write_bytes(b"%PDF-own")
    subject.private_write(case / (code + ".download.private.json"), {"document_uuid": code,
        "sha256": hashlib.sha256(original.read_bytes()).hexdigest(), "bytes": original.stat().st_size})
    record = {"accepted": True, "convocation_id": 123, "status": "llm1_complete",
        "document_set_hash": "a" * 64, "document_storage_policy": subject.OFFICIAL_LINKS_POLICY}
    subject.private_write(case / "outcome.private.json", record)
    def verification(conn, private, outcome):
        assert subject.read_private(private / "outcome.private.json") == outcome
        return {"pg_verified": True, "convocation_id": 123, "status": "llm1_complete",
            "document_set_hash": "a" * 64, "active_payments": 0, "usage_unknown_attempts": 0,
            "remote_cleanup_pending": 0, "remote_cleanup_unknown": False}
    monkeypatch.setattr(subject, "verify_completed_storage", verification)
    manifest = {"document_storage_policy": subject.OFFICIAL_LINKS_POLICY}
    result = subject.cleanup_finished_case(None, case, record, manifest)
    assert result["deleted_bytes"] == 8 and not original.exists()
    assert subject.read_private(case / "outcome.private.json") == record
    second = subject.cleanup_finished_case(None, case, record, manifest)
    assert second["deleted_bytes"] == 0 and second["cumulative_deleted_bytes"] == 8
    assert len(list(case.glob("temporary-cleanup-check-*.private.json"))) == 2
    with pytest.raises(RuntimeError, match="durable outcome"):
        subject.enrich(None, None, case, None, manifest, None, None, None)
    class Stage:
        pending, acked = True, []
        def next(self):
            if not self.pending: return None
            self.pending = False
            return "123", {"row": {"convocation_id": 123}}
        def ack(self, value): self.acked.append(value)
    @contextmanager
    def connect(*a, **kw): yield None
    def forbidden(*a, **kw): raise AssertionError("Completed outcome cannot call sources/models")
    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject, "paid_jev", forbidden)
    monkeypatch.setattr(subject, "enrich", forbidden)
    monkeypatch.setattr(subject, "STOP", threading.Event())
    stage = Stage()
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, manifest, tmp_path, None, None, None)
    assert stage.acked == ["123"]


def test_cleanup_readback_failure_preserves_success_and_original(tmp_path, monkeypatch):
    record = {"convocation_id": 123, "status": "llm1_complete"}
    original = tmp_path / "own.pdf"
    original.write_bytes(b"own")
    def failed(*a): raise ValueError("Private detail must not reach console")
    monkeypatch.setattr(subject, "verify_completed_storage", failed)
    result = subject.cleanup_finished_case(None, tmp_path, record,
        {"document_storage_policy": subject.OFFICIAL_LINKS_POLICY})
    assert result["status"] == "retained_verification_failed" and result["error_type"] == "ValueError"
    assert original.exists() and record["status"] == "llm1_complete"
    assert "Private detail" not in str(result)


def test_frozen_snapshot_replays_identical_inputs_and_rejects_conflicting_stage(tmp_path):
    path = tmp_path / "snapshot.private.json"
    subject.snapshot(path, {"frozen": True})
    subject.snapshot(path, {"frozen": True})
    with pytest.raises(ValueError, match="Frozen stage"):
        subject.snapshot(path, {"frozen": False})


def test_multikeyword_sweep_skips_only_verified_empty_scopes(tmp_path, monkeypatch):
    from test_prod2_keyword_sweep import Factory
    factory = Factory([])
    monkeypatch.setattr(subject, "source_factory", lambda pacer: factory())
    monkeypatch.setattr(subject, "STOP", threading.Event())
    manifest = subject.plan(args("--keyword", "software", "--keyword", "licencia")) | {
        "resolved_years": [2026], "published_from": "2026-04-06", "published_to": "2026-10-06"}
    limits = subject.RunLimits(max_accepted=10, max_candidates=100, max_jev_calls=100, max_llm1_calls=10)
    reports, proofs = subject.run_sweeps(manifest, tmp_path, subject.SharedPacer(2), None, limits)
    assert len(reports) == 4 and len(proofs) == 2
    assert factory.sessions == 4  # two sessions per scope, zero per individual keyword
    assert all(row["coverage_complete"] and row["coverage_kind"] == "verified_empty_scope_observed" for row in reports)


@pytest.mark.parametrize("fail_batch", [False, True])
def test_all_results_cached_replay_batches_business_payloads_and_halts_on_lost_response(
        tmp_path, monkeypatch, fail_batch):
    from test_prod2_keyword_sweep import Factory, row, no_http
    from buenapro_worker.prod2.keyword_sweep import sweep_query
    manifest = subject.plan(args("--keyword", "software", "--all-results")) | {
        "resolved_years": [2026], "objects": ["service"], "probe_empty_scopes": False,
        "published_from": "2026-04-06", "published_to": "2026-10-06",
        "business_plan": {"keyword_index": {"software": {"batch_id": "synthetic-digital"}}}}
    query = subject.planned_queries(manifest)[0]
    cache = tmp_path / "queries" / subject.digest(query)
    first = sweep_query(query, source_factory=Factory([row(index) for index in range(5)]),
        emit=lambda _: False, folder=cache)
    assert first["coverage_complete"]
    monkeypatch.setattr(subject, "source_factory", lambda _: no_http())
    monkeypatch.setattr(subject, "STOP", threading.Event())
    class Stage:
        def __init__(self): self.batches = []
        def stage(self, *a): raise AssertionError("Cached all-results replay must batch")
        def stage_many(self, entries):
            self.batches.append(entries)
            if fail_batch: raise ConnectionError("Lost batch response")
            return [False] * len(entries)
    stage = Stage()
    limits = subject.RunLimits(max_accepted=None, max_candidates=None, max_jev_calls=None, max_llm1_calls=None)
    reports, _ = subject.run_sweeps(manifest, tmp_path, None, stage, limits)
    assert subject.STOP.is_set() is fail_batch
    assert all(r["coverage_complete"] is (not fail_batch) for r in reports)
    entries = [entry for batch in stage.batches for entry in batch]
    assert [e[0] for e in entries] == [str(2000 + i) for i in range(2 if fail_batch else 5)]
    assert all(e[1]["discovery_business_context"] == {"batch_id": "synthetic-digital"} for e in entries)
    assert limits.seen == 0


def test_source_403_halts_remaining_queries_and_processing(tmp_path, monkeypatch):
    from buenapro_worker.prod2.client import Prod2ProtocolError
    calls = []
    @contextmanager
    def source(pacer):
        class Source:
            def search(self, *a, **kw):
                calls.append(kw)
                raise Prod2ProtocolError("PROD2 HTTP status 403")
        yield Source()
    monkeypatch.setattr(subject, "source_factory", source)
    monkeypatch.setattr(subject, "STOP", threading.Event())
    manifest = subject.plan(args("--keyword", "software", "--keyword", "licencia")) | {
        "resolved_years": [2026, 2025], "published_from": "2026-04-06", "published_to": "2026-10-06"}
    limits = subject.RunLimits(max_accepted=10, max_candidates=100, max_jev_calls=100, max_llm1_calls=10)
    reports, proofs = subject.run_sweeps(manifest, tmp_path, subject.SharedPacer(2), None, limits)
    assert len(calls) == 1 and subject.STOP.is_set()
    assert not any(row["coverage_complete"] for row in reports)
    assert proofs[0]["errors"][0]["code"] == "source_403"


def test_sevenzip_opt_in_previews_selection_preparation_read_and_exact_reuse_offline(tmp_path, monkeypatch):
    test_real_zip_inventory_selection_pdf_preparation_to_llm1_envelope_offline(
        tmp_path, monkeypatch, with_previews=True, with_ficha_context=True, analysis_pages=1,
        new_reading=True, storage_mode='official', archive_format='7z')


def test_sevenzip_new_policy_is_explicit_and_frozen_runner_remains_exact(tmp_path):
    options = args('--folder', str(tmp_path))
    legacy = subject.plan(options)
    assert 'archive_inventory_policy' not in legacy
    subject.private_write(tmp_path / 'manifest.private.json', {**legacy, 'requested': legacy})
    options.archive_7z = True
    with pytest.raises(ValueError, match='new folder.*archive'):
        subject.plan(options)
    assert subject.read_private(tmp_path / 'manifest.private.json')['requested'] == legacy
    (tmp_path / 'manifest.private.json').unlink()
    modern = subject.plan(options)
    assert modern['archive_inventory_policy'] == subject.POLICY_7Z
    subject.private_write(tmp_path / 'manifest.private.json', {**modern, 'requested': modern})
    options.archive_7z = False
    assert subject.plan(options) == modern


@pytest.mark.parametrize('policy', [None, {}, True, 'unknown'])
def test_invalid_archive_policy_blocks_enrichment_before_any_source(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(subject, 'get_detail', lambda *a: pytest.fail('must validate before source'))
    with pytest.raises(ValueError, match='invalid_archive_inventory_policy'):
        subject.enrich(None, None, tmp_path, None, {'archive_inventory_policy': policy}, None, None, None)


def test_direct_sevenzip_download_requires_frozen_opt_in_before_http(tmp_path, monkeypatch):
    parent = {'filename': 'BASES.7z', 'document_uuid': '00000000-0000-4000-8000-000000000001'}
    monkeypatch.setattr(subject, 'download_document', lambda *a, **k: pytest.fail('No HTTP without opt-in'))
    with pytest.raises(ValueError, match='Unsupported official source format'):
        subject.download(None, tmp_path, 1, parent, {}, None)
