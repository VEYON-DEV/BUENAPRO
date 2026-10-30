"""Manual grading requests («Calificar») in the PROD6 runtime: how a queued request is read and evaluated,
the guard that admits only its origin, and that a (contract, company) pair is paid once. Offline: Gemini is
a mock; PostgreSQL tests run only with BUENAPRO_TEST_DATABASE_URL and are always rolled back."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from buenapro_worker.jobs.match import _automatic_jobs_today
from buenapro_worker.prod6 import manual_requests, runtime
from buenapro_worker.prod6.evaluation import AUTO_ORIGIN, INVALID_OUTCOME, enqueue_evaluations
from buenapro_worker.prod6.manual_requests import CLAIM_BATCH, MANUAL_ORIGIN, MANUAL_SOURCE, pair_verdict
from buenapro_worker.queue.repository import Job
from test_prod6_auto_evaluation import (  # noqa: F401  (fixtures and builders shared with the automatic evaluation)
    contract, db, decide, gemini, mismatch, notification_traces, reply, rule, settings, world,
)
from test_prod6_runtime_recovery import offline_runtime  # noqa: F401

DATABASE_URL = os.environ.get("BUENAPRO_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="BUENAPRO_TEST_DATABASE_URL not set")


# --- pair_verdict: one verdict per pair, free of charge ------------------------------------------------------

def pair(**fields):
    return {"contract_found": True, "profile_active": True, "extraction_id": 7, "quality": "auto",
            "requires_human_review": False, "facets": 3, "match_id": None, "match_extraction_id": None,
            "invalid_answer": False} | fields


@pytest.mark.parametrize("fields, expected", [
    ({}, ("ready", None)),
    ({"contract_found": False}, ("failed", "contract_not_found")),
    ({"profile_active": False}, ("failed", "profile_inactive")),
    ({"extraction_id": None}, ("absent", None)),
    ({"quality": "failed"}, ("failed", "reading_failed")),
    ({"requires_human_review": True}, ("failed", "reading_requires_review")),
    ({"facets": 0}, ("deriving", None)),
    ({"invalid_answer": True}, ("failed", "invalid_model_output")),
    ({"match_id": 1, "match_extraction_id": "7"}, ("done", None)),
    ({"match_id": 1, "match_extraction_id": "6"}, ("ready", None)),       # A match of an older reading is graded again.
    ({"match_id": 1, "match_extraction_id": None}, ("done", None)),       # Unknown origin: never overwritten.
    ({"match_id": 1, "match_extraction_id": None, "extraction_id": None}, ("done", None)),
])
def test_pair_verdicts(fields, expected):
    assert pair_verdict(pair(**fields)) == expected


def test_a_reading_in_review_or_failed_outranks_a_match_of_an_older_reading():
    assert pair_verdict(pair(requires_human_review=True, match_id=1, match_extraction_id="6")) == (
        "failed", "reading_requires_review")


# --- the runtime queue admits exactly one more kind of analyze_match ------------------------------------------------

def scoped(profiles=("veyon", "din")):
    return runtime.ScopedRepository(MagicMock(), profile_id=profiles[0], batch_id="b", profile_ids=list(profiles))


def manual(**fields):
    return {"id_contrato": 42, "profile_id": "din", "source": MANUAL_SOURCE, "origin": MANUAL_ORIGIN,
            "manual_request_id": 5} | fields


@patch("buenapro_worker.queue.repository.JobRepository.enqueue", return_value=11)
def test_the_manual_evaluation_is_queued_with_runtime_ownership(enqueue):
    repo = scoped()
    assert repo.enqueue("analyze_match", manual(), queue_name="llm", priority=3, dedup_key="analyze_match:din:42") == 11
    payload = enqueue.call_args.args[1]
    assert payload["source_runtime"] == runtime.NAMESPACE and payload["scope_profile_id"] == "veyon"
    assert payload["origin"] == MANUAL_ORIGIN and payload["manual_request_id"] == 5
    assert enqueue.call_args.kwargs["dedup_key"] == f"{runtime.NAMESPACE}:veyon:analyze_match:din:42"
    assert enqueue.call_args.kwargs["max_attempts"] == 1


@pytest.mark.parametrize("payload", [
    manual(origin=AUTO_ORIGIN),                       # The manual source with the automatic origin.
    manual(source="automatic"),                       # The automatic source with the manual origin.
    manual(origin="legacy_router"), manual(source="manual"), manual(source=None),
    manual(manual_request_id=None), manual(manual_request_id="5"), manual(manual_request_id=True),
    manual(profile_id="other"), manual(profile_id=None),
    {k: v for k, v in manual().items() if k != "manual_request_id"},
])
@patch("buenapro_worker.queue.repository.JobRepository.enqueue")
def test_only_the_manual_origin_with_its_request_for_a_runtime_company_passes_the_guard(enqueue, payload):
    repo = scoped()
    assert repo.enqueue("analyze_match", payload, queue_name="llm") is None
    enqueue.assert_not_called()
    assert repo.suppressed == {"analyze_match": 1}


@patch("buenapro_worker.queue.repository.JobRepository.enqueue", return_value=3)
def test_the_automatic_origin_is_still_admitted_exactly_as_before(enqueue):
    repo = scoped()
    payload = {"id_contrato": 42, "profile_id": "din", "source": "automatic", "origin": AUTO_ORIGIN}
    assert repo.enqueue("analyze_match", payload, queue_name="llm") == 3
    assert repo.enqueue("analyze_match", {**payload, "manual_request_id": 5}, queue_name="llm") == 3  # Extra key.


def test_the_free_io_executor_cannot_run_a_manual_evaluation():
    job = Job(1, "analyze_match", "llm", manual(), 1)
    with patch.object(runtime, "connect") as connect, pytest.raises(ValueError, match="free HTTP"):
        runtime.execute_io_job(MagicMock(), "veyon", job, Path("unused"))
    connect.assert_not_called()


def test_a_manual_job_with_a_forged_source_is_refused_before_any_work():
    forged = Job(1, "analyze_match", "llm", manual(source="automatic", batch_id="b"), 1)
    with patch.object(runtime, "GuardedMatchAnalyzer") as guarded, patch.object(runtime, "eligible_contract",
                                                                              return_value=False):
        with pytest.raises(ValueError, match="manual grading origin"):
            runtime.dispatch(MagicMock(), scoped(), forged, Path("unused"))
    guarded.assert_not_called()


def test_the_claim_takes_at_most_twenty_with_skip_locked():
    assert CLAIM_BATCH == 20 and "FOR UPDATE SKIP LOCKED" in manual_requests.LOCK_SQL
    assert "mode = 'queued'" in manual_requests.LOCK_SQL and "'seace_prod6'" in manual_requests.LOCK_SQL


# --- the evaluation job with fakes -----------------------------------------------------------------------------------

def evaluation_job(attempts=1, **fields):
    return Job(5, "analyze_match", "llm", manual(profile_id="veyon") | {"batch_id": "b", "business_line_id": None}
               | fields, attempts)


@pytest.fixture
def evaluator(monkeypatch, tmp_path):
    ready = pair(contract_found=True, extraction_id=9)
    monkeypatch.setattr(manual_requests, "is_evaluating", MagicMock(return_value=True))
    monkeypatch.setattr(manual_requests, "pair_row", MagicMock(return_value=ready))
    settle = MagicMock(return_value=True)
    monkeypatch.setattr(manual_requests, "settle", settle)
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=False))  # A closed contract.
    analyze = MagicMock(return_value={"match_id": 3, "verdict": "verde", "score": 90})
    monkeypatch.setattr(runtime, "analyze_match_job", analyze)
    guarded = MagicMock(provider_answered=True)
    factory = MagicMock(return_value=guarded)
    monkeypatch.setattr(runtime, "GuardedMatchAnalyzer", factory)
    return SimpleNamespace(row=ready, analyze=analyze, guarded=guarded, factory=factory, settle=settle,
                           is_evaluating=manual_requests.is_evaluating, folder=tmp_path,
                           run=lambda job: runtime.dispatch(MagicMock(), scoped(), job, tmp_path))


def test_a_manual_job_on_a_closed_contract_pays_once_without_notifications_or_a_jev_decision(evaluator):
    assert evaluator.run(evaluation_job()) == {"outcome": "evaluated", "match_id": 3, "verdict": "verde",
                                               "score": 90, "provider_answered": True}
    options = evaluator.analyze.call_args.kwargs
    assert options["notifications"] is False and options["source"] == MANUAL_SOURCE
    assert options["analyzer"] is evaluator.guarded and options["id_contrato"] == 42
    assert options["extra_meta"] == {"evaluation_origin": MANUAL_ORIGIN, "manual_request_id": 5}
    assert evaluator.factory.call_args.args[1] == evaluator.folder / "evaluations"
    evaluator.settle.assert_called_once_with(evaluator.settle.call_args.args[0], 5, "done")
    evaluator.guarded.close.assert_called_once()


def test_a_job_claimed_before_may_only_replay_its_own_answer(evaluator):
    evaluator.run(evaluation_job(attempts=1))
    assert evaluator.factory.call_args.kwargs == {"allow_new_requests": True}
    evaluator.run(evaluation_job(attempts=2))
    assert evaluator.factory.call_args.kwargs == {"allow_new_requests": False}


def test_an_invalid_model_answer_fails_only_that_request_and_stays_replayable(evaluator):
    evaluator.analyze.side_effect = [mismatch()]
    assert evaluator.run(evaluation_job()) == {"outcome": INVALID_OUTCOME, "reason": "requirement_coverage_changed",
                                               "extraction_id": 9, "provider_answered": True}
    assert evaluator.settle.call_args.args[1:] == (5, "failed", "invalid_model_output")
    evaluator.guarded.close.assert_called_once()


def test_a_request_that_is_no_longer_evaluating_costs_nothing(evaluator):
    evaluator.is_evaluating.return_value = False  # Timed out, or already settled.
    assert evaluator.run(evaluation_job()) == {"outcome": "skipped", "reason": "request_not_active"}
    evaluator.factory.assert_not_called()
    evaluator.analyze.assert_not_called()


@pytest.mark.parametrize("fields, status, reason", [
    ({"requires_human_review": True}, "failed", "reading_requires_review"),
    ({"quality": "failed"}, "failed", "reading_failed"),
    ({"profile_active": False}, "failed", "profile_inactive"),
    ({"extraction_id": None}, "failed", "reading_failed"),
    ({"match_id": 1, "match_extraction_id": "9"}, "done", None),
])
def test_a_waiting_job_whose_pair_stopped_qualifying_settles_its_request_for_free(evaluator, fields, status, reason):
    evaluator.row.update(fields)
    outcome = evaluator.run(evaluation_job())
    assert outcome["outcome"] == "skipped"
    assert evaluator.settle.call_args.args[1:] == (5, status, reason)
    evaluator.factory.assert_not_called()


def test_a_company_outside_the_runtime_fails_its_request_without_paying(evaluator):
    assert evaluator.run(evaluation_job(profile_id="stranger")) == {"outcome": "skipped",
                                                                   "reason": "profile_out_of_scope"}
    assert evaluator.settle.call_args.args[1:] == (5, "failed", "profile_inactive")
    evaluator.factory.assert_not_called()


def test_any_other_failure_still_stops_like_an_automatic_one(evaluator):
    evaluator.analyze.side_effect = ValueError("Contract not found: 42")
    with pytest.raises(ValueError, match="Contract not found"):
        evaluator.run(evaluation_job())
    evaluator.settle.assert_not_called()


def test_a_closed_contract_without_a_request_is_still_preserved(monkeypatch):
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=False))
    for kind in ("process_contract", "download_file", "extract_tdr", "derive_summary"):
        job = Job(1, kind, "io", {"id_contrato": 42, "id_contrato_archivo": 9, "extraction_id": 1, "batch_id": "b"}, 1)
        assert runtime.dispatch(MagicMock(), scoped(), job, Path("unused")) == {"outcome": "closed_preserved"}


# --- the runtime loop ------------------------------------------------------------------------------------------------

def test_the_step_runs_in_every_cycle_and_an_idle_runtime_wakes_for_a_new_request(offline_runtime, monkeypatch):
    h = offline_runtime
    steps = []
    monkeypatch.setattr(runtime.manual_requests, "step", lambda repo: steps.append(repo.batch_id) or {
        "reading": 1, "waiting": 4})
    pending = {"ids": {3}}
    monkeypatch.setattr(runtime.manual_requests, "pending_ids", lambda conn, profiles: set(pending["ids"]))
    monkeypatch.setattr(runtime, "MANUAL_WAKE_SECONDS", 0)
    woke = []

    def wait(should_stop, seconds):
        woke.append(should_stop())  # The request the cycle already saw does not wake it.
        pending["ids"] = {3, 4}
        woke.append(should_stop())  # A new one does.
        h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False) == 0
    assert woke == [False, True] and len(steps) == 1 and steps[0].startswith("prod6-")
    assert h.states[-1]["manual_requests"] == {"reading": 1}  # 'waiting' is not recorded.


def test_a_database_failure_while_looking_for_requests_never_breaks_the_wait(offline_runtime, monkeypatch):
    h = offline_runtime
    monkeypatch.setattr(runtime.manual_requests, "step", lambda repo: {})
    monkeypatch.setattr(runtime.manual_requests, "pending_ids",
                        MagicMock(side_effect=psycopg.OperationalError("down")))
    monkeypatch.setattr(runtime, "MANUAL_WAKE_SECONDS", 0)

    def wait(should_stop, seconds):
        assert should_stop() is False
        h.stop()

    h.wait.side_effect = wait
    assert h.run(once=False) == 0


# --- PostgreSQL ------------------------------------------------------------------------------------------------------

def company(world, index=0):
    return world.companies[index]


def request(db, profile, number, *, mode="queued", status="pending", source_key="seace_prod6", age_hours=0):
    return db.execute(
        """INSERT INTO manual_grading_requests (tenant_id, profile_id, source_key, source_id, requested_by, mode,
             status, created_at, updated_at)
           SELECT tenant_id, id, %s, %s, 'tester', %s, %s, now() - make_interval(hours => %s),
                  now() - make_interval(hours => %s) FROM company_profiles WHERE id = %s RETURNING id""",
        (source_key, number, mode, status, age_hours, age_hours, profile)).fetchone()["id"]


def state(db, request_id):
    row = db.execute("SELECT status, reason FROM manual_grading_requests WHERE id = %s", (request_id,)).fetchone()
    return row["status"], row["reason"]


def jobs_of(db, number, *types):
    return db.execute(
        """SELECT job_type, status, queue_name, priority, dedup_key, payload, run_after FROM worker_jobs
           WHERE payload->>'id_contrato' = %s::text AND (cardinality(%s::text[]) = 0 OR job_type = ANY(%s::text[]))
           ORDER BY id""", (number, list(types), list(types))).fetchall()


def add_reading(db, number, *, flagged=False, quality="auto", facets=2, derived=True):
    """The current reading of a contract that had none: a document, its extraction and its facets."""
    document = db.execute(
        """INSERT INTO contract_documents (id_contrato, id_contrato_archivo, categoria, filename, sha256_original,
             seace_download_url) VALUES (%s, %s, 1, 'tdr.pdf', %s, 'https://example.invalid') RETURNING id""",
        (number, number, f"{number:064d}")).fetchone()["id"]
    extraction = db.execute(
        """INSERT INTO tdr_extractions (contract_document_id, model, prompt_version, schema_version,
             raw_extraction_json, summary_json, requires_human_review, quality)
           VALUES (%s, 'm', 'p', 's', '{}'::jsonb, '{"descripcion_corta": "x"}'::jsonb, %s, %s) RETURNING id""",
        (document, flagged, quality)).fetchone()["id"]
    for index in range(facets if derived else 0):
        db.execute(
            """INSERT INTO requirement_facets (id_contrato, extraction_id, facet, label, required, details_json,
                 facet_hash) VALUES (%s, %s, 'equipment', %s, true, '{}'::jsonb, %s)""",
            (number, extraction, f"Servidor {index}", f"hash-{number}-{index}"))
    return extraction


def finish(db, job_type, number, status="done"):
    db.execute("UPDATE worker_jobs SET status = %s WHERE job_type = %s AND payload->>'id_contrato' = %s::text",
               (status, job_type, str(number)))


def hold(db, number, job_type="download_file"):
    """What the runtime does with a job it sends to manual review."""
    db.execute("""UPDATE worker_jobs SET status = 'pending', run_after = 'infinity'::timestamptz
                  WHERE job_type = %s AND payload->>'id_contrato' = %s::text""", (job_type, str(number)))


def claim_evaluation(db, world):
    db.execute("UPDATE worker_jobs SET run_after = now() - interval '1 second' WHERE job_type = 'analyze_match'")
    return world.scoped().claim_scoped("worker", ("analyze_match",))


def stored_match(db, number, profile):
    return db.execute("SELECT * FROM matches WHERE id_contrato = %s AND profile_id = %s", (number, profile)).fetchone()


@needs_db
def test_a_request_without_a_reading_queues_the_reading_of_a_closed_contract_and_goes_to_reading(db, world):
    veyon = company(world)
    number, _ = contract(db, closes_in_hours=-48, state=3, read=False)
    rid = request(db, veyon, number)
    assert manual_requests.step(world.scoped()) == {"reading": 1}
    assert state(db, rid) == ("reading", None)
    (job,) = jobs_of(db, number)
    assert (job["job_type"], job["queue_name"], job["priority"]) == ("process_contract", "io", 2)
    assert job["dedup_key"] == f"{runtime.NAMESPACE}:{veyon}:process_contract:manual:{number}"
    # The payload of admit_contract (contract, segment, batch) plus the runtime's ownership and policy keys.
    assert job["payload"]["id_contrato"] == number and job["payload"]["segment"] == 43
    assert job["payload"]["batch_id"] == "test-batch"
    assert job["payload"]["source_runtime"] == runtime.NAMESPACE and job["payload"]["scope_profile_id"] == veyon


@needs_db
def test_claiming_twice_changes_nothing_and_never_queues_the_reading_again(db, world):
    number, _ = contract(db, closes_in_hours=-48, state=3, read=False)
    rid = request(db, company(world), number)
    assert manual_requests.step(world.scoped()) == {"reading": 1}
    assert manual_requests.step(world.scoped()) == {"waiting": 1}  # The job is still pending.
    assert manual_requests.step(world.scoped()) == {"waiting": 1}
    assert state(db, rid) == ("reading", None) and len(jobs_of(db, number)) == 1


@needs_db
def test_a_process_contract_already_pending_for_the_contract_is_not_queued_again(db, world):
    veyon = company(world)
    number, _ = contract(db, read=False)
    existing = world.scoped().enqueue("process_contract", {"id_contrato": number, "segment": 43}, queue_name="io",
                                      dedup_key=f"process_contract:other-batch:{number}:hash")
    assert existing is not None
    rid = request(db, veyon, number)
    assert manual_requests.step(world.scoped()) == {"reading": 1}
    assert state(db, rid) == ("reading", None) and len(jobs_of(db, number)) == 1
    # Not even a download or an extraction in flight is duplicated by a request.
    other, _ = contract(db, read=False)
    world.scoped().enqueue("download_file", {"id_contrato": other, "id_contrato_archivo": 1}, queue_name="io")
    request(db, veyon, other)
    assert manual_requests.step(world.scoped()) == {"reading": 1, "waiting": 1}
    assert [job["job_type"] for job in jobs_of(db, other)] == ["download_file"]


@needs_db
def test_a_valid_reading_goes_straight_to_the_evaluation_even_of_a_closed_contract_without_any_decision(db, world):
    veyon = company(world)
    number, extraction = contract(db, closes_in_hours=-300, state=3, facets=2)
    rule(db, veyon, enabled=False)  # The company rules are for the automatic evaluation only.
    rid = request(db, veyon, number)
    assert manual_requests.step(world.scoped()) == {"evaluating": 1}
    assert state(db, rid) == ("evaluating", None)
    (job,) = jobs_of(db, number)
    assert (job["job_type"], job["queue_name"], job["priority"]) == ("analyze_match", "llm", 3)
    assert job["dedup_key"] == f"{runtime.NAMESPACE}:{veyon}:analyze_match:{veyon}:{number}"  # The automatic key.
    payload = job["payload"]
    assert (payload["source"], payload["origin"], payload["manual_request_id"]) == (MANUAL_SOURCE, MANUAL_ORIGIN, rid)
    assert (payload["profile_id"], payload["extraction_id"]) == (veyon, extraction)
    assert payload["business_line_id"]  # By the contract's segment.
    assert payload["source_runtime"] == runtime.NAMESPACE and payload["scope_profile_id"] == veyon
    claimed = claim_evaluation(db, world)
    assert claimed.job_type == "analyze_match" and claimed.payload["manual_request_id"] == rid


@needs_db
@pytest.mark.parametrize("build, reason", [
    (dict(flagged=True), "reading_requires_review"), (dict(quality="failed"), "reading_failed"),
])
def test_a_reading_in_review_or_failed_fails_the_request_and_queues_nothing(db, world, build, reason):
    number, _ = contract(db, **build)
    rid = request(db, company(world), number)
    assert manual_requests.step(world.scoped()) == {"failed": 1}
    assert state(db, rid) == ("failed", reason) and jobs_of(db, number) == []


@needs_db
def test_a_missing_contract_and_an_inactive_company_fail_the_request(db, world):
    veyon, din = world.companies
    gone = request(db, veyon, 9_999_999_001)
    number, _ = contract(db)
    inactive = request(db, din, number)
    db.execute("UPDATE company_profiles SET is_active = false WHERE id = %s", (din,))
    # An inactive company is not served by the runtime's own scope list either, but it fails when listed.
    world_scoped = runtime.ScopedRepository(db, profile_id=veyon, batch_id="b", profile_ids=[veyon, din])
    assert manual_requests.step(world_scoped) == {"failed": 2}
    assert state(db, gone) == ("failed", "contract_not_found")
    assert state(db, inactive) == ("failed", "profile_inactive")
    assert jobs_of(db, number) == []


@needs_db
def test_only_queued_prod6_requests_of_the_runtime_companies_are_touched(db, world):
    veyon, din = world.companies
    number = contract(db, read=False)[0]
    direct = request(db, veyon, number, mode="direct", status="done")
    other_source = request(db, veyon, number, source_key="seace_prod2")
    stranger = contract(db, read=False)[0]
    foreign = request(db, din, stranger)
    repo = runtime.ScopedRepository(db, profile_id=veyon, batch_id="b", profile_ids=[veyon])  # din is not served.
    assert manual_requests.step(repo) == {}
    assert state(db, direct) == ("done", None) and state(db, other_source) == ("pending", None)
    assert state(db, foreign) == ("pending", None) and jobs_of(db, number) == jobs_of(db, stranger) == []


@needs_db
def test_the_claim_takes_twenty_at_a_time(db, world):
    veyon = company(world)
    ids = [request(db, veyon, contract(db, closes_in_hours=-9, state=3, read=False)[0]) for _ in range(25)]
    assert manual_requests.step(world.scoped()) == {"reading": CLAIM_BATCH}
    assert sum(state(db, rid)[0] == "pending" for rid in ids) == 5
    assert manual_requests.step(world.scoped()) == {"reading": 5, "waiting": CLAIM_BATCH}
    assert all(state(db, rid)[0] == "reading" for rid in ids)


@needs_db
def test_the_oldest_requests_are_claimed_first(db, world):
    veyon = company(world)
    young = request(db, veyon, contract(db, read=False)[0], age_hours=1)
    old = request(db, veyon, contract(db, read=False)[0], age_hours=5)
    with patch.object(manual_requests, "CLAIM_BATCH", 1):
        assert manual_requests.step(world.scoped()) == {"reading": 1}
    assert state(db, old)[0] == "reading" and state(db, young)[0] == "pending"


# --- the hook after derive_summary and the sweep -------------------------------------------------------------------

def derive(db, world, number, extraction, review):
    summary = {"facets": 2, "diff": [], "requires_human_review": review}
    with patch.object(runtime, "derive_summary_job", return_value=summary):
        return runtime.dispatch(settings(), world.scoped(), Job(1, "derive_summary", "match", {
            "id_contrato": number, "extraction_id": extraction, "batch_id": "b"}, 1), Path("unused"))


@needs_db
def test_the_whole_reading_of_a_closed_contract_runs_because_a_request_is_reading_it(db, world, tmp_path):
    veyon = company(world)
    number, _ = contract(db, closes_in_hours=-48, state=3, read=False)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    assert state(db, rid)[0] == "reading"
    process = claim(db, world, "process_contract")
    with patch.object(runtime, "process_contract", return_value={"files_changed": False}) as read, \
            patch.object(runtime, "enqueue_unread_documents", return_value=1) as unread:
        outcome = runtime.dispatch(settings(), world.scoped(), process, tmp_path)
    read.assert_called_once()
    assert outcome == {"files_changed": False, "unread_enqueued": 1} and unread.call_count == 1
    # The same job for a closed contract nobody asked about is preserved untouched.
    other, _ = contract(db, closes_in_hours=-48, state=3, read=False)
    world.scoped().enqueue("process_contract", {"id_contrato": other, "segment": 43}, queue_name="io")
    with patch.object(runtime, "process_contract") as read:
        preserved = runtime.dispatch(settings(), world.scoped(), claim(db, world, "process_contract"), tmp_path)
    assert preserved == {"outcome": "closed_preserved"} and read.call_count == 0


def claim(db, world, job_type):
    db.execute("UPDATE worker_jobs SET run_after = now() - interval '1 second' WHERE job_type = %s", (job_type,))
    return world.scoped().claim_scoped("worker", (job_type,))


@needs_db
def test_a_usable_summary_moves_the_request_to_evaluating_in_the_hook(db, world):
    veyon = company(world)
    number, _ = contract(db, closes_in_hours=-48, state=3, read=False)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    extraction = add_reading(db, number)
    result = derive(db, world, number, extraction, review=False)
    assert result["manual_requests"] == {"evaluating": 1} and state(db, rid) == ("evaluating", None)
    assert [job["job_type"] for job in jobs_of(db, number, "analyze_match")] == ["analyze_match"]


@needs_db
def test_a_summary_in_review_fails_the_request_in_the_hook(db, world):
    number, _ = contract(db, read=False)
    rid = request(db, company(world), number)
    manual_requests.step(world.scoped())
    extraction = add_reading(db, number, flagged=True)
    result = derive(db, world, number, extraction, review=True)
    assert result["manual_requests"] == {"failed": 1}
    assert state(db, rid) == ("failed", "reading_requires_review") and jobs_of(db, number, "analyze_match") == []


@needs_db
def test_the_hook_leaves_the_requests_of_other_contracts_and_states_alone(db, world):
    veyon = company(world)
    number, _ = contract(db, read=False)
    waiting = request(db, veyon, contract(db, read=False)[0], status="reading")
    pending = request(db, veyon, number)
    extraction = add_reading(db, number)
    result = derive(db, world, number, extraction, review=False)
    assert "manual_requests" not in result
    assert state(db, waiting) == ("reading", None) and state(db, pending) == ("pending", None)


@needs_db
def test_the_sweep_settles_a_reading_that_arrived_without_the_hook(db, world):
    number, _ = contract(db, read=False)
    rid = request(db, company(world), number)
    manual_requests.step(world.scoped())
    finish(db, "process_contract", number)
    add_reading(db, number)
    assert manual_requests.step(world.scoped()) == {"evaluating": 1}
    assert state(db, rid)[0] == "evaluating"


@needs_db
def test_a_reading_whose_requirements_were_never_derived_only_needs_the_summary(db, world):
    number, _ = contract(db, read=False)
    extraction = add_reading(db, number, derived=False)
    rid = request(db, company(world), number)
    assert manual_requests.step(world.scoped()) == {"reading": 1}
    (job,) = jobs_of(db, number)
    assert job["job_type"] == "derive_summary" and job["payload"]["extraction_id"] == extraction
    # Derived with nothing to evaluate and nothing else running: it will not come.
    finish(db, "derive_summary", number)
    assert manual_requests.step(world.scoped()) == {"failed": 1}
    assert state(db, rid) == ("failed", "reading_failed")


@needs_db
def test_a_reading_nothing_produces_anymore_fails_with_its_reason(db, world):
    veyon = company(world)
    nothing, _ = contract(db, read=False)
    pdf, _ = contract(db, read=False)
    word, _ = contract(db, read=False)
    held_one, _ = contract(db, read=False)
    ids = {name: request(db, veyon, number) for name, number in
           (("nothing", nothing), ("pdf", pdf), ("word", word), ("held", held_one))}
    manual_requests.step(world.scoped())
    assert {state(db, rid)[0] for rid in ids.values()} == {"reading"}
    for number in (nothing, pdf, word, held_one):
        finish(db, "process_contract", number)
    # A readable PDF was listed, yet no download or extraction is left: a failure.
    db.execute("""INSERT INTO contract_documents (id_contrato, id_contrato_archivo, categoria, filename, mime,
                    sha256_original, seace_download_url) VALUES (%s, %s, 1, 'tdr.pdf', 'application/pdf',
                    'pending:1', 'https://example.invalid')""", (pdf, pdf))
    # Only a Word file: SEACE listed no readable requirement.
    db.execute("""INSERT INTO contract_documents (id_contrato, id_contrato_archivo, categoria, filename, mime,
                    sha256_original, seace_download_url) VALUES (%s, %s, 1, 'tdr.docx', 'application/msword',
                    'pending:2', 'https://example.invalid')""", (word, word))
    world.scoped().enqueue("download_file", {"id_contrato": held_one, "id_contrato_archivo": 3}, queue_name="io")
    hold(db, held_one)
    assert manual_requests.step(world.scoped()) == {"failed": 4}
    assert state(db, ids["nothing"]) == ("failed", "no_documents")
    assert state(db, ids["pdf"]) == ("failed", "reading_failed")
    assert state(db, ids["word"]) == ("failed", "no_documents")
    assert state(db, ids["held"]) == ("failed", "reading_failed")  # Under manual review in the queue.


@needs_db
def test_a_deferred_job_is_still_work_in_flight_but_a_held_one_is_not(db, world):
    veyon = company(world)
    number, _ = contract(db, read=False)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    db.execute("UPDATE worker_jobs SET run_after = now() + interval '3 hours' WHERE job_type = 'process_contract'")
    assert manual_requests.step(world.scoped()) == {"waiting": 1} and state(db, rid)[0] == "reading"
    db.execute("UPDATE worker_jobs SET status = 'claimed' WHERE job_type = 'process_contract'")
    assert manual_requests.step(world.scoped()) == {"waiting": 1}
    hold(db, number, "process_contract")
    assert manual_requests.step(world.scoped()) == {"failed": 1} and state(db, rid)[1] == "reading_failed"


# --- the timeout --------------------------------------------------------------------------------------------------

@needs_db
def test_a_request_stuck_for_a_day_in_reading_or_evaluating_fails_and_frees_the_pair(db, world):
    veyon, din = world.companies
    numbers = [contract(db, read=False)[0] for _ in range(4)]
    stuck_reading = request(db, veyon, numbers[0], status="reading", age_hours=25)
    stuck_evaluating = request(db, veyon, numbers[1], status="evaluating", age_hours=30)
    fresh = request(db, veyon, numbers[2], status="reading", age_hours=23)
    world.scoped().enqueue("process_contract", {"id_contrato": numbers[2], "segment": 43}, queue_name="io")
    waiting_for_another_runtime = request(db, din, numbers[3], status="pending", age_hours=26)
    repo = runtime.ScopedRepository(db, profile_id=veyon, batch_id="b", profile_ids=[veyon])
    stats = manual_requests.step(repo)
    assert stats["expired"] == 3
    assert state(db, stuck_reading) == ("failed", "reading_timeout")
    assert state(db, stuck_evaluating) == ("failed", "reading_timeout")
    assert state(db, waiting_for_another_runtime) == ("failed", "reading_timeout")
    assert state(db, fresh) == ("reading", None)  # Under a day, and its reading is still queued.
    again = request(db, veyon, numbers[0])  # The active-pair index no longer blocks a new request.
    assert state(db, again) == ("pending", None)


@needs_db
def test_a_pending_request_the_runtime_serves_is_claimed_not_expired(db, world):
    number, _ = contract(db, closes_in_hours=-9, state=3, read=False)
    rid = request(db, company(world), number, age_hours=40)
    assert manual_requests.step(world.scoped()) == {"reading": 1}
    assert state(db, rid) == ("reading", None)


# --- evaluation end to end -------------------------------------------------------------------------------------------

@needs_db
def test_a_full_manual_evaluation_of_a_closed_contract_stores_the_match_and_finishes_the_request(
        db, world, gemini, tmp_path):
    veyon = company(world)
    number, extraction = contract(db, closes_in_hours=-72, state=3, facets=1)
    rule(db, veyon, enabled=False, max_daily_evaluations=1, min_hours_before_close=72)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    gemini.return_value = reply()
    before = notification_traces(db)
    daily = _automatic_jobs_today(world.scoped(), veyon)
    job = claim_evaluation(db, world)
    outcome = runtime.dispatch(settings(), world.scoped(), job, tmp_path)
    assert outcome["outcome"] == "evaluated" and outcome["provider_answered"] is True
    match = stored_match(db, number, veyon)
    meta = match["breakdown_json"]["meta"]
    assert (match["score"], match["verdict"]) == (90, "verde") and match["business_line_id"] is not None
    assert meta["evaluation_origin"] == MANUAL_ORIGIN and meta["manual_request_id"] == rid
    assert "jev_decision_id" not in meta and "jev_score" not in meta
    assert meta["execution"] == MANUAL_SOURCE and meta["extraction_id"] == extraction and meta["cost_usd"] > 0
    assert db.execute("SELECT event_type FROM match_events WHERE match_id = %s", (match["id"],)
                      ).fetchone()["event_type"] == "analysis"
    assert state(db, rid) == ("done", None) and notification_traces(db) == before
    assert _automatic_jobs_today(world.scoped(), veyon) == daily  # A manual job never counts against the limit.
    assert gemini.call_count == 1


@needs_db
def test_asking_again_for_a_graded_pair_pays_nothing(db, world, gemini, tmp_path):
    veyon = company(world)
    number, _ = contract(db, closes_in_hours=-72, state=3, facets=1)
    request(db, veyon, number)
    manual_requests.step(world.scoped())
    gemini.return_value = reply()
    runtime.dispatch(settings(), world.scoped(), claim_evaluation(db, world), tmp_path)
    second = request(db, veyon, number)  # The first one is done, so the pair is free to ask again.
    assert manual_requests.step(world.scoped()) == {"done": 1}
    assert state(db, second) == ("done", None) and len(jobs_of(db, number, "analyze_match")) == 1
    assert gemini.call_count == 1


@needs_db
def test_an_invalid_answer_fails_the_request_and_the_same_reading_is_not_sent_again_by_either_flow(
        db, world, gemini, tmp_path):
    veyon = company(world)
    number, extraction = contract(db, facets=2)  # The mocked answer judges only one of the two requirements.
    decide(db, number, veyon)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    gemini.return_value = reply()
    job = claim_evaluation(db, world)
    outcome = runtime.dispatch(settings(), world.scoped(), job, tmp_path)
    assert outcome == {"outcome": INVALID_OUTCOME, "reason": "requirement_coverage_changed",
                       "extraction_id": extraction, "provider_answered": True}
    assert state(db, rid) == ("failed", "invalid_model_output") and stored_match(db, number, veyon) is None
    db.execute("UPDATE worker_jobs SET payload = payload || %s::jsonb, status = 'done' WHERE id = %s",
               (json.dumps({"runtime_result": outcome}), job.id))
    # The automatic flow keeps the pair out of its queue...
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert stats["enqueued"] == 0 and stats["skipped"] == {"model_output_invalid": 1}
    # ...and so does a new request for the same reading: it fails at once, without a job.
    again = request(db, veyon, number)
    assert manual_requests.step(world.scoped()) == {"failed": 1}
    assert state(db, again) == ("failed", "invalid_model_output") and len(jobs_of(db, number, "analyze_match")) == 1
    assert gemini.call_count == 1


@needs_db
def test_a_job_whose_request_timed_out_is_not_paid(db, world, gemini, tmp_path):
    veyon = company(world)
    number, _ = contract(db, facets=1)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    db.execute("UPDATE manual_grading_requests SET updated_at = now() - interval '25 hours' WHERE id = %s", (rid,))
    manual_requests.step(world.scoped())
    assert state(db, rid) == ("failed", "reading_timeout")
    outcome = runtime.dispatch(settings(), world.scoped(), claim_evaluation(db, world), tmp_path)
    assert outcome == {"outcome": "skipped", "reason": "request_not_active"}
    gemini.assert_not_called()
    assert state(db, rid) == ("failed", "reading_timeout") and stored_match(db, number, veyon) is None


@needs_db
def test_a_company_deactivated_while_the_job_waited_fails_the_request_without_paying(db, world, gemini, tmp_path):
    veyon = company(world)
    number, _ = contract(db, facets=1)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    db.execute("UPDATE company_profiles SET is_active = false WHERE id = %s", (veyon,))
    outcome = runtime.dispatch(settings(), world.scoped(), claim_evaluation(db, world), tmp_path)
    assert outcome["outcome"] == "skipped" and state(db, rid) == ("failed", "profile_inactive")
    gemini.assert_not_called()


@needs_db
def test_a_lost_or_held_evaluation_job_fails_its_request(db, world):
    veyon = company(world)
    number, _ = contract(db)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    assert state(db, rid)[0] == "evaluating" and manual_requests.step(world.scoped()) == {"waiting": 1}
    hold(db, number, "analyze_match")  # The runtime held it for manual review after an error.
    assert manual_requests.step(world.scoped()) == {"failed": 1}
    assert state(db, rid) == ("failed", "invalid_model_output")


# --- one paid call per pair ---------------------------------------------------------------------------------------

@needs_db
def test_an_automatic_job_already_queued_takes_the_pair_and_the_request_waits_for_its_match(db, world):
    veyon = company(world)
    number, extraction = contract(db)
    decide(db, number, veyon)
    assert enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)["enqueued"] == 1
    rid = request(db, veyon, number)
    assert manual_requests.step(world.scoped()) == {"waiting": 1} and state(db, rid)[0] == "pending"
    assert len(jobs_of(db, number, "analyze_match")) == 1  # Still only the automatic one.
    # The automatic job evaluates the pair: the request is graded without a second call.
    finish(db, "analyze_match", number)
    db.execute("""INSERT INTO matches (profile_id, id_contrato, score, verdict, breakdown_json)
                  VALUES (%s, %s, 80, 'verde', %s::jsonb)""",
               (veyon, number, json.dumps({"meta": {"extraction_id": extraction}})))
    assert manual_requests.step(world.scoped()) == {"done": 1}
    assert state(db, rid) == ("done", None) and len(jobs_of(db, number, "analyze_match")) == 1


@needs_db
def test_when_the_automatic_job_ends_without_a_match_the_request_queues_its_own(db, world):
    veyon = company(world)
    number, _ = contract(db)
    decide(db, number, veyon)
    enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    finish(db, "analyze_match", number)  # E.g. skipped because the company rule changed meanwhile.
    assert manual_requests.step(world.scoped()) == {"evaluating": 1}
    kinds = [(job["status"], job["payload"]["origin"]) for job in jobs_of(db, number, "analyze_match")]
    assert kinds == [("done", AUTO_ORIGIN), ("pending", MANUAL_ORIGIN)] and state(db, rid)[0] == "evaluating"


@needs_db
def test_a_manual_job_already_queued_takes_the_pair_from_the_automatic_hook_and_the_backfill(db, world):
    veyon = company(world)
    number, _ = contract(db)
    request(db, veyon, number)
    manual_requests.step(world.scoped())
    decide(db, number, veyon)  # JEV accepts the contract afterwards.
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=number)
    assert (stats["enqueued"], stats["already_queued"]) == (0, 1)
    assert [job["payload"]["origin"] for job in jobs_of(db, number, "analyze_match")] == [MANUAL_ORIGIN]


@needs_db
def test_the_derive_hook_queues_one_evaluation_when_the_pair_is_both_accepted_and_requested(db, world):
    veyon = company(world)
    number, _ = contract(db, read=False)
    decide(db, number, veyon)
    rid = request(db, veyon, number)
    manual_requests.step(world.scoped())
    extraction = add_reading(db, number)
    result = derive(db, world, number, extraction, review=False)
    assert result["evaluations"]["enqueued"] == 1
    assert result["manual_requests"] == {"waiting": 1} and state(db, rid)[0] == "reading"
    assert [job["payload"]["origin"] for job in jobs_of(db, number, "analyze_match")] == [AUTO_ORIGIN]


@needs_db
def test_manual_jobs_leave_the_automatic_daily_limit_and_counts_unchanged(db, world):
    veyon = company(world)
    rule(db, veyon, max_daily_evaluations=1)
    mine = contract(db)[0]
    request(db, veyon, mine)
    manual_requests.step(world.scoped())
    assert len(jobs_of(db, mine, "analyze_match")) == 1 and _automatic_jobs_today(world.scoped(), veyon) == 0
    accepted = contract(db)[0]
    decide(db, accepted, veyon)
    stats = enqueue_evaluations(world.scoped(), profile_ids=world.companies, contract_id=accepted)
    assert (stats["enqueued"], stats["daily_limited"]) == (1, 0)
    assert _automatic_jobs_today(world.scoped(), veyon) == 1


# --- robustness -----------------------------------------------------------------------------------------------------

@needs_db
def test_one_broken_request_fails_alone_and_the_rest_go_on(db, world, capsys):
    veyon = company(world)
    good, _ = contract(db, read=False)
    bad, _ = contract(db, read=False)
    good_id, bad_id = request(db, veyon, good, age_hours=2), request(db, veyon, bad, age_hours=1)
    real = manual_requests.pair_row

    def flaky(conn, contract_id, profile_id):
        if contract_id == bad:
            raise ValueError("synthetic")
        return real(conn, contract_id, profile_id)

    with patch.object(manual_requests, "pair_row", flaky):
        assert manual_requests.step(world.scoped()) == {"reading": 1, "failed": 1}
    assert state(db, good_id) == ("reading", None) and state(db, bad_id) == ("failed", "reading_failed")
    assert json.loads(capsys.readouterr().out.strip()) == {"manual_request_failed": bad_id, "error_type": "ValueError"}


@needs_db
def test_a_database_error_in_the_manual_step_never_stops_the_automatic_work(db, world):
    with patch.object(manual_requests, "lock_requests", side_effect=psycopg.errors.DataError("synthetic")):
        assert manual_requests.step(world.scoped()) == {"error": 1}
        assert manual_requests.advance_reading(world.scoped(), 1) == {"error": 1}


def test_the_reading_job_is_built_by_the_same_function_as_the_discovery_admission():
    from buenapro_worker.jobs import poll_prod6_profile
    assert manual_requests.enqueue_process_contract is poll_prod6_profile.enqueue_process_contract
    repo = scoped()
    with patch("buenapro_worker.queue.repository.JobRepository.enqueue", return_value=4) as enqueue:
        assert poll_prod6_profile.enqueue_process_contract(repo, 42, segment=43, batch_id="b", dedup_key="k") == 4
    assert enqueue.call_args.args[:2] == ("process_contract", {
        "id_contrato": 42, "segment": 43, "batch_id": "b", "source_runtime": runtime.NAMESPACE,
        "scope_profile_id": "veyon", **repo.reading_policy})
    assert enqueue.call_args.kwargs["queue_name"] == "io" and enqueue.call_args.kwargs["priority"] == 2


@needs_db
def test_a_lost_database_connection_is_not_swallowed(db, world):
    number, _ = contract(db, read=False)
    request(db, company(world), number)
    with patch.object(manual_requests, "pair_row", side_effect=psycopg.OperationalError("down")):
        with pytest.raises(psycopg.OperationalError):
            manual_requests.step(world.scoped())


@needs_db
def test_a_worker_deployed_before_the_migration_just_has_no_requests(db, world):
    db.execute("DROP TABLE manual_grading_requests")
    assert manual_requests.step(world.scoped()) == {}
    assert manual_requests.pending_ids(db, world.companies) == set()
    assert manual_requests.reading_requested(db, 123) is False
    assert manual_requests.advance_reading(world.scoped(), 123) == {}
    number, _ = contract(db, closes_in_hours=-9, state=3, read=False)
    world.scoped().enqueue("process_contract", {"id_contrato": number, "segment": 43}, queue_name="io")
    outcome = runtime.dispatch(settings(), world.scoped(), claim(db, world, "process_contract"), Path("unused"))
    assert outcome == {"outcome": "closed_preserved"}


@needs_db
def test_the_migration_is_reapplicable_and_keeps_one_active_request_per_pair(db, world):
    migration = Path(__file__).resolve().parents[3] / "db" / "migrations" / "0050_manual_grading_requests.sql"
    before = db.execute("SELECT count(*) AS n FROM manual_grading_requests").fetchone()["n"]
    for _ in range(2):
        db.execute(migration.read_text(encoding="utf-8"))
    assert db.execute("SELECT count(*) AS n FROM manual_grading_requests").fetchone()["n"] == before
    veyon = company(world)
    first = request(db, veyon, 4242)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            request(db, veyon, 4242, status="reading")
    db.execute("UPDATE manual_grading_requests SET status = 'done' WHERE id = %s", (first,))
    request(db, veyon, 4242)  # A finished request frees the pair.
    valid = {"source_key": "seace_prod6", "mode": "queued", "status": "pending", "profile": veyon}
    for invalid in ({"mode": "inventado"}, {"status": "inventado"}, {"source_key": "inventado"}):
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.transaction():
                db.execute("""INSERT INTO manual_grading_requests (tenant_id, profile_id, source_key, source_id,
                                mode, status) SELECT tenant_id, id, %(source_key)s, 1, %(mode)s, %(status)s
                              FROM company_profiles WHERE id = %(profile)s""", valid | invalid)
    row = db.execute("SELECT lima_day, (now() AT TIME ZONE 'America/Lima')::date AS today FROM "
                     "manual_grading_requests WHERE id = %s", (first,)).fetchone()
    assert row["lima_day"] == row["today"]
