from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import handoff_guards
from buenapro_worker.prod2.handoff_selection import file_sha
from buenapro_worker.prod2.selected_control import RunLimits
from test_prod2_handoff_selection import package, write, update_selection

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_handoff_script", ROOT / "scripts/run_prod2_selected.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


@pytest.fixture(autouse=True)
def stops(monkeypatch):
    monkeypatch.setattr(subject, "STOP", threading.Event())
    monkeypatch.setattr(subject, "EMBEDDING_DISPATCH_STOP", threading.Event())
    monkeypatch.setattr(subject, "HANDOFF_GUARD", None)


def fixture_binding_key(path):
    # Mirror the importer's canonical key convention for both pytest's system
    # temp directory and an explicit --basetemp located inside this checkout.
    path = path.resolve()
    try:
        return str(path.relative_to(subject.ROOT.resolve()))
    except ValueError:
        return str(path)


def options(package, *extra):
    inventory = json.loads(package["inventory"].read_text())
    inventory["input_bindings"] = {
        fixture_binding_key(package["report"]): file_sha(package["report"]),
        fixture_binding_key(package["handoff"]): file_sha(package["handoff"])}
    sha = write(package["inventory"], inventory)
    update_selection(package, lambda value: value.update(inventory_sha256=sha))
    return ["--folder", str(package["root"] / "reading-new"),
        "--mapping-handoff", str(package["handoff"]), "--selection", str(package["selection"]),
        "--selection-sha256", package["selection_sha"], "--max-new-documents", "10",
        "--max-candidates", "10", "--max-accepted", "10", "--max-jev-calls", "30",
        "--max-llm1-calls", "10", "--llm1-fallback", "none", "--preserve-prior-readings", *extra]


def test_fixture_bindings_match_importer_for_inside_and_outside_checkout():
    assert fixture_binding_key(subject.ROOT / "tmp/synthetic-mapping/report.private.json") == (
        "tmp/synthetic-mapping/report.private.json")
    outside = subject.ROOT.parent / "synthetic-external-mapping/report.private.json"
    assert fixture_binding_key(outside) == str(outside.resolve())


def test_id_plan_and_finite_budget_expansion_preserve_frozen_paid_inputs(package):
    argv = options(package)
    first = subject.plan(subject.parser().parse_args(argv))
    second_args = subject.parser().parse_args([*argv, "--max-new-documents", "20",
        "--max-candidates", "20", "--max-accepted", "20", "--max-jev-calls", "60", "--max-llm1-calls", "20"])
    second = subject.plan(second_args)
    assert first == second
    assert first["handoff_selection"]["candidate_ids"] == [123]
    assert first["from_date"] == "2026-10-04" and first["to_date"] == "2026-10-20"
    assert not first["probe_empty_scopes"] and "keyword_tracking" not in first
    assert "max_candidates" not in first
    assert subject.execution_policy(second_args)["max_new_documents"] == 20


@pytest.mark.parametrize("extra", [
    ["--all-results"], ["--keyword", "software"], ["--sweep-id", "old"],
    ["--embedding-resume-from", "old"], ["--preflight"], ["--from-date", "2026-10-01"],
    ["--llm1-fallback", "luna"], ["--max-new-documents", "0"],
])
def test_handoff_cannot_sweep_adopt_old_queue_expand_dates_or_remove_guards(package, extra):
    with pytest.raises(ValueError):
        subject.plan(subject.parser().parse_args(options(package, *extra)))


def test_id_dry_run_has_zero_sweeps_and_no_runtime_or_embedding_warmup(package, monkeypatch, capsys):
    def forbidden(*a, **kw):
        raise AssertionError("No network, providers or database in dry-run")
    argv = options(package, "--dry-run")
    monkeypatch.setattr(sys, "argv", ["selected", *argv])
    monkeypatch.setattr(subject, "runtime", forbidden)
    monkeypatch.setattr(subject, "resolve_years", forbidden)
    monkeypatch.setattr(subject, "run_sweeps", forbidden)
    assert subject.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["base_queries"] == 0 and report["dry_run"]
    assert report["handoff_selection"]["candidate_ids"] == [123]


@pytest.mark.parametrize("extra", [["--runtime-recovery", "/offline/receipt.json"],
    ["--runtime-recovery-sha256", "a" * 64],
    ["--runtime-recovery", "/offline/receipt.json", "--runtime-recovery-sha256", "a" * 64]])
def test_runtime_recovery_requires_the_pair_and_explicit_id_mode(extra):
    args = subject.parser().parse_args(["--folder", "tmp/offline-reader", *extra])
    with pytest.raises(ValueError, match="Runtime recovery requires"):
        subject.plan(args)


@pytest.mark.parametrize("sha", ["", "not-a-sha", "A" * 64])
def test_runtime_recovery_rejects_malformed_sha_before_loading_handoff(package, sha):
    args = subject.parser().parse_args(options(package, "--runtime-recovery", "/offline/receipt.json",
        "--runtime-recovery-sha256", sha))
    with pytest.raises(ValueError, match="Runtime recovery requires"):
        subject.plan(args)


@pytest.mark.parametrize("use_recovery", [False, True])
def test_both_cli_loads_delegate_exact_recovery_args_without_changing_frozen_inputs(
        package, monkeypatch, capsys, use_recovery):
    from buenapro_worker.prod2.handoff_selection import load_selection as original_load
    calls = []
    def load(*args, **kwargs):
        calls.append((args, kwargs.copy()))
        return original_load(*args, root=kwargs["root"])
    # The CLI contract is isolated from the compatibility validator's own tests;
    # the real helper validates authorization and approved code/receipt hashes.
    monkeypatch.setitem(sys.modules, "buenapro_worker.prod2.handoff_runtime_recovery", SimpleNamespace(load_selection=load))
    argv = options(package)
    baseline = subject.plan(subject.parser().parse_args(argv))
    folder = package["root"] / "reading-new"
    frozen = {**baseline, "requested": baseline, "run_id": folder.name}
    frozen_path = folder / "manifest.private.json"
    write(frozen_path, frozen)
    before = frozen_path.read_bytes()
    extra = (["--runtime-recovery", str(package["root"] / "approved-recovery.private.json"),
        "--runtime-recovery-sha256", "b" * 64] if use_recovery else [])
    monkeypatch.setattr(sys, "argv", ["selected", *argv, *extra, "--dry-run"])
    monkeypatch.setattr(subject, "runtime", lambda *a, **k: pytest.fail("dry-run must remain offline"))
    calls.clear()
    assert subject.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert len(calls) == 2
    for args, kwargs in calls:
        assert args == (package["handoff"], package["selection"], package["selection_sha"])
        assert kwargs == {"root": subject.ROOT, "reader_folder": folder,
            "recovery_path": package["root"] / "approved-recovery.private.json" if use_recovery else None,
            "recovery_sha256": "b" * 64 if use_recovery else None}
    assert all(report[key] == value for key, value in baseline.items())
    assert "runtime_recovery" not in report and "runtime_recovery_sha256" not in report
    assert frozen_path.read_bytes() == before


def test_recovery_report_context_is_separate_opt_in_metadata(monkeypatch, tmp_path):
    calls = []
    expected = {"authorization_sha256": "a" * 64, "new_code_commit": "b" * 40,
        "runtime_bindings": {"synthetic.py": "c" * 64}, "frozen_binding_identity_only": True}
    def report_context(*args, **kwargs):
        calls.append((args, kwargs))
        return expected
    monkeypatch.setitem(sys.modules, "buenapro_worker.prod2.handoff_runtime_recovery",
        SimpleNamespace(report_context=report_context))
    absent = subject.parser().parse_args(["--folder", str(tmp_path)])
    assert subject.runtime_recovery_report_context(absent, tmp_path) is None and not calls
    args = subject.parser().parse_args(["--folder", str(tmp_path), "--runtime-recovery",
        str(tmp_path / "authorization.private.json"), "--runtime-recovery-sha256", "a" * 64])
    assert subject.runtime_recovery_report_context(args, tmp_path) == expected
    assert calls == [((args.runtime_recovery, "a" * 64), {"root": subject.ROOT, "reader_folder": tmp_path})]


class Stage:
    def __init__(self, item):
        self.item, self.acks, self.claimed = item, [], False
    def next(self):
        if self.claimed:
            return None
        self.claimed = True
        return str(self.item["row"]["convocation_id"]), self.item
    def ack(self, identity):
        self.acks.append(identity)


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    @contextmanager
    def connection(*a, **kw):
        yield object()
    monkeypatch.setattr(subject.psycopg, "connect", connection)
    control = RunLimits(max_accepted=10, max_candidates=10, max_jev_calls=30, max_llm1_calls=10)
    control.handoff_guard = handoff_guards.HandoffGuards(tmp_path, binding={"scope": "frozen"},
        max_candidates=10, max_new_documents=10, stop=subject.STOP)
    item = {"row": {"convocation_id": 123, "procedure_id": 456, "description": "TABLERO ELÉCTRICO",
        "object_type": "good"}, "query": {"keyword": "tablero"}, "page_number": 1,
        "observed_at": "2026-10-20T08:00:00+00:00"}
    stage = Stage(item)
    return tmp_path, control, stage, {"profile_id": "frozen", "commercial_profiles": []}


def run_consumer(consumer):
    folder, control, stage, manifest = consumer
    subject.consume(SimpleNamespace(database_url="offline"), stage, manifest, folder, None, control, "offline")
    path = folder / "123/outcome.private.json"
    return json.loads(path.read_text()) if path.exists() else None


def assessment(state="submission_open"):
    return {"state": state, "confirmed_open": state == "submission_open",
        "as_of": "2026-10-20T08:00:00+00:00", "reason_codes": [],
        "evidence": {"windows": {"submission": {"end": {"at": "2026-10-21T23:59:00-05:00"}}}}}


def test_prior_uncertain_never_reaches_source_or_affinity(consumer, monkeypatch):
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: {
        "status": "needs_review", "llm1_prior_preserved": True, "requires_human_review": True})
    monkeypatch.setattr(subject, "handoff_detail", lambda *a, **k: pytest.fail("historical uncertainty must not fetch"))
    monkeypatch.setattr(subject, "paid_jev", lambda *a, **k: pytest.fail("historical uncertainty must not pay"))
    outcome = run_consumer(consumer)
    assert outcome["status"] == "needs_review" and consumer[2].acks == ["123"]


@pytest.mark.parametrize("state", ["closed", "unknown", "pending_registration"])
def test_fresh_closed_or_unknown_ficha_stops_before_affinity_or_documents(consumer, monkeypatch, state):
    seen = []
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: seen.append("history") or None)
    monkeypatch.setattr(subject, "handoff_detail", lambda *a, **k: seen.append("ficha") or {"detail": {"schedule": []}})
    monkeypatch.setattr(subject, "decide", lambda *a, **k: assessment(state))
    monkeypatch.setattr(subject, "paid_jev", lambda *a, **k: pytest.fail("closed ficha must not pay"))
    monkeypatch.setattr(subject, "download", lambda *a, **k: pytest.fail("closed ficha must not download"))
    outcome = run_consumer(consumer)
    assert seen == ["history", "ficha"] and consumer[2].acks == ["123"]
    assert not outcome["confirmed_open"] and not outcome["matching_eligible"]
    assert outcome["status"] == ("closed_without_reading" if state == "closed" else "opening_not_confirmed")


@pytest.mark.parametrize("state", ["submission_open", "closed"])
def test_prior_valid_reading_refreshes_metadata_only_and_exports_matching_gate(consumer, monkeypatch, state):
    seen = []
    prior = {"status": "llm1_complete", "llm1_prior_preserved": True,
        "prior_reading_preserved": {"canonical": {"document_uuid": "official"}}}
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: seen.append("history") or prior)
    monkeypatch.setattr(subject, "handoff_detail", lambda *a, **k: seen.append("ficha") or {
        "observed_at": "2026-10-20T08:00:00+00:00", "detail": {"schedule": [],
            "document_inventory_complete": True, "documents": [{"uuid": "official"}]}})
    monkeypatch.setattr(subject, "decide", lambda *a, **k: assessment(state))
    monkeypatch.setattr(subject, "persist_detail", lambda *a, **k: {"detail_hash": "known"})
    monkeypatch.setattr(subject, "save_participation_assessment", lambda *a, **k: None)
    monkeypatch.setattr(subject, "paid_jev", lambda *a, **k: pytest.fail("paid read must be preserved"))
    monkeypatch.setattr(subject, "download", lambda *a, **k: pytest.fail("no PDF for preserved reading"))
    outcome = run_consumer(consumer)
    assert seen == ["history", "ficha"] and outcome["status"] == "llm1_complete"
    assert outcome["llm1_prior_preserved"] and not outcome["requires_fresh_lifecycle"]
    assert outcome["matching_eligible"] is (state == "submission_open")
    assert outcome["closes_at"] == "2026-10-21T23:59:00-05:00"


def test_budget_deferral_preserves_pending_payload_without_terminal_outcome(consumer, monkeypatch):
    folder, limits, stage, manifest = consumer
    limits.handoff_guard.max_candidates = 1
    limits.handoff_guard.admit_candidate(999)
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: pytest.fail("no new candidate slot"))
    assert run_consumer(consumer) is None
    assert not stage.acks and limits.handoff_guard.report()["budget_deferred"]


def test_source_403_sets_durable_handoff_stop(consumer, monkeypatch):
    monkeypatch.setattr(subject, "HANDOFF_GUARD", consumer[1].handoff_guard)
    subject.source_hooks(lambda _: None)["response"][0](SimpleNamespace(status_code=403))
    assert subject.STOP.is_set() and consumer[1].handoff_guard.report()["dispatch_stopped"]


def test_only_exact_bounded_lookup_missing_is_a_per_id_hold_without_models(consumer, monkeypatch):
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: None)
    monkeypatch.setattr(subject, "get_detail", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError(handoff_guards.BOUNDED_ID_NOT_LOCATED)))
    monkeypatch.setattr(subject, "paid_jev", lambda *a, **k: pytest.fail("unlocated ID must not pay"))
    monkeypatch.setattr(subject, "download", lambda *a, **k: pytest.fail("unlocated ID must not download"))
    outcome = run_consumer(consumer)
    assert outcome["status"] == "opening_not_confirmed" and outcome["requires_human_review"]
    assert outcome["requires_fresh_lifecycle"] and not outcome["matching_eligible"]
    assert "error_type" not in outcome and consumer[2].acks == ["123"]
    receipt = consumer[0] / "123/bounded-native-identity-review.private.json"
    assert outcome["reading_gate"]["reason"] == "bounded_native_identity_not_located"
    assert outcome["reading_gate"]["receipt_sha256"] == file_sha(receipt)
    assert json.loads(receipt.read_text())["provider_calls"] == 0
    assert not consumer[1].handoff_guard.report()["dispatch_stopped"]


@pytest.mark.parametrize("message", ["Accepted source row changed; a new Jev version is required",
    handoff_guards.BOUNDED_ID_NOT_LOCATED + " "])
def test_changed_identity_or_nonexact_runtime_error_is_not_a_missing_id_hold(consumer, monkeypatch, message):
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: None)
    monkeypatch.setattr(subject, "get_detail", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
    outcome = run_consumer(consumer)
    assert outcome is None and not consumer[2].acks and subject.STOP.is_set()
    receipt = subject.read_private(consumer[1].handoff_guard.marker)
    assert receipt["reason"] == "unexpected_local_exception" and receipt["error_type"] == "RuntimeError"


@pytest.mark.parametrize("kind", ["attribute", "value", "preparation_without_valid_receipt"])
def test_unexpected_handoff_error_stops_dispatch_without_ack_or_terminal_review(consumer, monkeypatch, kind):
    from buenapro_worker.prod2.embedding_runtime import EmbeddingPreparationReview
    error = {"attribute": AttributeError, "value": ValueError,
        "preparation_without_valid_receipt": EmbeddingPreparationReview}[kind]("secret-text-must-not-be-recorded")
    calls = []
    next_job = consumer[2].next
    monkeypatch.setattr(consumer[2], "next", lambda: calls.append("reserve") or next_job())
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: (_ for _ in ()).throw(error))
    monkeypatch.setattr(subject, "handoff_detail", lambda *a, **k: pytest.fail("no source after local failure"))
    local = consumer[0] / "123/pending-input.private.json"
    write(local, {"synthetic": "unique pending input"})
    before = local.read_bytes()
    assert run_consumer(consumer) is None
    assert subject.STOP.is_set() and not consumer[2].acks and local.read_bytes() == before
    marker = consumer[1].handoff_guard.marker
    data = marker.read_bytes()
    receipt = json.loads(data)
    assert receipt["reason"] == "unexpected_local_exception"
    assert receipt["convocation_id"] == 123 and receipt["phase"] == "consumer_case"
    assert receipt["error_type"] == type(error).__name__
    assert b"secret-text" not in data and "request_sent" not in receipt and "cost_usd" not in receipt
    assert run_consumer(consumer) is None and calls == ["reserve"]
    restarted_stop = threading.Event()
    handoff_guards.HandoffGuards(consumer[0], binding={"scope": "frozen"}, max_candidates=10,
        max_new_documents=10, stop=restarted_stop)
    assert restarted_stop.is_set() and marker.read_bytes() == data


def test_handoff_uncertain_provider_stop_preserves_original_reason_and_pending_payload(consumer, monkeypatch):
    guard = consumer[1].handoff_guard
    def uncertain(*a, **k):
        guard.observe_attempt({"provider": "jev", "request_sent": True, "usage_known": False,
            "attempt_id": "owned-synthetic-attempt"})
        raise ValueError("local parsing after uncertain response")
    monkeypatch.setattr(handoff_guards, "preserve_by_id", uncertain)
    assert run_consumer(consumer) is None and not consumer[2].acks
    marker = subject.read_private(guard.marker)
    assert marker["reason"] == "sent_provider_attempt_with_uncertain_usage"
    assert marker["request_sent"] is True and marker["cost_usd"] is None


def test_failed_handoff_consumer_future_stops_before_waiting_for_busy_consumer(consumer):
    busy, failed = Future(), Future()
    failed.set_exception(ValueError("secret future failure"))
    with pytest.raises(ValueError):
        subject.wait_consumers([busy, failed], consumer[1].handoff_guard)
    assert subject.STOP.is_set() and not busy.done() and not busy.cancelled()
    marker = subject.read_private(consumer[1].handoff_guard.marker)
    assert marker["phase"] == "consumer_worker" and "convocation_id" not in marker


def test_legacy_wait_keeps_existing_order_without_handoff(monkeypatch):
    order = []
    monkeypatch.setattr(subject, "as_completed", lambda *a: pytest.fail("legacy result order must stay unchanged"))
    subject.wait_consumers([SimpleNamespace(result=lambda: order.append(1)),
        SimpleNamespace(result=lambda: order.append(2))])
    assert order == [1, 2] and not subject.STOP.is_set()


def test_legacy_runtime_error_contract_is_unchanged_without_id_guard(consumer, monkeypatch):
    del consumer[1].handoff_guard
    monkeypatch.setattr(subject, "jev_request_hash", lambda *a, **k: "a" * 64)
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", lambda *a, **k: None)
    monkeypatch.setattr(subject, "AuditedJevClient", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(subject, "paid_jev", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError(handoff_guards.BOUNDED_ID_NOT_LOCATED)))
    outcome = run_consumer(consumer)
    assert outcome["status"] == "needs_review" and outcome["error_type"] == "RuntimeError"
    assert "reading_gate" not in outcome


def test_typed_economical_preparation_review_exports_only_bound_local_proof(consumer, monkeypatch):
    from buenapro_worker.prod2 import prior_reading_guard
    from buenapro_worker.prod2.embedding_runtime import EmbeddingPreparationReview
    consumer[3]["run_id"] = "synthetic-reader"
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: None)
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", lambda *a, **k: None)
    monkeypatch.setattr(subject, "handoff_detail", lambda *a, **k: {"detail": {"schedule": []}})
    monkeypatch.setattr(subject, "decide", lambda *a, **k: assessment())
    monkeypatch.setattr(subject, "jev_request_hash", lambda *a, **k: "a" * 64)
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", lambda *a, **k: None)
    monkeypatch.setattr(subject, "AuditedJevClient", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(subject, "paid_jev", lambda *a, **k: (
        "a" * 64, {"affinity_index": 90, "cost_usd": 0}, True))
    monkeypatch.setattr(subject.repo, "promote_accepted", lambda *a, **k: {"created": False})
    metadata = consumer[0] / "123/metadata.private.json"
    old_lifecycle = {**handoff_guards.lifecycle_fields(assessment("closed")),
        "temporal_observed_at": "2026-10-19T08:00:00+00:00", "detail_hash": "d" * 64}
    write(metadata, old_lifecycle)
    original_metadata = metadata.read_bytes()
    def review(conn, settings, folder, *a, **k):
        write(folder / "embedding-preparation-review.private.json", {
            "reason": "retrieval_context_needs_expansion_or_review", "corpus_hash": "b" * 64,
            "retrieval_hash": "c" * 64, "embedding_cost_usd": 0.01, "full_pdf_fallback": False})
        raise EmbeddingPreparationReview("synthetic")
    monkeypatch.setattr(subject, "enrich", review)
    outcome = run_consumer(consumer)
    assert outcome["status"] == "needs_review" and outcome["error_type"] == "EmbeddingPreparationReview"
    assert outcome["document_preparation_review"] == {
        "reason": "retrieval_context_needs_expansion_or_review",
        "receipt_sha256": file_sha(consumer[0] / "123/embedding-preparation-review.private.json")}
    assert not outcome["matching_eligible"] and not subject.STOP.is_set()
    assert outcome["temporal_observed_at"] == assessment()["as_of"]
    assert outcome["temporal_state"] == "submission_open" and outcome["confirmed_open"]
    assert metadata.read_bytes() == original_metadata


def test_handoff_local_stop_keeps_already_inflight_known_response_and_result(consumer, monkeypatch):
    from buenapro_worker.prod2 import prior_reading_guard
    consumer[3]["run_id"] = "synthetic-reader"
    monkeypatch.setattr(handoff_guards, "preserve_by_id", lambda *a, **k: None)
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", lambda *a, **k: None)
    monkeypatch.setattr(subject, "handoff_detail", lambda *a, **k: {"detail": {"schedule": []}})
    monkeypatch.setattr(subject, "decide", lambda *a, **k: assessment())
    monkeypatch.setattr(subject, "jev_request_hash", lambda *a, **k: "a" * 64)
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", lambda *a, **k: None)
    monkeypatch.setattr(subject, "AuditedJevClient", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(subject, "paid_jev", lambda *a, **k: (
        "a" * 64, {"affinity_index": 90, "cost_usd": 0}, True))
    monkeypatch.setattr(subject.repo, "promote_accepted", lambda *a, **k: {"created": False})
    began, finish = threading.Event(), threading.Event()
    response = consumer[0] / "123/known-response.private.json"
    def inflight(conn, settings, folder, *a, **k):
        began.set()
        assert finish.wait(5), "test must release the already in-flight response"
        write(response, {"usage_known": True, "cost_usd": 0.01, "response_hash": "b" * 64})
        return {"status": "llm1_complete", "llm1_cost_usd": 0.01}
    monkeypatch.setattr(subject, "enrich", inflight)
    with ThreadPoolExecutor(max_workers=1) as pool:
        live = pool.submit(run_consumer, consumer)
        try:
            assert began.wait(5)
            failed = Future()
            failed.set_exception(AttributeError("another consumer failed locally"))
            with pytest.raises(AttributeError):
                subject.wait_consumers([live, failed], consumer[1].handoff_guard)
        finally:
            finish.set()
        outcome = live.result(timeout=5)
    assert subject.STOP.is_set() and outcome["status"] == "llm1_complete"
    assert outcome["llm1_cost_usd"] == 0.01 and subject.read_private(response)["usage_known"]
    assert consumer[2].acks == ["123"]


def metadata_snapshot():
    return {"schedule": [{"stage": "Presentación de ofertas", "starts_at_text": "20/10/2026 10:00",
            "ends_at_text": "20/10/2026 11:00"}], "item_states": ["Convocado"],
        "document_scope": subject.SELECTED_DOCUMENTS_POLICY, "document_inventory_complete": True,
        "documents": [{"document_uuid": "official", "filename": "bases.pdf", "type": "Bases",
            "stage": "Convocatoria", "published_at": "2026-10-19", "source_url": "official-metadata"}],
        "detail_hash": "a" * 64, "document_storage_policy": subject.OFFICIAL_LINKS_POLICY,
        **handoff_guards.lifecycle_fields(assessment())}


def test_handoff_metadata_clock_replay_keeps_source_identity_and_old_bytes(tmp_path):
    old = metadata_snapshot()
    marker = tmp_path / "metadata.private.json"
    write(marker, old)
    original_bytes = marker.read_bytes()
    fresh = {**old, "temporal_observed_at": "2026-10-20T09:00:00+00:00"}
    proof = subject.snapshot_handoff_metadata(tmp_path, fresh)
    assert marker.read_bytes() == original_bytes
    assert subject.handoff_metadata_identity(old) == subject.handoff_metadata_identity(fresh)
    observation = tmp_path / proof["relative_path"]
    assert file_sha(observation) == proof["sha256"]
    assert json.loads(observation.read_text())["metadata"] == fresh
    assert subject.snapshot_handoff_metadata(tmp_path, fresh) == proof
    newer = {**fresh, "temporal_observed_at": "2026-10-20T09:30:00+00:00"}
    subject.snapshot_handoff_metadata(tmp_path, newer)
    assert len(list((tmp_path / "metadata-observations").glob("*.private.json"))) == 2


@pytest.mark.parametrize("change", [
    lambda value: value.update(detail_hash="b" * 64),
    lambda value: value["documents"][0].update(filename="changed.pdf"),
    lambda value: value["schedule"][0].update(ends_at_text="21/10/2026 11:00"),
    lambda value: value.update(item_states=["Cancelado"]),
])
def test_handoff_clock_replay_never_ignores_changed_source_content(tmp_path, change):
    old = metadata_snapshot()
    marker = tmp_path / "metadata.private.json"
    write(marker, old)
    before = marker.read_bytes()
    fresh = deepcopy(old)
    fresh["temporal_observed_at"] = "2026-10-20T09:00:00+00:00"
    change(fresh)
    with pytest.raises(ValueError, match="source metadata changed"):
        subject.snapshot_handoff_metadata(tmp_path, fresh)
    assert marker.read_bytes() == before and not (tmp_path / "metadata-observations").exists()


def test_new_handoff_metadata_baseline_is_static_and_legacy_snapshot_remains_strict(tmp_path):
    first = metadata_snapshot()
    subject.snapshot_handoff_metadata(tmp_path, first)
    marker = tmp_path / "metadata.private.json"
    assert json.loads(marker.read_text()) == subject.handoff_metadata_identity(first)
    before = marker.read_bytes()
    subject.snapshot_handoff_metadata(tmp_path, {**first, "temporal_observed_at": "2026-10-20T09:00:00+00:00"})
    assert marker.read_bytes() == before
    legacy = tmp_path / "legacy.private.json"
    subject.snapshot(legacy, first)
    with pytest.raises(ValueError, match="Frozen stage inputs changed"):
        subject.snapshot(legacy, {**first, "temporal_observed_at": "2026-10-20T09:00:00+00:00"})


def test_replayed_source_with_new_closed_lifecycle_stops_enrich_before_any_paid_or_document_work(tmp_path, monkeypatch):
    real_decide = subject.decide
    detail = {"procedure_id": 456, "convocatoria_id": 123, "selection_type": "Licitación Pública",
        "description": "TABLERO ELÉCTRICO", "item_states": ["Convocado"], "document_inventory_complete": True,
        "documents": [{"uuid": "official", "filename": "bases.pdf", "type": "Bases",
            "stage": "Convocatoria", "published_at": "2026-10-19"}],
        "schedule": [{"stage": "Registro de participantes", "starts_at_text": "19/10/2026 09:00",
            "ends_at_text": "20/10/2026 09:00"}, {"stage": "Presentación de ofertas",
            "starts_at_text": "20/10/2026 10:00", "ends_at_text": "20/10/2026 11:00"}]}
    open_time = datetime.fromisoformat("2026-10-20T15:30:00+00:00")
    closed_time = datetime.fromisoformat("2026-10-20T17:00:00+00:00")
    old = {"schedule": detail["schedule"], "item_states": detail["item_states"],
        "document_scope": subject.SELECTED_DOCUMENTS_POLICY, "document_inventory_complete": True,
        "documents": subject.official_inventory(detail), "detail_hash": "a" * 64,
        "document_storage_policy": subject.OFFICIAL_LINKS_POLICY,
        **handoff_guards.lifecycle_fields(real_decide(detail, open_time))}
    assert old["confirmed_open"]
    marker = tmp_path / "metadata.private.json"
    write(marker, old)
    before = marker.read_bytes()
    monkeypatch.setattr(subject, "decide", lambda value, now: real_decide(value, closed_time))
    monkeypatch.setattr(subject, "persist_detail", lambda *a, **k: {"detail_hash": "a" * 64})
    monkeypatch.setattr(subject, "save_participation_assessment", lambda *a, **k: None)
    def forbidden(*a, **k):
        pytest.fail("closed replay must not request source/documents/models")
    for name in ("get_detail", "download", "preview_candidates", "JevDocumentSelector", "prepare_selected_document_set"):
        monkeypatch.setattr(subject, name, forbidden)
    limits = SimpleNamespace(handoff_guard=SimpleNamespace(observe_attempt=forbidden))
    record = subject.enrich(object(), SimpleNamespace(gemini_model="gemini-3.1-flash-lite"), tmp_path,
        {"row": {"convocation_id": 123}}, {"document_storage_policy": subject.OFFICIAL_LINKS_POLICY},
        None, limits, "offline", capture={"detail": detail, "observed_at": closed_time.isoformat()})
    assert record["status"] == "closed_without_reading" and record["temporal_state"] == "closed"
    assert not record["confirmed_open"] and not record["matching_eligible"]
    assert marker.read_bytes() == before
    current = json.loads((tmp_path / record["metadata_observation"]["relative_path"]).read_text())
    assert current["metadata"]["temporal_state"] == "closed"
