"""Automatic evaluation (LLM2) in the PROD6 runtime: who is evaluated, how it is queued and how the
paid call is protected. Offline: Gemini is a mock; PostgreSQL tests run only with
BUENAPRO_TEST_DATABASE_URL and are always rolled back."""
import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from google.genai import errors as genai_errors

from buenapro_worker.jobs.analyze_match import analyze_match_job
from buenapro_worker.matching.substantive_policy import ModelOutputMismatch
from buenapro_worker.prod6 import backfill_evaluations, evaluation, runtime
from buenapro_worker.prod6.evaluation import AUTO_ORIGIN, INVALID_OUTCOME, enqueue_evaluations, skip_reason
from buenapro_worker.prod6.evaluation_guard import GuardedMatchAnalyzer
from buenapro_worker.prod6.reading import (
    GuardedReadingError, ProviderRejectedError, ReadingBlockedError,
)
from buenapro_worker.prod6.screening import POLICY_VERSION, record_decision
from buenapro_worker.queue.repository import Job
from buenapro_worker.seace.schemas import SearchItem
from buenapro_worker.settings import Settings

SECRET = "synthetic-private-token"
PRIVATE_TEXT = "NOMBRE-PRIVADO-DE-LA-EMPRESA"
ANALYSIS = {"veredicto": "verde", "score": 90, "resumen": "Cumple.", "acciones_recomendadas": [],
            "requisitos": [{"requisito": "Servidor", "categoria": "equipamiento", "estado": "cumple",
                            "critico": False, "source_index": 1, "evidence_explanation": "Declarado."}]}


def settings():
    return Settings(database_url="postgresql://unused", gemini_api_key=SECRET)


# --- skip_reason: one verdict per pair, free of charge -------------------------------------------

def pair(**fields):
    return {"decision": "accepted", "decided_by": "jev", "profile_active": True, "contract_open": True,
            "rule_enabled": True, "extraction_id": 7, "quality": "auto", "requires_human_review": False,
            "facets": 3, "match_id": None, "match_extraction_id": None, "hours_left": 48.0,
            "min_hours": 0} | fields


def test_a_jev_accepted_pair_with_a_usable_reading_is_evaluated():
    assert skip_reason(pair()) is None
    assert skip_reason(pair(hours_left=None)) is None  # No closing date: nothing to compare.


@pytest.mark.parametrize("fields, reason", [
    ({"decision": "rejected"}, "decision_not_accepted"),
    ({"decision": "pending"}, "decision_not_accepted"),
    ({"decision": "superseded"}, "decision_not_accepted"),
    ({"decided_by": "keywords"}, "decision_not_accepted"),
    ({"decided_by": "prefilter"}, "decision_not_accepted"),
    ({"profile_active": False}, "profile_inactive"),
    ({"contract_open": False}, "contract_closed"),
    ({"rule_enabled": False}, "rule_disabled"),
    ({"extraction_id": None}, "reading_missing"),
    ({"quality": "failed"}, "reading_failed"),
    ({"requires_human_review": True}, "reading_requires_review"),
    ({"facets": 0}, "no_requirements"),
    ({"hours_left": 3.5, "min_hours": 4}, "too_close_to_close"),
    ({"match_id": 1, "match_extraction_id": "7"}, "match_current"),
    ({"match_id": 1, "match_extraction_id": "6"}, "match_exists"),
    ({"match_id": 1, "match_extraction_id": None}, "match_exists"),
])
def test_skip_reasons(fields, reason):
    assert skip_reason(pair(**fields)) == reason


def test_exactly_the_minimum_hours_still_evaluates():
    assert skip_reason(pair(hours_left=4.0, min_hours=4)) is None


def test_a_pair_whose_answers_were_invalid_for_this_reading_is_not_sent_again():
    assert skip_reason(pair(invalid_answer=True)) == "model_output_invalid"
    assert skip_reason(pair(invalid_answer=True, match_id=1, match_extraction_id="7")) == "match_current"
    assert skip_reason(pair(invalid_answer=False)) is None


def test_a_match_from_an_older_reading_is_re_evaluated_only_when_asked_and_unknown_ones_never():
    stale = pair(match_id=1, match_extraction_id="6")
    assert skip_reason(stale, include_stale=True) is None
    assert skip_reason(pair(match_id=1, match_extraction_id=None), include_stale=True) == "match_exists"
    assert skip_reason(pair(match_id=1, match_extraction_id="7"), include_stale=True) == "match_current"


# --- the runtime queue accepts exactly one kind of analyze_match ------------------------------------

def scoped(profiles=("veyon", "din")):
    return runtime.ScopedRepository(MagicMock(), profile_id=profiles[0], batch_id="b",
                                    profile_ids=list(profiles))


def automatic(**fields):
    return {"id_contrato": 42, "profile_id": "din", "source": "automatic", "origin": AUTO_ORIGIN} | fields


@patch("buenapro_worker.queue.repository.JobRepository.enqueue", return_value=11)
def test_the_automatic_evaluation_of_a_runtime_company_is_queued_with_runtime_ownership(enqueue):
    repo = scoped()
    assert repo.enqueue("analyze_match", automatic(), queue_name="llm", priority=4,
                        dedup_key="analyze_match:din:42") == 11
    payload = enqueue.call_args.args[1]
    assert payload["source_runtime"] == runtime.NAMESPACE and payload["scope_profile_id"] == "veyon"
    assert enqueue.call_args.kwargs["dedup_key"] == f"{runtime.NAMESPACE}:veyon:analyze_match:din:42"
    assert enqueue.call_args.kwargs["max_attempts"] == 1


@pytest.mark.parametrize("payload", [
    {"id_contrato": 42, "profile_id": "din"},                                          # derive_summary's diff path
    automatic(origin="legacy_router"), automatic(source="manual"), automatic(profile_id="other"),
    automatic(profile_id=None), {"id_contrato": 42, "source": "automatic", "origin": AUTO_ORIGIN},
])
@patch("buenapro_worker.queue.repository.JobRepository.enqueue")
def test_any_other_llm2_job_stays_suppressed(enqueue, payload):
    repo = scoped()
    assert repo.enqueue("analyze_match", payload, queue_name="llm") is None
    enqueue.assert_not_called()
    assert repo.suppressed == {"analyze_match": 1}


@patch("buenapro_worker.queue.repository.JobRepository.enqueue")
def test_the_legacy_router_and_every_notification_job_stay_suppressed(enqueue):
    repo = scoped()
    for kind in ("route_contract_profiles", "send_notification", "send_alert", "prod4_extract"):
        assert repo.enqueue(kind, automatic()) is None
    enqueue.assert_not_called()


def test_the_free_io_executor_cannot_run_an_evaluation():
    job = Job(1, "analyze_match", "llm", automatic(), 1)
    with patch.object(runtime, "connect") as connect, pytest.raises(ValueError, match="free HTTP"):
        runtime.execute_io_job(MagicMock(), "veyon", job, Path("unused"))
    connect.assert_not_called()


# --- the hook after derive_summary -----------------------------------------------------------------

@patch("buenapro_worker.prod6.runtime.eligible_contract", return_value=True)
@patch("buenapro_worker.prod6.runtime.enqueue_evaluations")
@patch("buenapro_worker.prod6.runtime.derive_summary_job")
@pytest.mark.parametrize("review, queued", [(False, True), (True, False)])
def test_evaluations_are_queued_after_a_usable_summary_and_never_after_one_in_review(
        derive, enqueue, eligible, review, queued):
    derive.return_value = {"facets": 3, "diff": [], "requires_human_review": review}
    enqueue.return_value = {"candidates": 2, "enqueued": 1, "already_queued": 0, "daily_limited": 0,
                            "skipped": {"rule_disabled": 1}, "planned": []}
    repo = scoped()
    job = Job(1, "derive_summary", "match", {"id_contrato": 42, "extraction_id": 9, "batch_id": "b"}, 1)
    result = runtime.dispatch(MagicMock(), repo, job, Path("unused"))
    assert enqueue.called is queued
    if queued:
        assert enqueue.call_args.kwargs == {"profile_ids": ["veyon", "din"], "contract_id": 42,
                                            "include_stale": True}
        assert result["evaluations"]["enqueued"] == 1 and "planned" not in result["evaluations"]
    else:
        assert "evaluations" not in result


# --- dispatch of an evaluation job ---------------------------------------------------------------------

def evaluation_job(attempts=1, **fields):
    return Job(5, "analyze_match", "llm", automatic(profile_id="veyon") | {
        "batch_id": "b", "business_line_id": None} | fields, attempts)


def decided_row(**fields):
    return pair(decision_id=31, score="88.50") | fields


@pytest.fixture
def evaluator(monkeypatch, tmp_path):
    rows = [decided_row()]
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=True))
    monkeypatch.setattr(runtime, "profile_enabled", MagicMock(return_value=True))
    monkeypatch.setattr(runtime, "evaluation_rows", MagicMock(side_effect=lambda *a, **k: rows))
    analyze = MagicMock(return_value={"match_id": 3, "verdict": "verde", "score": 90})
    monkeypatch.setattr(runtime, "analyze_match_job", analyze)
    guarded = MagicMock(provider_answered=True)
    factory = MagicMock(return_value=guarded)
    monkeypatch.setattr(runtime, "GuardedMatchAnalyzer", factory)
    return SimpleNamespace(rows=rows, analyze=analyze, guarded=guarded, factory=factory, folder=tmp_path,
                           run=lambda job: runtime.dispatch(MagicMock(), scoped(), job, tmp_path))


def test_a_qualifying_job_pays_once_without_notifications_and_with_the_receipt_guard(evaluator):
    outcome = evaluator.run(evaluation_job())
    assert outcome == {"outcome": "evaluated", "match_id": 3, "verdict": "verde", "score": 90,
                       "provider_answered": True}
    options = evaluator.analyze.call_args.kwargs
    assert options["notifications"] is False and options["source"] == "automatic"
    assert options["analyzer"] is evaluator.guarded and options["id_contrato"] == 42
    assert options["extra_meta"] == {"evaluation_origin": AUTO_ORIGIN, "jev_decision_id": 31, "jev_score": 88.5}
    assert evaluator.factory.call_args.args[1] == evaluator.folder / "evaluations"
    evaluator.guarded.close.assert_called_once()


def test_a_job_claimed_before_may_only_replay_its_own_answer(evaluator):
    evaluator.run(evaluation_job(attempts=1))
    assert evaluator.factory.call_args.kwargs == {"allow_new_requests": True}
    evaluator.run(evaluation_job(attempts=2))
    assert evaluator.factory.call_args.kwargs == {"allow_new_requests": False}


@pytest.mark.parametrize("fields, reason", [
    ({"decision": "rejected"}, "decision_not_accepted"), ({"rule_enabled": False}, "rule_disabled"),
    ({"requires_human_review": True}, "reading_requires_review"), ({"contract_open": False}, "contract_closed"),
    ({"match_id": 1, "match_extraction_id": "7"}, "match_current"),
    ({"hours_left": 1.0, "min_hours": 24}, "too_close_to_close"),
])
def test_a_waiting_job_that_stopped_qualifying_costs_nothing(evaluator, fields, reason):
    evaluator.rows[0] = decided_row(**fields)
    assert evaluator.run(evaluation_job()) == {"outcome": "skipped", "reason": reason}
    evaluator.factory.assert_not_called()
    evaluator.analyze.assert_not_called()


def test_a_missing_decision_a_company_out_of_scope_or_paused_costs_nothing(evaluator, monkeypatch):
    evaluator.rows.clear()
    assert evaluator.run(evaluation_job())["reason"] == "decision_not_accepted"
    evaluator.rows.append(decided_row())
    assert evaluator.run(evaluation_job(profile_id="stranger")) == {
        "outcome": "skipped", "reason": "profile_out_of_scope"}
    evaluator.rows.clear()
    monkeypatch.setattr(runtime, "profile_enabled", MagicMock(return_value=False))
    evaluator.rows.append(decided_row())
    assert evaluator.run(evaluation_job())["reason"] == "profile_paused"
    evaluator.factory.assert_not_called()


def test_a_job_from_another_origin_is_refused_before_any_work(evaluator):
    job = evaluation_job()
    job = Job(job.id, job.job_type, job.queue_name, {**job.payload, "origin": "legacy_router"}, 1)
    with pytest.raises(ValueError, match="origin"):
        evaluator.run(job)
    evaluator.factory.assert_not_called()


def test_the_unchanged_guard_is_reported_and_is_not_a_provider_answer(evaluator):
    evaluator.analyze.return_value = {"skipped": "unchanged", "match_id": 3}
    evaluator.guarded.provider_answered = False
    outcome = evaluator.run(evaluation_job())
    assert outcome == {"outcome": "unchanged", "skipped": "unchanged", "match_id": 3}


def test_the_closed_contract_is_preserved_without_an_evaluation(evaluator, monkeypatch):
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=False))
    assert evaluator.run(evaluation_job()) == {"outcome": "closed_preserved"}
    evaluator.factory.assert_not_called()


def mismatch():
    return ModelOutputMismatch("requirement_coverage_changed", "Substantive requirement coverage/order changed")


def test_an_answer_that_skips_requirements_fails_only_that_pair_and_is_not_resent(evaluator):
    """Production 26/10: Gemini judged 4 of 8 requirements and the whole service stopped. With
    temperature 0 a resend returned the same answer, so the pair fails at once."""
    evaluator.analyze.side_effect = [mismatch()]
    outcome = evaluator.run(evaluation_job())
    assert outcome == {"outcome": INVALID_OUTCOME, "reason": "requirement_coverage_changed",
                       "extraction_id": 7, "provider_answered": True}
    assert evaluator.analyze.call_count == 1
    evaluator.guarded.close.assert_called_once()


def test_a_replayed_invalid_answer_is_reported_without_a_provider_answer(evaluator):
    evaluator.analyze.side_effect = [mismatch()]
    evaluator.guarded.provider_answered = False
    outcome = evaluator.run(evaluation_job(attempts=2))
    assert outcome == {"outcome": INVALID_OUTCOME, "reason": "requirement_coverage_changed", "extraction_id": 7}


def test_any_other_failure_still_stops_the_service(evaluator):
    evaluator.analyze.side_effect = ValueError("Contract not found: 42")
    with pytest.raises(ValueError, match="Contract not found"):
        evaluator.run(evaluation_job())


# --- GuardedMatchAnalyzer: the paid call and its receipts ----------------------------------------------

def reply(analysis=None, *, usage=(1200, 300, 40), finish=None, text=None):
    body = json.dumps(analysis or ANALYSIS) if text is None else text
    prompt, candidates, thoughts = usage
    return SimpleNamespace(
        text=body, candidates=[SimpleNamespace(finish_reason=finish)] if finish else [],
        usage_metadata=SimpleNamespace(prompt_token_count=prompt, candidates_token_count=candidates,
                                       thoughts_token_count=thoughts))


def rejection(code, status="RESOURCE_EXHAUSTED"):
    return genai_errors.ClientError(code, {"error": {"code": code, "message": SECRET, "status": status}})


@pytest.fixture
def gemini(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("buenapro_worker.matching.analyzer.genai.Client", MagicMock(return_value=client))
    return client.models.generate_content


def analyzer_in(folder, **options):
    return GuardedMatchAnalyzer(settings(), folder / "evaluations", **options)


def ask(analyzer, company=PRIVATE_TEXT):
    return analyzer.analyze(
        perfil={"razon_social": company, "ruc": "20999999991"},
        oportunidad={"codigo": "CM-1-2026", "descripcion": "Servidor", "entidad": "E", "resumen": {}},
        requisitos=[{"source_index": 1, "tipo": "equipment", "requisito": "Servidor",
                     "obligatorio": True, "detalle": {}}])


def receipts(folder, kind):
    return sorted((folder / "evaluations").glob(f"*.{kind}.private.json"))


def test_the_answer_is_saved_before_parsing_and_a_second_ask_replays_it_without_paying(gemini, tmp_path):
    gemini.return_value = reply()
    first = analyzer_in(tmp_path)
    result = ask(first)
    assert result.analysis.score == 90 and first.provider_answered is True
    # Output tokens include the thinking tokens the provider bills as output.
    assert (result.input_tokens, result.output_tokens) == (1200, 340)
    assert result.cost_usd == pytest.approx(1200 * 0.25 / 1e6 + 340 * 1.5 / 1e6)
    assert len(receipts(tmp_path, "started")) == len(receipts(tmp_path, "response")) == 1
    second = analyzer_in(tmp_path, allow_new_requests=False)  # Even a replay-only job gets its answer.
    replay = ask(second)
    assert replay.analysis == result.analysis and replay.cost_usd == result.cost_usd
    assert second.provider_answered is False and gemini.call_count == 1


def test_receipts_are_private_and_never_hold_the_request_payload(gemini, tmp_path):
    gemini.return_value = reply()
    ask(analyzer_in(tmp_path))
    folder = tmp_path / "evaluations"
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    for path in folder.glob("*.private.json"):
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert PRIVATE_TEXT not in path.read_text() and SECRET not in path.read_text()


def test_a_different_payload_is_a_different_request(gemini, tmp_path):
    gemini.return_value = reply()
    ask(analyzer_in(tmp_path))
    ask(analyzer_in(tmp_path), company="OTRA EMPRESA")
    assert gemini.call_count == 2 and len(receipts(tmp_path, "response")) == 2


@pytest.mark.parametrize("code", [400, 401, 402, 403, 429, 500, 503, 529])
def test_an_http_error_proves_nothing_was_billed_and_does_not_open_the_guard(gemini, tmp_path, code):
    gemini.side_effect = rejection(code)
    analyzer = analyzer_in(tmp_path)
    with pytest.raises(ProviderRejectedError) as raised:
        ask(analyzer)
    assert raised.value.code == code and analyzer.provider_answered is False
    folder = tmp_path / "evaluations"
    assert not (folder / "guard.json").exists() and receipts(tmp_path, "started") == []
    (receipt,) = receipts(tmp_path, "provider_error")
    saved = json.loads(receipt.read_text())
    assert saved["http_status"] == code and saved["cost_status"] == "no_response"
    assert SECRET not in receipt.read_text()


def test_after_an_http_error_the_same_request_may_be_sent_again_even_by_a_replay_only_job(gemini, tmp_path):
    gemini.side_effect = [rejection(429), reply()]
    with pytest.raises(ProviderRejectedError):
        ask(analyzer_in(tmp_path))
    retry = analyzer_in(tmp_path, allow_new_requests=False)
    assert ask(retry).analysis.score == 90 and gemini.call_count == 2
    assert receipts(tmp_path, "provider_error") == []  # The retry consumed it.


def test_the_http_error_receipt_authorizes_one_retry_only(gemini, tmp_path):
    gemini.side_effect = [rejection(503, "UNAVAILABLE"), TimeoutError(SECRET)]
    with pytest.raises(ProviderRejectedError):
        ask(analyzer_in(tmp_path))
    with pytest.raises(GuardedReadingError):  # The retry ends uncertain...
        ask(analyzer_in(tmp_path, allow_new_requests=False))
    with pytest.raises(ReadingBlockedError):  # ...so nothing may be sent again.
        ask(analyzer_in(tmp_path))
    assert gemini.call_count == 2


@pytest.mark.parametrize("failure", [TimeoutError(SECRET), ConnectionError(SECRET), KeyboardInterrupt(),
                                     RuntimeError(SECRET)])
def test_any_other_failure_after_sending_is_uncertain_and_blocks_every_new_send(gemini, tmp_path, failure):
    gemini.side_effect = failure
    with pytest.raises(GuardedReadingError) as raised:
        ask(analyzer_in(tmp_path))
    assert SECRET not in str(raised.value) and raised.value.__cause__ is None
    folder = tmp_path / "evaluations"
    assert json.loads((folder / "guard.json").read_text())["reason"] == "sent_request_without_durable_response"
    assert len(receipts(tmp_path, "started")) == 1 and receipts(tmp_path, "response") == []
    gemini.side_effect = None
    gemini.return_value = reply()
    for company in (PRIVATE_TEXT, "OTRA EMPRESA"):  # The same request and any other one.
        with pytest.raises(ReadingBlockedError):
            ask(analyzer_in(tmp_path), company=company)
    assert gemini.call_count == 1


def test_a_started_request_without_an_answer_blocks_others_and_opens_the_guard(gemini, tmp_path):
    gemini.return_value = reply()
    folder = tmp_path / "evaluations"
    folder.mkdir()
    (folder / ("0" * 64 + ".started.private.json")).write_text(json.dumps(
        {"version": 1, "request_key": "0" * 64, "state": "started", "started_at": "2026-10-25T00:00:00+00:00"}))
    with pytest.raises(ReadingBlockedError):
        ask(analyzer_in(tmp_path))
    assert json.loads((folder / "guard.json").read_text())["reason"] == "started_request_without_durable_response"
    gemini.assert_not_called()


def test_a_replay_only_job_without_its_own_receipts_cannot_send(gemini, tmp_path):
    gemini.return_value = reply()
    with pytest.raises(ReadingBlockedError, match="local recovery only"):
        ask(analyzer_in(tmp_path, allow_new_requests=False))
    gemini.assert_not_called()
    assert receipts(tmp_path, "started") == []


def test_a_known_answer_is_replayed_even_while_the_guard_is_open(gemini, tmp_path):
    gemini.return_value = reply()
    first = ask(analyzer_in(tmp_path))
    (tmp_path / "evaluations" / "guard.json").write_text("{}")
    assert ask(analyzer_in(tmp_path)).analysis == first.analysis and gemini.call_count == 1
    with pytest.raises(ReadingBlockedError):  # But no new request leaves.
        ask(analyzer_in(tmp_path), company="OTRA EMPRESA")


@pytest.mark.parametrize("answer, reason", [
    (dict(finish="FinishReason.MAX_TOKENS"), "durable_response_requires_local_review"),
    (dict(text="not json"), "durable_response_requires_local_review"),
    (dict(text='{"veredicto": "verde"}'), "durable_response_requires_local_review"),
    (dict(usage=(None, None, None)), "durable_response_requires_local_review"),
])
def test_an_unusable_answer_is_preserved_blocks_new_sends_and_is_never_paid_for_twice(
        gemini, tmp_path, answer, reason):
    gemini.return_value = reply(**answer)
    with pytest.raises(GuardedReadingError):
        ask(analyzer_in(tmp_path))
    folder = tmp_path / "evaluations"
    assert json.loads((folder / "guard.json").read_text())["reason"] == reason
    assert len(receipts(tmp_path, "response")) == 1
    with pytest.raises(GuardedReadingError):  # Replaying the same request neither pays nor passes.
        ask(analyzer_in(tmp_path))
    with pytest.raises(ReadingBlockedError):
        ask(analyzer_in(tmp_path), company="OTRA EMPRESA")
    assert gemini.call_count == 1


def test_each_answered_evaluation_paces_the_next_send_by_its_tokens(gemini, tmp_path, monkeypatch):
    from buenapro_worker.prod6 import evaluation_guard
    events = []
    pacer = MagicMock()
    pacer.wait.side_effect = lambda: events.append(("wait", len(receipts(tmp_path, "started"))))
    pacer.spent.side_effect = lambda i, o: events.append(("spent", i, o))
    monkeypatch.setattr(evaluation_guard, "GEMINI_PACER", pacer)
    gemini.return_value = reply()
    ask(analyzer_in(tmp_path))
    # The wait comes before the started receipt; the tokens (thinking included) after the answer.
    assert events == [("wait", 0), ("spent", 1200, 340)]
    ask(analyzer_in(tmp_path, allow_new_requests=False))  # A replay sends nothing and never waits.
    assert len(events) == 2


def test_a_rejected_evaluation_spends_nothing_and_keeps_its_quota_window(gemini, tmp_path, monkeypatch):
    from buenapro_worker.prod6 import evaluation_guard
    pacer = MagicMock()
    monkeypatch.setattr(evaluation_guard, "GEMINI_PACER", pacer)
    gemini.side_effect = genai_errors.ClientError(429, {"error": {
        "code": 429, "message": SECRET, "status": "RESOURCE_EXHAUSTED",
        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier"}]}]}})
    with pytest.raises(ProviderRejectedError) as rejected:
        ask(analyzer_in(tmp_path))
    assert (rejected.value.code, rejected.value.quota) == (429, "minute")
    pacer.wait.assert_called_once()
    pacer.spent.assert_not_called()


def models_sent(gemini):
    return [call.kwargs["model"] for call in gemini.call_args_list]


def test_a_saturated_evaluation_goes_once_to_the_fallback_model_and_is_replayed(gemini, tmp_path, monkeypatch):
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    gemini.side_effect = [genai_errors.ServerError(503, {"error": {"code": 503, "message": SECRET,
                                                                     "status": "UNAVAILABLE"}}), reply()]
    result = ask(analyzer_in(tmp_path))
    assert result.model == "gemini-3.5-flash-lite" and result.analysis.score == 90
    assert models_sent(gemini) == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    assert len(receipts(tmp_path, "provider_error")) == 1 and len(receipts(tmp_path, "response")) == 1
    replay = ask(analyzer_in(tmp_path, allow_new_requests=False))
    assert replay.model == "gemini-3.5-flash-lite" and gemini.call_count == 2


def test_quota_errors_do_not_use_the_fallback(gemini, tmp_path, monkeypatch):
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    gemini.side_effect = rejection(429)
    with pytest.raises(ProviderRejectedError):
        ask(analyzer_in(tmp_path))
    assert models_sent(gemini) == ["gemini-3.1-flash-lite"]


def test_a_replay_only_evaluation_never_starts_a_send_on_the_fallback(gemini, tmp_path, monkeypatch):
    saturated = genai_errors.ServerError(503, {"error": {"code": 503, "message": SECRET, "status": "UNAVAILABLE"}})
    gemini.side_effect = [saturated]
    with pytest.raises(ProviderRejectedError):
        ask(analyzer_in(tmp_path))  # Fallback off: only the main model's HTTP error receipt.
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    gemini.side_effect = [saturated]
    with pytest.raises(ProviderRejectedError):
        ask(analyzer_in(tmp_path, allow_new_requests=False))
    assert models_sent(gemini) == ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite"]


def test_a_tampered_answer_receipt_is_refused(gemini, tmp_path):
    gemini.return_value = reply()
    ask(analyzer_in(tmp_path))
    (path,) = receipts(tmp_path, "response")
    saved = json.loads(path.read_text())
    saved["text"] = saved["text"].replace("90", "99")
    path.write_text(json.dumps(saved))
    with pytest.raises(GuardedReadingError, match="does not match"):
        ask(analyzer_in(tmp_path))
    assert gemini.call_count == 1


def test_a_model_without_known_prices_is_refused_before_any_receipt(gemini, tmp_path):
    other = Settings(database_url="postgresql://unused", gemini_api_key=SECRET, gemini_model="gemini-unknown")
    with pytest.raises(ValueError, match="known local prices"):
        GuardedMatchAnalyzer(other, tmp_path / "evaluations").analyze(
            perfil={}, oportunidad={}, requisitos=[])
    gemini.assert_not_called()
    assert not list((tmp_path / "evaluations").glob("*.private.json"))


def test_the_plain_analyzer_is_unchanged(gemini):
    from buenapro_worker.matching.analyzer import MatchAnalyzer
    gemini.return_value = reply(usage=(1000, 200, 99))
    result = ask(MatchAnalyzer(settings()))
    assert (result.input_tokens, result.output_tokens) == (1000, 200)  # The legacy count, as before.


# --- PostgreSQL: who is queued, the rules, dedup, no notifications ---------------------------------------

DATABASE_URL = os.environ.get("BUENAPRO_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="BUENAPRO_TEST_DATABASE_URL not set")
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        conn.execute("SET TIME ZONE 'UTC'")
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def world(db):
    """A tenant with two companies (rules, an active line each) and one user who wants every alert."""
    tenant = db.execute("INSERT INTO tenants (name) VALUES ('auto-evaluation-test') RETURNING id").fetchone()["id"]
    companies = []
    for index in range(2):
        profile = db.execute(
            """INSERT INTO company_profiles (tenant_id, ruc, razon_social, identity_json)
               VALUES (%s, %s, %s, '{"descripcion": "Servidores"}') RETURNING id""",
            (tenant, f"2099999{random.randint(1000, 9999)}", f"{PRIVATE_TEXT}-{index}")).fetchone()["id"]
        db.execute("""INSERT INTO business_lines (profile_id, nombre, cubso_segmentos, keyword_phrases)
                      VALUES (%s, %s, '{43}', '{servidor}')""", (profile, f"Linea {index}"))
        db.execute("INSERT INTO automation_rules (profile_id) VALUES (%s)", (profile,))
        companies.append(str(profile))
    user = db.execute("INSERT INTO users (email) VALUES (%s) RETURNING id",
                      (f"{uuid4()}@example.invalid",)).fetchone()["id"]
    db.execute("INSERT INTO tenant_members (tenant_id, user_id) VALUES (%s, %s)", (tenant, user))
    db.execute("""INSERT INTO notification_preferences (tenant_id, user_id, channel, min_verdict)
                  VALUES (%s, %s, 'in_app', 'gris')""", (tenant, user))
    return SimpleNamespace(companies=companies, scoped=lambda: runtime.ScopedRepository(
        db, profile_id=companies[0], batch_id="test-batch", profile_ids=companies))


def rule(db, profile, **fields):
    assignments = ", ".join(f"{name} = %s" for name in fields)
    db.execute(f"UPDATE automation_rules SET {assignments} WHERE profile_id = %s", (*fields.values(), profile))


def contract(db, *, closes_in_hours=48, flagged=False, quality="auto", facets=2, read=True, state=2):
    """An open contract with a document, a current reading and its requirement facets."""
    number = random.randint(9_000_000_000, 9_900_000_000)
    db.execute(
        """INSERT INTO seace_contracts (id_contrato, codigo, anio, objeto_codigo, estado_codigo, descripcion,
             cubso_segmento, fec_fin_cotizacion, hash_search, raw_search_json)
           VALUES (%s, %s, 2026, 2, %s, 'Servicio de servidores', '43', %s, 'h', '{}'::jsonb)""",
        (number, f"TEST-{number}", state, None if closes_in_hours is None
         else NOW + timedelta(hours=closes_in_hours)))
    extraction = None
    if read:
        document = db.execute(
            """INSERT INTO contract_documents (id_contrato, id_contrato_archivo, categoria, filename,
                 sha256_original, seace_download_url) VALUES (%s, %s, 1, 'tdr.pdf', %s, 'https://example.invalid')
               RETURNING id""", (number, number, f"{number:064d}")).fetchone()["id"]
        extraction = db.execute(
            """INSERT INTO tdr_extractions (contract_document_id, model, prompt_version, schema_version,
                 raw_extraction_json, summary_json, requires_human_review, quality)
               VALUES (%s, 'm', 'p', 's', '{}'::jsonb, '{"descripcion_corta": "x"}'::jsonb, %s, %s) RETURNING id""",
            (document, flagged, quality)).fetchone()["id"]
        for index in range(facets):
            db.execute(
                """INSERT INTO requirement_facets (id_contrato, extraction_id, facet, label, required,
                     details_json, facet_hash) VALUES (%s, %s, 'equipment', %s, true, '{}'::jsonb, %s)""",
                (number, extraction, f"Servidor {index}", f"hash-{number}-{index}"))
    return number, extraction


def decide(db, number, profile, decision="accepted", *, decided_by="jev", contract_hash="c1",
           profile_hash="p1", score=80):
    item = SearchItem.model_validate({
        "idContrato": number, "desContratacion": f"TEST-{number}", "idObjetoContrato": 2,
        "desObjetoContrato": "Servicio de servidores", "idEstadoContrato": 2,
        "fecPublica": "01/10/2026 10:00:00", "fecFinCotizacion": "30/12/2026 10:00:00"})
    assert record_decision(
        db, item=item, segment=43, profile_id=profile, contract_hash=contract_hash, profile_hash=profile_hash,
        policy_version=POLICY_VERSION, prefilter_passed=True, prefilter_reasons=[], keyword_lines=[],
        decision=decision, decided_by=decided_by)
    db.execute("""UPDATE prod6_profile_decisions SET score = %s WHERE id_contrato = %s AND profile_id = %s
                  AND contract_hash = %s""", (score, number, profile, contract_hash))


def jobs(db, number=None):
    return db.execute(
        """SELECT payload, queue_name, priority, dedup_key, max_attempts, status FROM worker_jobs
           WHERE job_type = 'analyze_match' AND (%s::bigint IS NULL OR (payload->>'id_contrato')::bigint = %s)
           ORDER BY id""", (number, number)).fetchall()


@needs_db
def test_only_accepted_jev_decisions_are_queued_and_the_job_carries_runtime_ownership(db, world):
    veyon, din = world.companies
    number, extraction = contract(db)
    decide(db, number, veyon, "accepted")
    decide(db, number, din, "rejected")
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert stats["enqueued"] == 1 and stats["skipped"] == {"decision_not_accepted": 1}
    (job,) = jobs(db, number)
    assert job["queue_name"] == "llm" and job["priority"] == 4 and job["max_attempts"] == 1
    assert job["dedup_key"] == f"{runtime.NAMESPACE}:{veyon}:analyze_match:{veyon}:{number}"
    assert job["payload"]["source_runtime"] == runtime.NAMESPACE and job["payload"]["scope_profile_id"] == veyon
    assert job["payload"]["source"] == "automatic" and job["payload"]["origin"] == AUTO_ORIGIN
    assert (job["payload"]["profile_id"], job["payload"]["extraction_id"]) == (veyon, extraction)
    assert job["payload"]["jev_score"] == 80.0 and job["payload"]["business_line_id"]
    # The runtime claims it with its own whitelist.
    claimed = claim_evaluation(db, world)
    assert claimed.job_type == "analyze_match" and claimed.payload["profile_id"] == veyon


@needs_db
@pytest.mark.parametrize("state", ["pending", "superseded"])
def test_pending_and_superseded_decisions_are_never_evaluated(db, world, state):
    number, _ = contract(db)
    decide(db, number, world.companies[0], "pending")
    if state == "superseded":
        db.execute("UPDATE prod6_profile_decisions SET decision = 'superseded' WHERE id_contrato = %s", (number,))
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert stats["enqueued"] == 0 and jobs(db, number) == []


@needs_db
def test_the_newest_decision_rules_and_a_keyword_or_prefilter_acceptance_is_not_jev(db, world):
    veyon, din = world.companies
    number, _ = contract(db)
    decide(db, number, veyon, "accepted", contract_hash="old")
    decide(db, number, veyon, "rejected", contract_hash="new")  # The card changed and JEV now rejects.
    decide(db, number, din, "accepted", decided_by="keywords")
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert stats["enqueued"] == 0 and stats["skipped"] == {"decision_not_accepted": 2}


@needs_db
def test_a_company_outside_the_runtime_is_not_evaluated(db, world):
    number, _ = contract(db)
    decide(db, number, world.companies[1])
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies[:1], contract_id=number)
    assert stats["candidates"] == 0 and jobs(db, number) == []


@needs_db
@pytest.mark.parametrize("build, reason", [
    (dict(flagged=True), "reading_requires_review"), (dict(quality="failed"), "reading_failed"),
    (dict(read=False), "reading_missing"), (dict(facets=0), "no_requirements"),
    (dict(closes_in_hours=-2), "contract_closed"), (dict(state=3), "contract_closed"),
])
def test_a_reading_in_review_failed_missing_or_empty_and_a_closed_contract_are_not_evaluated(
        db, world, build, reason):
    number, _ = contract(db, **build)
    decide(db, number, world.companies[0])
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert stats["skipped"] == {reason: 1} and jobs(db, number) == []


@needs_db
def test_the_company_rules_enabled_and_minimum_hours(db, world):
    veyon, din = world.companies
    number, _ = contract(db, closes_in_hours=6)
    decide(db, number, veyon)
    decide(db, number, din)
    rule(db, din, enabled=False)
    rule(db, veyon, min_hours_before_close=12)
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert stats["skipped"] == {"rule_disabled": 1, "too_close_to_close": 1} and jobs(db, number) == []
    rule(db, veyon, min_hours_before_close=5)  # Just under six hours left is not fewer than five.
    rule(db, din, enabled=True)
    assert enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)["enqueued"] == 2


@needs_db
def test_a_company_without_a_rules_row_follows_the_legacy_defaults(db, world):
    veyon, _ = world.companies
    db.execute("DELETE FROM automation_rules WHERE profile_id = %s", (veyon,))
    number, _ = contract(db)
    decide(db, number, veyon)
    assert enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)["enqueued"] == 1


@needs_db
def test_the_daily_limit_counts_automatic_jobs_of_the_day_and_zero_means_no_limit(db, world):
    veyon, din = world.companies
    rule(db, veyon, max_daily_evaluations=2)
    rule(db, din, max_daily_evaluations=0)
    numbers = [contract(db)[0] for _ in range(4)]
    for number in numbers:
        decide(db, number, veyon)
        decide(db, number, din)
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, only_accepted=True,
                                limit=None)
    mine = [job for job in jobs(db) if job["payload"]["id_contrato"] in numbers]
    assert {veyon: 2, din: 4} == {profile: sum(job["payload"]["profile_id"] == profile for job in mine)
                                  for profile in (veyon, din)}
    assert stats["daily_limited"] == 2
    # Later the same day nothing more is queued for the limited company; the others are queued already.
    again = enqueue_evaluations(world.scoped(), profile_ids=world.companies, only_accepted=True)
    assert again["enqueued"] == 0 and again["already_queued"] == 4 and again["daily_limited"] == 4


@needs_db
def test_a_pair_already_queued_or_evaluated_is_not_queued_again(db, world):
    veyon, _ = world.companies
    number, extraction = contract(db)
    decide(db, number, veyon)
    assert enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)["enqueued"] == 1
    again = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert again["enqueued"] == 0 and again["already_queued"] == 1 and len(jobs(db, number)) == 1
    # The job finished and left a match for this very reading: nothing is paid for again.
    db.execute("UPDATE worker_jobs SET status = 'done' WHERE job_type = 'analyze_match'")
    db.execute("""INSERT INTO matches (profile_id, id_contrato, score, verdict, breakdown_json)
                  VALUES (%s, %s, 80, 'verde', %s::jsonb)""",
               (veyon, number, json.dumps({"meta": {"extraction_id": extraction}})))
    assert enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number,
                               include_stale=True)["skipped"] == {"match_current": 1}


@needs_db
def test_a_new_reading_re_evaluates_a_stale_match_but_a_match_of_unknown_origin_is_kept(db, world):
    veyon, din = world.companies
    number, extraction = contract(db)
    decide(db, number, veyon)
    decide(db, number, din)
    db.execute("""INSERT INTO matches (profile_id, id_contrato, score, verdict, breakdown_json)
                  VALUES (%s, %s, 80, 'verde', %s::jsonb)""",
               (veyon, number, json.dumps({"meta": {"extraction_id": extraction - 1}})))
    db.execute("""INSERT INTO matches (profile_id, id_contrato, score, verdict, breakdown_json)
                  VALUES (%s, %s, 80, 'verde', '{"meta": {"prompt_version": "jev_capacity_gemma_recommendations_v1"}}')""",
               (din, number))
    plain = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert plain["enqueued"] == 0 and plain["skipped"] == {"match_exists": 2}
    hooked = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number,
                                 include_stale=True)
    assert hooked["enqueued"] == 1 and hooked["skipped"] == {"match_exists": 1}
    assert jobs(db, number)[0]["payload"]["profile_id"] == veyon


@needs_db
def test_dry_run_writes_nothing_and_reports_what_it_would_queue(db, world):
    veyon, din = world.companies
    first, second = contract(db, closes_in_hours=30)[0], contract(db, closes_in_hours=10)[0]
    for number in (first, second):
        decide(db, number, veyon)
    decide(db, first, din)
    rule(db, din, enabled=False)
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, only_accepted=True, apply=False)
    assert stats["enqueued"] == 0
    assert all(job["payload"]["id_contrato"] not in (first, second) for job in jobs(db))
    planned = [item for item in stats["planned"] if item["id_contrato"] in (first, second)]
    assert [item["id_contrato"] for item in planned] == [second, first]  # Soonest closing first.
    assert all(item["profile_id"] == veyon for item in planned) and stats["skipped"]["rule_disabled"] >= 1


@needs_db
def test_the_backfill_dry_run_then_apply_then_nothing_new(db, world):
    veyon, din = world.companies
    number, _ = contract(db)
    decide(db, number, veyon)
    decide(db, number, din)
    dry = backfill_evaluations.backfill(db, profile_ids=world.companies)
    assert dry["mode"] == "dry-run" and dry["would_enqueue"] == 2 and dry["contracts"] == 1
    assert dry["by_profile"] == {veyon: 1, din: 1} and jobs(db, number) == []
    applied = backfill_evaluations.backfill(db, profile_ids=world.companies, apply=True)
    assert applied["mode"] == "apply" and applied["enqueued"] == 2 and len(jobs(db, number)) == 2
    repeated = backfill_evaluations.backfill(db, profile_ids=world.companies, apply=True)
    assert repeated["enqueued"] == 0 and repeated["already_queued"] == 2
    assert backfill_evaluations.backfill(db, profile_ids=world.companies)["would_enqueue"] == 0


@needs_db
def test_the_backfill_limit_keeps_the_soonest_closing_contracts(db, world):
    veyon, _ = world.companies
    late, soon = contract(db, closes_in_hours=40)[0], contract(db, closes_in_hours=5)[0]
    decide(db, late, veyon)
    decide(db, soon, veyon)
    result = backfill_evaluations.backfill(db, profile_ids=world.companies, apply=True, limit=1)
    assert result["enqueued"] == 1 and jobs(db, soon) and not jobs(db, late)


@needs_db
def test_the_backfill_requires_companies_and_a_positive_limit(db, world):
    with pytest.raises(ValueError):
        backfill_evaluations.backfill(db, profile_ids=[])
    with pytest.raises(ValueError):
        backfill_evaluations.backfill(db, profile_ids=world.companies, limit=0)


@needs_db
def test_the_hook_after_derive_summary_queues_through_the_real_dispatch(db, world):
    veyon, din = world.companies
    number, extraction = contract(db)
    decide(db, number, veyon)
    decide(db, number, din, "rejected")
    summary = {"facets": 2, "diff": [], "requires_human_review": False}
    with patch.object(runtime, "derive_summary_job", return_value=summary):
        result = runtime.dispatch(settings(), world.scoped(),
                                  Job(1, "derive_summary", "match", {"id_contrato": number,
                                                                    "extraction_id": extraction, "batch_id": "b"}, 1),
                                  Path("unused"))
    assert result["evaluations"]["enqueued"] == 1 and result["evaluations"]["skipped"] == {
        "decision_not_accepted": 1}
    assert [job["payload"]["profile_id"] for job in jobs(db, number)] == [veyon]


def claim_evaluation(db, world):
    # now() is the start of this rolled-back transaction, earlier than the enqueue instant.
    db.execute("UPDATE worker_jobs SET run_after = now() - interval '1 second' WHERE job_type = 'analyze_match'")
    return world.scoped().claim_scoped("worker", ("analyze_match",))


def stored_match(db, number, profile):
    return db.execute("SELECT * FROM matches WHERE id_contrato = %s AND profile_id = %s",
                      (number, profile)).fetchone()


def notification_traces(db):
    return (db.execute("SELECT count(*) AS n FROM notifications").fetchone()["n"]
            + db.execute("SELECT count(*) AS n FROM worker_jobs WHERE job_type = 'send_notification'"
                         ).fetchone()["n"])


@needs_db
def test_a_full_evaluation_stores_the_match_with_its_cost_and_creates_no_notifications(
        db, world, gemini, tmp_path):
    veyon, _ = world.companies
    number, extraction = contract(db, facets=1)
    decide(db, number, veyon)
    enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    gemini.return_value = reply()
    before = notification_traces(db)
    job = claim_evaluation(db, world)
    outcome = runtime.dispatch(settings(), world.scoped(), job, tmp_path)
    assert outcome["outcome"] == "evaluated" and outcome["provider_answered"] is True
    match = stored_match(db, number, veyon)
    meta = match["breakdown_json"]["meta"]
    assert (match["score"], match["verdict"]) == (90, "verde") and match["business_line_id"] is not None
    assert meta["execution"] == "worker_auto" and meta["extraction_id"] == extraction
    assert meta["evaluation_origin"] == AUTO_ORIGIN and meta["jev_score"] == 80.0
    assert (meta["input_tokens"], meta["output_tokens"]) == (1200, 340) and meta["cost_usd"] > 0
    assert db.execute("SELECT event_type FROM match_events WHERE match_id = %s", (match["id"],)
                      ).fetchone()["event_type"] == "automatic_analysis"
    assert notification_traces(db) == before  # No rows in notifications, no send_notification job.
    # Asking again pays nothing: the match is current.
    gemini.reset_mock()
    again = runtime.dispatch(settings(), world.scoped(), Job(9, "analyze_match", "llm", job.payload, 1), tmp_path)
    assert again["outcome"] == "skipped" and again["reason"] == "match_current"
    gemini.assert_not_called()
    assert notification_traces(db) == before


@needs_db
def test_the_same_evaluation_with_notifications_on_does_create_them(db, world, gemini):
    """Control: the seed really would notify, so the zero above is the runtime's doing."""
    veyon, _ = world.companies
    number, _ = contract(db, facets=1)
    gemini.return_value = reply()
    before = notification_traces(db)
    analyze_match_job(settings(), world.scoped(), id_contrato=number, profile_id=veyon, source="automatic")
    assert notification_traces(db) > before


@needs_db
def test_a_flagged_reading_is_never_sent_to_gemini_even_if_a_job_was_waiting(db, world, gemini, tmp_path):
    veyon, _ = world.companies
    number, extraction = contract(db)
    decide(db, number, veyon)
    enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    db.execute("UPDATE tdr_extractions SET requires_human_review = true WHERE id = %s", (extraction,))
    job = claim_evaluation(db, world)
    outcome = runtime.dispatch(settings(), world.scoped(), job, tmp_path)
    assert outcome == {"outcome": "skipped", "reason": "reading_requires_review"}
    gemini.assert_not_called()
    assert stored_match(db, number, veyon) is None


@needs_db
def test_a_provider_rejection_stores_nothing_and_leaves_the_pair_retryable(db, world, gemini, tmp_path):
    veyon, _ = world.companies
    number, _ = contract(db)
    decide(db, number, veyon)
    enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    gemini.side_effect = rejection(429)
    job = claim_evaluation(db, world)
    with pytest.raises(ProviderRejectedError):
        runtime.dispatch(settings(), world.scoped(), job, tmp_path)
    assert stored_match(db, number, veyon) is None
    assert not (tmp_path / "evaluations" / "guard.json").exists()


def complete(db, job, outcome):
    """What the runtime loop does after dispatch returns."""
    db.execute("UPDATE worker_jobs SET payload = payload || %s::jsonb WHERE id = %s",
               (json.dumps({"runtime_result": outcome}), job.id))
    db.execute("UPDATE worker_jobs SET status = 'done', finished_at = now() WHERE id = %s", (job.id,))


def add_proposal_document(db, number, extraction, label):
    db.execute("""INSERT INTO requirement_facets (id_contrato, extraction_id, facet, label, required,
                    details_json, facet_hash) VALUES (%s, %s, 'proposal_document', %s, true, '{}'::jsonb, %s)""",
               (number, extraction, label, f"hash-{number}-{label}"))


@needs_db
def test_proposal_documents_the_model_leaves_out_become_paperwork_and_the_evaluation_is_stored(
        db, world, gemini, tmp_path):
    veyon, _ = world.companies
    number, extraction = contract(db, facets=1)
    add_proposal_document(db, number, extraction, "Copia de contratos y su conformidad")
    decide(db, number, veyon)
    enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    gemini.return_value = reply()  # Judges the equipment requirement only (source_index 1).
    outcome = runtime.dispatch(settings(), world.scoped(), claim_evaluation(db, world), tmp_path)
    assert outcome["outcome"] == "evaluated" and gemini.call_count == 1
    breakdown = stored_match(db, number, veyon)["breakdown_json"]
    assert [r["source_index"] for r in breakdown["requisitos"]] == [1]
    assert {"tipo": "proposal_document", "requisito": "Copia de contratos y su conformidad",
            "estado": "preparar", "score_impact": False} in breakdown["administrative_checklist"]


@needs_db
def test_any_other_omitted_requirement_fails_only_that_pair_once_and_it_is_not_queued_again(
        db, world, gemini, tmp_path):
    veyon, _ = world.companies
    number, extraction = contract(db, facets=2)
    decide(db, number, veyon)
    enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    gemini.return_value = reply()  # Judges 1 of the 2 equipment requirements.
    job = claim_evaluation(db, world)
    outcome = runtime.dispatch(settings(), world.scoped(), job, tmp_path)
    assert outcome == {"outcome": INVALID_OUTCOME, "reason": "requirement_coverage_changed",
                       "extraction_id": extraction, "provider_answered": True}
    assert gemini.call_count == 1 and stored_match(db, number, veyon) is None
    assert not (tmp_path / "evaluations" / "guard.json").exists()
    complete(db, job, outcome)
    again = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert again["enqueued"] == 0 and again["skipped"].get("model_output_invalid") == 1
