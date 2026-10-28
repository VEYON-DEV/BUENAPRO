"""Automatic evaluation (LLM2) of PROD2 readings per company: who is evaluated, the daily limits and
how the paid call and the ledger close. Offline: Gemini is a fake and PostgreSQL a scripted fake."""
import json
from types import SimpleNamespace

import pytest

from buenapro_worker.matching.analyzer import MatchAnalysisResult
from buenapro_worker.matching.schemas import SubstantiveMatchAnalysis
from buenapro_worker.prod2 import evaluation
from buenapro_worker.prod6.reading import GuardedReadingError, ProviderRejectedError, ReadingBlockedError

VEYON, DIN = evaluation.VEYON, evaluation.DIN
FACETS = [{"facet": "equipment", "label": "Servidor", "required": True, "details_json": {}, "facet_hash": "h1"},
          {"facet": "rnp", "label": "RNP vigente", "required": True, "details_json": {}, "facet_hash": "h2"}]
ANSWER = {"veredicto": "verde", "score": 90, "resumen": "Cumple.", "acciones_recomendadas": [],
          "requisitos": [{"requisito": "Servidor", "categoria": "equipamiento", "estado": "cumple",
                          "critico": False, "source_index": 1, "evidence_explanation": "Declarado."}]}


def row(**fields):
    return {"extraction_id": 70, "id_convocatoria": 1051566, "opportunity_id": "00000000-0000-0000-0000-000000000001",
            "summary_json": {"descripcion_corta": "Licencias"}, "quality": "auto", "requires_human_review": True,
            "facet_count": 2, "domain": "informatica", "affinity": 96.0, "open": True, "hours_left": 72.0,
            "nomenclature": "AS-SM-1-2026", "description": "Licencias", "buyer_name": "Entidad",
            "profile_id": VEYON, "profile_hash": "a" * 64, "profile_active": True, "rule_enabled": True,
            "max_daily": 0, "min_hours": 0, "match_current": False} | fields


# --- skip_reason: who is evaluated, free of charge -------------------------------------------------

def test_a_jev_accepted_open_reading_under_review_is_evaluated():
    assert evaluation.skip_reason(row()) is None  # The review flag makes it provisional, not skipped.


@pytest.mark.parametrize("domain,company,reason", [
    ("informatica", VEYON, None), ("informatica", DIN, None), ("ambiguo", VEYON, None), ("ambiguo", DIN, None),
    ("industrial_electrica", DIN, None), ("industrial_electrica", VEYON, "domain_not_for_company"),
    ("otro", VEYON, "domain_not_for_company"), ("otro", DIN, "domain_not_for_company")])
def test_the_jev_domain_chooses_the_companies(domain, company, reason):
    assert evaluation.skip_reason(row(domain=domain, profile_id=company)) == reason


@pytest.mark.parametrize("fields,reason", [
    ({"domain": None, "affinity": None}, "jev_missing"), ({"affinity": 59.99}, "jev_rejected"),
    ({"profile_active": False}, "profile_inactive"), ({"profile_hash": None}, "profile_hash_missing"),
    ({"rule_enabled": False}, "rule_disabled"), ({"open": False}, "closed"),
    ({"quality": "failed"}, "reading_failed"), ({"facet_count": 0}, "no_requirements"),
    ({"match_current": True}, "match_current"), ({"hours_left": 23.0, "min_hours": 24}, "too_close_to_close")])
def test_skip_reasons(fields, reason):
    assert evaluation.skip_reason(row(**fields)) == reason


# --- a scripted PostgreSQL ---------------------------------------------------------------------------

class Conn:
    def __init__(self, rows=(), *, today=0, menores=0, profile_hash="a" * 64):
        self.rows, self.today, self.menores, self.profile_hash = list(rows), today, menores, profile_hash
        self.updates = []

    def execute(self, sql, params=None):
        if "WITH jev AS" in sql:
            return SimpleNamespace(fetchall=lambda: self.rows)
        if "FROM worker_jobs" in sql:
            return SimpleNamespace(fetchone=lambda: {"total": self.menores})
        if "kind = 'gemini_match' AND policy_version" in sql:
            return SimpleNamespace(fetchone=lambda: {"total": self.today})
        if "FROM company_profiles" in sql:
            return SimpleNamespace(fetchone=lambda: {
                "profile_hash": self.profile_hash, "razon_social": "PRIVADA", "ruc": "20999999991",
                "identity_json": {"descripcion": "Software"}, "team_json": [], "experience_json": [],
                "econ_experience_json": {}, "equipment_json": [{"nombre": "Servidor"}], "certifications_json": [],
                "hireable_roles_json": []})
        if "FROM prod2_requirement_facets" in sql:
            return SimpleNamespace(fetchall=lambda: FACETS)
        if sql.lstrip().startswith("UPDATE prod2_evaluations"):
            self.updates.append((sql, params))
            return SimpleNamespace()
        raise AssertionError(sql)


def test_the_plan_applies_the_daily_limit_shared_with_menores_and_the_call_limit():
    rows = [row(id_convocatoria=n, profile_id=DIN, max_daily=50) for n in range(1, 5)] + [row(id_convocatoria=9)]
    planned = evaluation.plan(Conn(rows, today=47, menores=1), max_calls=10)
    assert [item["id_convocatoria"] for item in planned["planned"]] == [1, 2, 9]  # 47 + 1 menores + 2 = 50.
    assert planned["skipped"] == {"daily_limited": 2}
    assert len(evaluation.plan(Conn(rows), max_calls=2)["planned"]) == 2


# --- evaluate_pair / run: the paid call and the ledger -----------------------------------------------

class Analyzer:
    prompt = "prompt"

    def __init__(self, outcome=None, answer=None):
        self.outcome, self.answer, self.calls, self.closed = outcome, answer or ANSWER, [], False

    def analyze(self, **request):
        self.calls.append(request)
        if self.outcome:
            raise self.outcome
        return MatchAnalysisResult(analysis=SubstantiveMatchAnalysis.model_validate(self.answer), raw_json=self.answer,
                                   model="gemini-3.1-flash-lite", prompt_version=evaluation.POLICY_VERSION,
                                   input_tokens=1200, output_tokens=300, cost_usd=0.0007)

    def close(self):
        self.closed = True


@pytest.fixture
def ledger(monkeypatch):
    state = SimpleNamespace(claims=[], completed=[], persisted=[], next="claimed", stored=None)

    def claim(conn, convocation, **fields):
        state.claims.append((convocation, fields))
        if state.next == "claimed":
            return {"status": "claimed", "id": 11, "claim_token": "t"}
        return {"status": state.next, "id": 11, "result_json": state.stored}

    monkeypatch.setattr(evaluation, "claim_evaluation", claim)
    monkeypatch.setattr(evaluation, "complete_evaluation",
                        lambda conn, id_, token, result, **usage: state.completed.append((id_, result, usage)))
    monkeypatch.setattr(evaluation, "persist_match",
                        lambda conn, convocation, **fields: state.persisted.append(fields) or {"match_id": 5,
                        "score": fields["score"], "verdict": fields["verdict"]})
    return state


def settings():
    return SimpleNamespace(gemini_model="gemini-3.1-flash-lite")


def test_a_pair_pays_once_completes_the_reservation_and_stores_a_provisional_match(ledger):
    analyzer = Analyzer()
    outcome = evaluation.evaluate_pair(Conn(), settings(), analyzer, row())
    assert outcome["outcome"] == "evaluated" and outcome["cost_usd"] == 0.0007 and len(analyzer.calls) == 1
    convocation, claim = ledger.claims[0]
    assert convocation == 1051566 and claim["kind"] == "gemini_match" and claim["profile_id"] == VEYON
    assert claim["policy_version"] == "prod2_match_v1"
    assert [item["tipo"] for item in analyzer.calls[0]["requisitos"]] == ["equipment"]  # RNP is checklist only.
    assert analyzer.calls[0]["oportunidad"]["codigo"] == "AS-SM-1-2026"
    (_, stored, usage), = ledger.completed
    assert usage == {"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.0007}
    assert stored["extraction_id"] == 70 and stored["profile_hash"] == "a" * 64 and stored["verdict"] == "verde"
    meta = stored["breakdown"]["meta"]
    assert meta["jev_domain"] == "informatica" and meta["evaluation_origin"] == "prod2_jev_domain_v1"
    persisted, = ledger.persisted
    assert persisted["allow_source_review"] is True and persisted["evaluation_id"] == 11
    assert persisted["breakdown_json"]["administrative_checklist"][0]["tipo"] == "rnp"


def test_a_completed_reservation_is_replayed_without_calling_the_provider(ledger):
    ledger.next, ledger.stored = "cached", {"extraction_id": 70, "profile_hash": "a" * 64, "score": 60,
        "verdict": "ambar", "breakdown": {"meta": {"cost_usd": 0.0007}}, "missing": []}
    analyzer = Analyzer()
    outcome = evaluation.evaluate_pair(Conn(), settings(), analyzer, row())
    assert outcome["outcome"] == "replayed" and outcome["cost_usd"] == 0 and analyzer.calls == []
    assert ledger.persisted[0]["verdict"] == "ambar" and ledger.completed == []


@pytest.mark.parametrize("status,outcome", [("busy", "skipped"), ("failed", "skipped"),
                                            ("uncertain", "payment_uncertain")])
def test_a_reservation_held_elsewhere_or_in_doubt_never_sends(ledger, status, outcome):
    ledger.next = status
    analyzer = Analyzer()
    assert evaluation.evaluate_pair(Conn(), settings(), analyzer, row())["outcome"] == outcome
    assert analyzer.calls == [] and ledger.persisted == []


def test_a_profile_changed_since_planning_costs_nothing(ledger):
    analyzer = Analyzer()
    outcome = evaluation.evaluate_pair(Conn(profile_hash="b" * 64), settings(), analyzer, row())
    assert outcome == {"outcome": "skipped", "reason": "profile_changed"} and ledger.claims == []


@pytest.mark.parametrize("error,outcome,status", [
    (ProviderRejectedError(503, "UNAVAILABLE"), "provider_rejected", "pending"),
    (ReadingBlockedError("guard open"), "receipts_blocked", "pending"),
    (GuardedReadingError("uncertain"), "payment_uncertain", "uncertain")])
def test_provider_and_receipt_failures_release_or_mark_the_reservation(ledger, error, outcome, status):
    conn = Conn()
    assert evaluation.evaluate_pair(conn, settings(), Analyzer(error), row())["outcome"] == outcome
    (sql, params), = conn.updates
    assert ("status='pending'" in sql) if status == "pending" else (params[0] == "uncertain")
    assert ledger.completed == [] and ledger.persisted == []


def test_an_answer_that_skips_requirements_fails_only_that_pair_with_its_cost(ledger):
    conn = Conn()
    skipped = {**ANSWER, "requisitos": []}
    outcome = evaluation.evaluate_pair(conn, settings(), Analyzer(answer=skipped), row())
    assert outcome["outcome"] == "model_output_invalid" and outcome["cost_usd"] == 0.0007
    (sql, params), = conn.updates
    assert params[0] == "failed" and json.loads(params[1])["reason"] == "requirement_coverage_changed"
    assert params[2:5] == (1200, 300, 0.0007)
    assert ledger.persisted == []


def test_a_run_stops_at_the_first_provider_rejection_and_closes_the_analyzer(ledger):
    rows = [row(id_convocatoria=1), row(id_convocatoria=2)]
    analyzer = Analyzer(ProviderRejectedError(429, "RESOURCE_EXHAUSTED"))
    report = evaluation.run(Conn(rows), settings(), None, max_calls=5, analyzer=analyzer)
    assert report["stopped"] == "provider_rejected" and len(report["outcomes"]) == 1 and analyzer.closed
    assert report["cost_usd"] == 0


def test_a_run_with_nothing_planned_never_builds_an_analyzer(ledger, monkeypatch):
    monkeypatch.setattr(evaluation, "Prod2GuardedMatchAnalyzer", lambda *a, **k: pytest.fail("no analyzer"))
    report = evaluation.run(Conn([row(match_current=True)]), settings(), None, max_calls=5)
    assert report["planned"] == 0 and report["skipped"] == {"match_current": 1}


def test_mayores_receipts_and_prompt_are_their_own():
    assert evaluation.Prod2GuardedMatchAnalyzer.receipt_kind == "prod2_match_analysis_v1"
    from buenapro_worker.matching.analyzer import SUBSTANTIVE_PROMPT_FILES, read_analysis_prompt
    prompt = read_analysis_prompt(SUBSTANTIVE_PROMPT_FILES[evaluation.POLICY_VERSION])
    assert prompt.startswith("Ámbito: procedimiento de selección") and "contratación menor" not in prompt
