"""National listing (discovery without keywords) and the reading queue in the selected coordinator.

Synthetic and offline: no SEACE, PostgreSQL, Redis or provider call.
"""
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pytest

from test_prod2_selected_script import args, subject

NATIONAL = ("--national-listing", "--from-date", "2026-10-17", "--to-date", "2026-10-19", "--years", "2026")


def national_manifest(*values, **extra):
    return subject.plan(args(*NATIONAL, *values)) | {"run_id": "synthetic", "commercial_profiles": {},
        "gemini_model": "synthetic", "resolved_years": [2026], "published_from": "2026-10-17",
        "published_to": "2026-10-19", **extra}


# --- Planning -------------------------------------------------------------------------------------------------

def test_the_national_listing_plan_is_one_pseudo_keyword_without_probes_and_only_when_chosen():
    plan = subject.plan(args(*NATIONAL))
    assert plan["keywords"] == [""] and plan["lanes"] == []
    assert plan["discovery_policy"] == "prod2_national_listing_v1" and plan["probe_empty_scopes"] is False
    assert plan["from_date"] == "2026-10-17" and plan["to_date"] == "2026-10-19" and plan["years"] == [2026]
    assert "reading_queue" not in plan and "llm1_min_affinity" not in plan
    for keyword_mode in (args(), args("--all-keywords"), args("--keyword-preset", "digitales30", "--sweep-id", "s")):
        legacy = subject.plan(keyword_mode)
        assert "discovery_policy" not in legacy and "reading_queue" not in legacy
        assert "" not in legacy["keywords"]


@pytest.mark.parametrize("selector", [["--keyword", "software"], ["--lane", "prioritarias"], ["--all-keywords"],
    ["--business-batch", "ia_datos"], ["--business-scope", "digitales"],
    ["--keyword-preset", "digitales30", "--sweep-id", "initial"]])
def test_the_national_listing_excludes_every_keyword_selector(selector):
    with pytest.raises(ValueError, match="excludes every keyword selector"):
        subject.plan(args(*NATIONAL, *selector))


@pytest.mark.parametrize("values,message", [
    (["--national-listing"], "explicit --from-date and --to-date"),
    (["--national-listing", "--from-date", "2026-10-17"], "explicit --from-date and --to-date"),
    (["--national-listing", "--to-date", "2026-10-19"], "explicit --from-date and --to-date"),
    (["--national-listing", "--from-date", "2026-10-19", "--to-date", "2026-10-17"], "reversed"),
    (["--national-listing", "--from-date", "2026-09-01", "--to-date", "2026-10-19"], "exceeds 31 days"),
    (["--national-listing", "--from-date", "2025-12-31", "--to-date", "2026-01-01", "--years", "2026"],
     "exercise years"),
    (["--national-listing", "--from-date", "2026-10-17", "--to-date", "2026-10-19", "--years", "2026,2026"],
     "Invalid exercise years"),
])
def test_the_national_listing_needs_an_explicit_bounded_window_and_its_years(values, message):
    with pytest.raises(ValueError, match=message):
        subject.plan(args(*values))


def test_the_national_listing_accepts_auto_or_the_covering_years():
    window = ("--national-listing", "--from-date", "2025-12-31", "--to-date", "2026-01-01")
    assert subject.plan(args(*window))["years"] == "auto"
    assert subject.plan(args(*window, "--years", "2025,2026"))["years"] == [2025, 2026]
    assert subject.plan(args(*window, "--years", "2026,2025,2024"))["years"] == [2026, 2025, 2024]


def test_the_national_listing_cannot_join_an_id_import():
    with pytest.raises(ValueError, match="ID import cannot"):
        subject.plan(args(*NATIONAL, "--mapping-handoff", "handoff", "--selection", "selection",
                          "--selection-sha256", "0" * 64))


def test_the_reading_queue_needs_a_minimum_affinity_above_the_jev_threshold():
    for values in ([], ["--llm1-min-affinity", "60"]):
        with pytest.raises(ValueError, match="above the Jev threshold"):
            subject.plan(args("--reading-queue", *values))
    queued = subject.plan(args("--reading-queue", "--llm1-min-affinity", "85"))
    assert queued["reading_queue"] is True and queued["llm1_min_affinity"] == 85
    both = subject.plan(args(*NATIONAL, "--reading-queue", "--llm1-min-affinity", "85"))
    assert both["reading_queue"] is True and both["discovery_policy"] == "prod2_national_listing_v1"
    assert {k: v for k, v in queued.items() if k != "reading_queue"} == subject.plan(
        args("--llm1-min-affinity", "85"))


def test_the_reading_queue_cannot_join_an_id_import():
    with pytest.raises(ValueError, match="ID import cannot"):
        subject.plan(args("--reading-queue", "--llm1-min-affinity", "85", "--mapping-handoff", "handoff",
                          "--selection", "selection", "--selection-sha256", "0" * 64))


def test_a_frozen_national_manifest_differs_from_the_keyword_and_queue_plans(tmp_path):
    options = args(*NATIONAL, "--folder", str(tmp_path))
    frozen = subject.plan(options)
    keyword = subject.plan(args("--keyword", "software", "--from-date", "2026-10-17", "--to-date", "2026-10-19",
                                "--years", "2026", "--folder", str(tmp_path)))
    queued = subject.plan(args(*NATIONAL, "--reading-queue", "--llm1-min-affinity", "85"))
    # A resume compares the whole ``requested``; any of these would be refused against the frozen national one.
    assert frozen != keyword and frozen != queued and subject.plan(options) == frozen


def test_the_national_queries_are_one_per_day_and_object_and_need_their_resolved_year():
    queries = subject.planned_queries(national_manifest())
    assert len(queries) == 6 and {q["keyword"] for q in queries} == {""}
    assert [q["published_from"] for q in queries[::2]] == ["2026-10-17", "2026-10-18", "2026-10-19"]
    assert all(q["published_from"] == q["published_to"] for q in queries)
    legacy = subject.plan(args("--keyword", "software", "--from-date", "2026-10-17", "--to-date", "2026-10-19"))
    keyword_queries = subject.planned_queries(legacy | {"resolved_years": [2026], "published_from": "2026-10-17",
                                                        "published_to": "2026-10-19"})
    assert len(keyword_queries) == 2 and {q["published_to"] for q in keyword_queries} == {"2026-10-19"}
    with pytest.raises(ValueError, match="resolved years"):
        subject.planned_queries(national_manifest(resolved_years=[2025]))


def test_the_queue_identity_changes_only_with_the_new_policies():
    base = subject.plan(args("--keyword-preset", "digitales30", "--sweep-id", "initial")) | {
        "run_id": "batch1", "commercial_profiles": {}, "gemini_model": "synthetic"}
    identity = subject.staging_identity(base)
    assert "discovery_policy" not in base and "reading_queue" not in base
    national = national_manifest("--sweep-id", "initial")
    assert subject.staging_identity(national) != identity
    assert subject.staging_identity(national | {"reading_queue": True}) != subject.staging_identity(national)
    assert subject.staging_identity(national | {"run_id": "other", "published_to": "2026-10-20"}) == (
        subject.staging_identity(national))


def test_the_national_dry_run_is_offline_and_counts_days_times_objects(monkeypatch, capsys):
    def forbidden(*a, **kw):
        raise AssertionError("External call from the national dry run")
    monkeypatch.setattr(subject, "runtime", forbidden)
    monkeypatch.setattr(subject, "source_factory", forbidden)
    monkeypatch.setattr(subject.sys, "argv", ["run_prod2_selected.py", "--folder", "tmp/national-dry", *NATIONAL,
        "--reading-queue", "--llm1-min-affinity", "85", "--dry-run"])
    assert subject.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["base_queries"] == 6 and plan["coverage"].startswith("national_listing_by_publication_day")
    assert plan["discovery_policy"] == "prod2_national_listing_v1" and plan["reading_queue"] is True


# --- Sweep ------------------------------------------------------------------------------------------------------

def test_the_national_sweep_asks_without_a_keyword_for_each_day_and_skips_the_empty_scope_probe(tmp_path, monkeypatch):
    from test_prod2_keyword_sweep import Factory, row
    factory = Factory([row(0, "2026-10-17"), row(1, "2026-10-17"), row(2, "2026-10-18")])
    monkeypatch.setattr(subject, "source_factory", lambda pacer: factory())
    monkeypatch.setattr(subject, "STOP", threading.Event())
    manifest = national_manifest(probe_empty_scopes=True, objects=["service"], published_to="2026-10-18")
    offered = []

    class Stage:
        def stage(self, candidate_id, payload):
            offered.append(payload)
            return True

        def stats(self):
            return {"seen": len(offered)}

    limits = subject.RunLimits(max_accepted=10, max_candidates=100, max_jev_calls=100, max_llm1_calls=10)
    reports, proofs = subject.run_sweeps(manifest, tmp_path, subject.SharedPacer(2), Stage(), limits)
    assert proofs == [] and len(reports) == 2 and all(r["coverage_complete"] for r in reports)
    assert {r["query"]["published_from"] for r in reports} == {"2026-10-17", "2026-10-18"}
    assert factory.sessions == 4  # Two sessions per day; no scope probe was made.
    searches = [event for event in factory.events if event[0] == "search"]
    assert [event[1] for event in searches] == ["", "", "", ""]
    assert all(event[2]["allow_empty_keyword"] is True for event in searches)
    assert sorted(item["row"]["convocation_id"] for item in offered) == [2000, 2001, 2002]
    assert all(item["query"]["keyword"] == "" and "discovery_business_context" not in item for item in offered)


def test_a_capped_national_day_is_blocked_for_review_not_split(tmp_path, monkeypatch):
    from test_prod2_keyword_sweep import Factory, row

    def capped(result, number):
        result["counter"] = [1, 2, 499, 1, 250]
        result["snapshot"].update(total_rows=499, total_pages=250)

    factory = Factory([row(0, "2026-10-17"), row(1, "2026-10-17")], mutate=capped)
    monkeypatch.setattr(subject, "source_factory", lambda pacer: factory())
    monkeypatch.setattr(subject, "STOP", threading.Event())
    manifest = national_manifest(objects=["service"], published_to="2026-10-17")
    limits = subject.RunLimits(max_accepted=10, max_candidates=100, max_jev_calls=100, max_llm1_calls=10)
    reports, _ = subject.run_sweeps(manifest, tmp_path, subject.SharedPacer(2), None, limits)
    assert len(reports) == 1 and reports[0]["status"] == "blocked" and not reports[0]["coverage_complete"]
    assert [e["code"] for e in reports[0]["errors"]] == ["capped_single_day_requires_review"]


# --- Consumer: the reading queue and the empty keyword ---------------------------------------------------------

class QueueConnection:
    """Records the statements of one consumer; ``enqueue_reading`` is the only write of the reading queue."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), params))


def consume_one(tmp_path, monkeypatch, *, affinity, manifest, prior=None, query=None):
    from buenapro_worker.prod2 import prior_reading_guard
    calls = {"enrich": [], "promote": []}
    connection = QueueConnection()

    class Stage:
        pending, acked = True, []

        def next(self):
            if not self.pending:
                return None
            self.pending = False
            return "123", {"row": {"convocation_id": 123, "description": "software", "object_type": "service"},
                "query": query or {"keyword": "software"}, "observed_at": "2026-10-09T00:00:00+00:00"}

        def ack(self, value):
            self.acked.append(value)

    @contextmanager
    def connect(*a, **kw):
        yield connection

    @contextmanager
    def permission(*a, **kw):
        yield lambda: None

    class Limits:
        def can_process(self, conv):
            return True
        model = permission

        def admit(self, conv, action):
            return action()

    def promote(*a, **kw):
        calls["promote"].append(kw)
        return {"created": False}

    def enrich(*a, **kw):
        calls["enrich"].append(kw)
        return {"status": "accepted_unread" if kw.get("defer_reading") else "llm1_complete"}

    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject, "provider_permission", permission)
    monkeypatch.setattr(subject, "AuditedJevClient", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", lambda *a, **kw: object())
    monkeypatch.setattr(subject, "jev_request_hash", lambda *a, **kw: "a" * 64)
    monkeypatch.setattr(subject, "paid_jev", lambda *a: ("a" * 64, {"affinity_index": affinity, "cost_usd": 0}, True))
    monkeypatch.setattr(subject.repo, "promote_accepted", promote)
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", lambda conn, **kw: prior)
    monkeypatch.setattr(subject, "enrich", enrich)
    monkeypatch.setattr(subject, "cleanup_finished_case", lambda *a: None)
    monkeypatch.setattr(subject, "STOP", threading.Event())
    stage = Stage()
    subject.consume(SimpleNamespace(database_url="synthetic"), stage, {
        "run_id": "synthetic-run", "commercial_profiles": {}, "profile_id": "profile", **manifest},
        tmp_path, None, Limits(), "synthetic")
    assert stage.acked == ["123"]
    return subject.read_private(tmp_path / "123/outcome.private.json"), calls, connection.statements


QUEUED = {"reading_queue": True, "llm1_min_affinity": 85}


def test_a_candidate_at_the_minimum_affinity_is_listed_unread_and_queued_once_for_reading(tmp_path, monkeypatch):
    outcome, calls, statements = consume_one(tmp_path, monkeypatch, affinity=90, manifest=QUEUED)
    assert calls["enrich"] == [{"defer_reading": True}]
    assert outcome["status"] == "accepted_unread" and outcome["accepted"] is True and outcome["reading_queued"] is True
    assert statements == [(
        "INSERT INTO prod2_reading_queue (id_convocatoria, affinity_index, enqueued_by) "
        "VALUES (%s, %s, %s) ON CONFLICT (id_convocatoria) DO NOTHING", (123, 90, "synthetic-run"))]


def test_the_minimum_affinity_itself_is_queued(tmp_path, monkeypatch):
    outcome, _, statements = consume_one(tmp_path, monkeypatch, affinity=85, manifest=QUEUED)
    assert outcome["reading_queued"] is True and statements[0][1] == (123, 85, "synthetic-run")


def test_a_candidate_below_the_minimum_affinity_is_listed_unread_and_not_queued(tmp_path, monkeypatch):
    outcome, calls, statements = consume_one(tmp_path, monkeypatch, affinity=70, manifest=QUEUED)
    assert calls["enrich"] == [{"defer_reading": True}] and statements == []
    assert outcome["status"] == "accepted_unread" and "reading_queued" not in outcome


def test_a_preserved_prior_reading_is_never_queued(tmp_path, monkeypatch):
    prior = {"status": "llm1_complete", "llm1_prior_preserved": True, "llm1_cost_usd": 0}
    outcome, calls, statements = consume_one(tmp_path, monkeypatch, affinity=95, manifest=QUEUED, prior=prior)
    assert calls["enrich"] == [] and statements == []
    assert outcome["status"] == "llm1_complete" and "reading_queued" not in outcome


def test_without_the_reading_queue_the_candidate_is_read_as_before(tmp_path, monkeypatch):
    outcome, calls, statements = consume_one(tmp_path, monkeypatch, affinity=90, manifest={"llm1_min_affinity": 85})
    assert calls["enrich"] == [{}] and statements == []
    assert outcome["status"] == "llm1_complete" and "reading_queued" not in outcome


def test_a_candidate_of_the_national_listing_is_promoted_with_its_empty_keyword_allowed(tmp_path, monkeypatch):
    query = {"keyword": "", "object_type": "service", "year": 2026, "published_from": "2026-10-09",
             "published_to": "2026-10-09", "seace_version": "3"}
    manifest = {**QUEUED, "discovery_policy": "prod2_national_listing_v1"}
    outcome, calls, _ = consume_one(tmp_path, monkeypatch, affinity=90, manifest=manifest, query=query)
    assert calls["promote"][0]["allow_empty_keyword"] is True and calls["promote"][0]["query_manifest"] == query
    assert "discovery_keyword" not in outcome and "business_batch_id" not in outcome
    assert outcome["reading_queued"] is True
    (tmp_path / "keyword").mkdir()
    _, keyword_calls, _ = consume_one(tmp_path / "keyword", monkeypatch, affinity=90, manifest=QUEUED)
    assert "allow_empty_keyword" not in keyword_calls["promote"][0]


def test_the_final_report_counts_queued_readings_as_settled_unread():
    report = {"llm1_complete": 1, "accepted_unread": 3, "accepted": 4, "source_coverage_complete": True,
              "redis": {"queued": 0, "pending": 0}, "costs": {"usage_unknown_attempts": 0},
              "records": [{"status": "llm1_complete"}, *({"status": "accepted_unread", "reading_queued": True},) * 3]}
    assert subject.completion_exit_code(report, {"all_results": True}) == 0
    assert subject.completion_exit_code(report, {"all_results": False, "effective_limits": {"max_accepted": 4}}) == 0


def test_the_report_names_the_new_policies_only_when_the_run_uses_them():
    outcomes = [{"reading_queued": True}, {"reading_queued": True}, {}, {"reading_queued": False}]
    assert subject.new_policy_report_fields({}, outcomes) == {}
    assert subject.new_policy_report_fields({"llm1_min_affinity": 85}, outcomes) == {}
    assert subject.new_policy_report_fields({"reading_queue": True}, outcomes) == {"reading_queued": 2}
    assert subject.new_policy_report_fields({"reading_queue": True}, []) == {"reading_queued": 0}
    assert subject.new_policy_report_fields({"discovery_policy": "prod2_national_listing_v1"}, outcomes) == {
        "discovery_policy": "prod2_national_listing_v1", "current_day_partial": []}


def test_the_detail_of_a_listing_candidate_is_searched_without_a_keyword(tmp_path, monkeypatch):
    import run_prod2_accepted as helper
    calls = []

    @contextmanager
    def client(**kw):
        yield object()

    class Source:
        def __init__(self, http):
            pass

        def search(self, keyword, **kw):
            calls.append((keyword, kw.get("allow_empty_keyword")))
            raise RuntimeError("stop after the search")

    monkeypatch.setattr(helper, "seace_http_client", client)
    monkeypatch.setattr(helper, "Prod2HttpClient", Source)
    query = {"keyword": "", "object_type": "service", "year": 2026, "published_from": "2026-10-01",
             "published_to": "2026-10-01", "seace_version": "3"}
    with pytest.raises(RuntimeError, match="stop after"):
        subject.get_detail({"query": query}, tmp_path / "listing", lambda _: None)
    with pytest.raises(RuntimeError, match="stop after"):
        subject.get_detail({"query": {**query, "keyword": "software"}}, tmp_path / "keyword", lambda _: None)
    assert calls == [("", True), ("software", None)]
