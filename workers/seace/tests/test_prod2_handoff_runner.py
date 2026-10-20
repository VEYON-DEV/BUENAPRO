from contextlib import contextmanager
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


def options(package, *extra):
    # The synthetic package is outside this real repository. Bind its absolute
    # source paths just as an externally located, explicitly approved map would.
    inventory = json.loads(package["inventory"].read_text())
    inventory["input_bindings"] = {
        str(package["report"]): file_sha(package["report"]),
        str(package["handoff"]): file_sha(package["handoff"])}
    sha = write(package["inventory"], inventory)
    update_selection(package, lambda value: value.update(inventory_sha256=sha))
    return ["--folder", str(package["root"] / "reading-new"),
        "--mapping-handoff", str(package["handoff"]), "--selection", str(package["selection"]),
        "--selection-sha256", package["selection_sha"], "--max-new-documents", "10",
        "--max-candidates", "10", "--max-accepted", "10", "--max-jev-calls", "30",
        "--max-llm1-calls", "10", "--llm1-fallback", "none", "--preserve-prior-readings", *extra]


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
    assert outcome["status"] == "needs_review" and outcome["error_type"] == "RuntimeError"
    assert "reading_gate" not in outcome


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
