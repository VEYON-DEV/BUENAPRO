from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from buenapro_worker.prod2 import handoff_guards as subject
from buenapro_worker.prod2.selected_control import LimitReached, RunLimits
from buenapro_worker.prod2.selected_payments import AuditedJevClient
from buenapro_worker.prod2.selected_extractor import SelectedGeminiExtractor

UUID = "12345678-1234-4234-8234-123456789abc"
OTHER = "22345678-1234-4234-8234-123456789abc"


def guard(folder, **extra):
    return subject.HandoffGuards(folder, **({"binding": {"scope": "frozen"}, "max_candidates": 2,
        "max_new_documents": 2, "stop": threading.Event()} | extra))


@pytest.mark.parametrize("state", ["closed", "adjudicado", "cancelado", "desierto", "suspendido"])
def test_closed_ficha_is_never_admitted_to_document_or_model_work(state):
    value = subject.lifecycle_gate({"state": state, "confirmed_open": False})
    assert value["status"] == "closed_without_reading"
    assert not value["requires_human_review"] and value["new_document_requests"] == 0


@pytest.mark.parametrize("state,confirmed", [("unknown", False), ("pending_registration", False),
    ("registered_only", False), ("submission_open", False), ("unknown", True), ("closed", True)])
def test_unconfirmed_or_contradictory_opening_does_not_allow_paid_work(state, confirmed):
    assert subject.lifecycle_gate({"state": state, "confirmed_open": confirmed}) is not None


@pytest.mark.parametrize("state", ["registration_open", "submission_open"])
def test_only_positively_observed_open_window_advances(state):
    assert subject.lifecycle_gate({"state": state, "confirmed_open": True}) is None


def test_document_admission_is_distinct_from_requests_and_survives_restart(tmp_path):
    control = guard(tmp_path)
    control.admit_document(UUID)
    control.bind_document(UUID, "a" * 64)
    control.bind_document(UUID, "a" * 64)
    control.admit_document(UUID, member="bases.pdf", archive_sha256="a" * 64)
    control.bind_document(UUID, "b" * 64, member="bases.pdf", archive_sha256="a" * 64)
    assert control.report()["document_admissions"] == 2
    with pytest.raises(LimitReached, match="documents admission"):
        control.admit_document(OTHER)
    resumed = guard(tmp_path, max_new_documents=3)
    resumed.bind_document(UUID, "a" * 64)
    assert resumed.report()["document_admissions"] == 2
    resumed.admit_document(OTHER)
    with pytest.raises(ValueError, match="source hash changed"):
        resumed.bind_document(UUID, "c" * 64)


def test_concurrent_candidate_admissions_are_exact_bounded_and_replayable(tmp_path):
    control = guard(tmp_path)
    def admit(value):
        try:
            control.admit_candidate(value)
            return True
        except LimitReached:
            return False
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(admit, range(1, 21))) == 2
    resumed = guard(tmp_path, max_candidates=3)
    assert resumed.report()["candidate_admissions"] == 2
    resumed.admit_candidate(99)
    assert resumed.report()["candidate_admissions"] == 3


def test_unknown_dispatch_blocks_all_stages_and_cannot_be_cleared_by_restart(tmp_path):
    control = guard(tmp_path)
    control.observe_attempt({"provider": "jev", "attempt_id": "owned", "usage_known": False})
    assert control.stop.is_set()
    for action in (control.before_request, lambda: control.admit_candidate(1), lambda: control.admit_document(UUID)):
        with pytest.raises(LimitReached, match="dispatch stopped"):
            action()
    resumed = guard(tmp_path)
    assert resumed.stop.is_set() and resumed.report()["dispatch_stopped"]
    marker = json.loads(resumed.marker.read_text())
    assert marker["cost_usd"] is None and marker["attempt_id"] == "owned"
    assert "token" not in marker
    with pytest.raises(ValueError, match="binding changed"):
        guard(tmp_path, binding={"scope": "changed"})


@pytest.mark.parametrize("attempt", [
    {"provider": "jev", "usage_known": True},
    {"provider": "gemini", "model_call_started": False, "provider_usage_known": False},
    {"provider": "embedding", "request_sent": False, "usage_known": False},
])
def test_known_usage_and_confirmed_presend_failure_do_not_stop_dispatch(tmp_path, attempt):
    control = guard(tmp_path)
    control.observe_attempt(attempt)
    assert not control.stop.is_set() and not control.marker.exists()


def test_source_access_block_is_durable_and_sanitized(tmp_path):
    control = guard(tmp_path)
    control.stop_access(403)
    assert guard(tmp_path).report()["dispatch_stopped"]
    assert json.loads(control.marker.read_text())["reason"] == "source_access_blocked"


@pytest.mark.parametrize("sent", [False, True])
def test_embedding_wrapper_stops_before_uncertain_payment_persistence(tmp_path, sent):
    from buenapro_worker.prod2.embedding_client import EmbeddingRequestError
    control = guard(tmp_path)
    client = SimpleNamespace(embed=lambda *a, **k: (_ for _ in ()).throw(
        EmbeddingRequestError("synthetic", request_sent=sent)), close=lambda: None)
    with pytest.raises(EmbeddingRequestError):
        control.embedding_client(client).embed(["synthetic"])
    assert control.stop.is_set() is sent


def test_deadline_is_literal_aware_and_future_not_invented():
    assessment = {"state": "registration_open", "confirmed_open": True,
        "as_of": "2026-10-20T08:00:00+00:00", "reason_codes": [],
        "evidence": {"windows": {"submission": {"end": {"at": "2026-10-21T23:59:00-05:00"}}}}}
    assert subject.lifecycle_fields(assessment)["closes_at"] == "2026-10-21T23:59:00-05:00"
    assert subject.lifecycle_fields(assessment)["matching_eligible"]
    for invalid in (None, "2026-10-21", "2026-10-21T23:59:00", "2026-10-19T23:59:00-05:00"):
        assessment["evidence"]["windows"]["submission"]["end"]["at"] = invalid
        projected = subject.lifecycle_fields(assessment)
        assert projected["closes_at"] is None and not projected["matching_eligible"]


def test_jev_unknown_usage_stops_before_typed_response_is_consumed(tmp_path):
    control = guard(tmp_path)
    limits = RunLimits(max_accepted=10, max_candidates=10, max_jev_calls=10, max_llm1_calls=10)
    calls = []
    def response(request):
        calls.append("sent")
        return httpx.Response(200, json={"answer": "synthetic", "usage": {}})
    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        client = AuditedJevClient(tmp_path / "case", limits, client=http,
            after_attempt=control.observe_attempt)
        assert client.post("https://offline.invalid").status_code == 200
        assert control.stop.is_set() and control.marker.exists()
        with pytest.raises(LimitReached):
            control.before_request()
    assert calls == ["sent"]


def test_gemini_generation_timeout_stops_before_persistence_or_cleanup(tmp_path):
    control = guard(tmp_path)
    def fail(**kwargs):
        raise TimeoutError("synthetic")
    reader = SelectedGeminiExtractor.__new__(SelectedGeminiExtractor)
    reader.folder = tmp_path
    reader._state = {"attempt": {"attempt_id": "owned"}, "generation_started": False,
        "before_request": lambda: None, "after_attempt": control.observe_attempt,
        "actual": SimpleNamespace(models=SimpleNamespace(generate_content=fail))}
    with pytest.raises(TimeoutError):
        reader._generate({"model": "synthetic", "config": SimpleNamespace(system_instruction="frozen")})
    assert control.stop.is_set()
    assert (tmp_path / "owned.started.private.json").exists()


def test_historical_canonical_reading_precedes_other_payments(monkeypatch):
    from buenapro_worker.prod2 import prior_reading_guard
    seen = []
    prior = {"status": "llm1_complete", "llm1_prior_preserved": True}
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", lambda *a, **k: seen.append("prior") or prior)
    class Connection:
        def execute(self, sql, params):
            seen.append("identity")
            return SimpleNamespace(fetchone=lambda: {"id_procedimiento": 2, "object_type": "good"})
    assert subject.preserve_by_id(Connection(), {"row": {"convocation_id": 1,
        "procedure_id": 2, "object_type": "good"}}, profile_id="frozen") == prior
    assert seen == ["identity", "prior"]


def test_historical_other_profile_or_uncertain_reservation_is_held_before_affinity(monkeypatch):
    from buenapro_worker.prod2 import prior_reading_guard
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", lambda *a, **k: None)
    class Connection:
        def execute(self, sql, params):
            return SimpleNamespace(fetchone=lambda: None,
                fetchall=lambda: [{"stage": "llm1", "reservation_id": "old", "status": "uncertain"}])
    held = subject.preserve_by_id(Connection(), {"row": {"convocation_id": 1,
        "procedure_id": 2, "object_type": "good"}}, profile_id="frozen")
    assert held["status"] == "needs_review" and held["llm1_prior_preserved"]
    assert held["prior_reading_preserved"]["source_calls"] == 0


def test_current_canonical_identity_cannot_be_silently_rebound():
    class Connection:
        def execute(self, sql, params):
            return SimpleNamespace(fetchone=lambda: {"id_procedimiento": 999, "object_type": "good"})
    with pytest.raises(ValueError, match="identity conflicts"):
        subject.preserve_by_id(Connection(), {"row": {"convocation_id": 1,
            "procedure_id": 2, "object_type": "good"}}, profile_id="frozen")


@pytest.mark.parametrize("status,code,expected", [("RESOURCE_EXHAUSTED", 429, (429, "RESOURCE_EXHAUSTED")),
    ("secret arbitrary body", "429", (None, None))])
def test_gemini_error_callback_preserves_only_whitelisted_machine_metadata(tmp_path, status, code, expected):
    control = guard(tmp_path)
    error = RuntimeError("credential and provider body must never enter the receipt")
    error.code, error.status = code, status
    reader = SelectedGeminiExtractor.__new__(SelectedGeminiExtractor)
    reader.folder = tmp_path
    reader._state = {"attempt": {"attempt_id": "owned"}, "generation_started": False,
        "before_request": lambda: None, "after_attempt": control.observe_attempt,
        "actual": SimpleNamespace(models=SimpleNamespace(generate_content=lambda **k: (_ for _ in ()).throw(error)))}
    with pytest.raises(RuntimeError):
        reader._generate({"model": "synthetic", "config": SimpleNamespace(system_instruction="frozen")})
    marker = json.loads(control.marker.read_text())
    assert (marker["http_status"], marker["provider_error_code"]) == expected
    assert "credential" not in control.marker.read_text() and "secret" not in control.marker.read_text()


@pytest.mark.parametrize("reason", ["source_has_no_usable_text", "incomplete_source_text",
    "retrieval_context_needs_expansion_or_review"])
def test_typed_document_review_requires_correct_exact_local_receipt(tmp_path, reason):
    from buenapro_worker.prod2.embedding_runtime import EmbeddingPreparationReview
    error = EmbeddingPreparationReview("synthetic")
    assert subject.document_preparation_review(error, tmp_path) is None
    value = {"reason": reason, "corpus_hash": "a" * 64, "full_pdf_fallback": False}
    if reason == "retrieval_context_needs_expansion_or_review":
        value.update(retrieval_hash="b" * 64, embedding_cost_usd=0.03)
    else:
        value.update(unresolved_pages=[], provider_calls=0)
    path = tmp_path / "embedding-preparation-review.private.json"
    path.write_text(json.dumps(value))
    proof = subject.document_preparation_review(error, tmp_path)
    assert proof["reason"] == reason and len(proof["receipt_sha256"]) == 64
    assert set(proof) == {"reason", "receipt_sha256"}
    assert subject.document_preparation_review(RuntimeError("synthetic"), tmp_path) is None
    value["full_pdf_fallback"] = True
    path.write_text(json.dumps(value))
    assert subject.document_preparation_review(error, tmp_path) is None
