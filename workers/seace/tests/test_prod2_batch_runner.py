"""Offline batch contracts: synthetic rows, no database or provider calls."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import stat
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION
from buenapro_worker.prod2 import batch_runner as runner
from buenapro_worker.prod2.batch_plan import (
    MEMBER_IDS, configured_keywords, freeze_plan, months_before, native_arguments,
    page_capture, publication_window,
)
from buenapro_worker.prod2.discovery import validate_capture


NOW = datetime(2026, 10, 5, 17, tzinfo=timezone.utc)
QUERY = {"keyword": "SOFTWARE", "object_type": "service", "year": 2026,
         "published_from": "2026-10-01", "published_to": "2026-10-05", "seace_version": "3"}


def search_row(index=0):
    return {"procedure_id": 1000 + index, "convocatoria_id": 2000 + index,
            "entity": "SYNTHETIC BUYER", "published_at": "03/10/2026 12:30",
            "nomenclature": f"SYNTHETIC-2026-{index + 1}", "object": "Servicio",
            "description": "SOFTWARE", "amount_text": "1,234.56", "currency": "Soles",
            "seace_version": "3"}


def page_result(number=1, *, total=3, size=2, rows=None):
    first, last = (number - 1) * size, min(number * size, total)
    pages = max(1, (total + size - 1) // size)
    return {"rows": [search_row(i) for i in range(first, last)] if rows is None else rows,
            "counter": [first + 1 if total else 0, last, total, number, pages],
            "search_verified": True, "counter_origin": "synthetic_native_counter",
            "coverage_complete": False,
            "snapshot": {"page_size": size, "total_rows": total, "total_pages": pages,
                         "year": QUERY["year"], "year_filter_kind": "convocatoria_year",
                         "object_type": "Servicio", "keyword": QUERY["keyword"],
                         "published_from": "01/10/2026", "published_to": "05/10/2026",
                         "native_atomic_snapshot": False}}


def frozen_plan():
    return freeze_plan(as_of=NOW, keywords=["SOFTWARE"], years=[2026, 2025],
                       published_from=QUERY["published_from"], published_to=QUERY["published_to"],
                       commercial_profiles=[{"descripcion": "Tecnología", "lineas": ["Software"]},
                                            {"descripcion": "Servicios TI", "lineas": ["Cloud"]}],
                       profile_id=MEMBER_IDS[0], jev_model=JEV_MODEL, jev_policy=JEV_POLICY_VERSION,
                       keyword_config_hash="a" * 64)


def test_plan_uses_six_calendar_months_and_lima_day():
    assert publication_window(NOW) == ("2026-04-05", "2026-10-05")
    assert publication_window(datetime(2026, 10, 6, 2, tzinfo=timezone.utc)) == (
        "2026-04-05", "2026-10-05")
    assert months_before(date(2024, 8, 31), 6) == date(2024, 2, 29)
    assert months_before(date(2025, 8, 31), 6) == date(2025, 2, 28)


@pytest.mark.parametrize("months", [0, -1, 121, True, 1.5])
def test_plan_rejects_invalid_month_bounds(months):
    with pytest.raises(ValueError):
        months_before(date(2026, 10, 5), months)


def test_plan_accepts_prior_exercise_year_for_current_publication_window():
    plan = frozen_plan()
    assert plan["procedure_years"] == [2026, 2025]
    assert plan["published_from"] == "2026-10-01"
    assert plan["pdf_downloads"] is False
    assert plan["gemini"] is False
    assert plan["save_rejected_jev"] is True
    assert plan["ficha_for_all_unique_records"] is True
    assert plan["source_atomic_snapshot"] is False


def test_frozen_plan_is_independent_of_mutations_to_profile_keyword_inputs():
    keywords = ["SOFTWARE"]
    profiles = [{"descripcion": "Tecnología", "lineas": ["Software"]},
                {"descripcion": "Servicios TI", "lineas": ["Cloud"]}]
    plan = freeze_plan(as_of=NOW, keywords=keywords, years=[2026],
                       published_from=QUERY["published_from"], published_to=QUERY["published_to"],
                       commercial_profiles=profiles, profile_id=MEMBER_IDS[0], jev_model=JEV_MODEL,
                       jev_policy=JEV_POLICY_VERSION, keyword_config_hash="a" * 64)
    before = deepcopy(plan)
    keywords.append("NETWORK")
    profiles[0]["lineas"].append("Consultoría")
    assert plan == before


def test_frozen_member_ids_are_not_the_global_authorized_id_inventory():
    original = list(MEMBER_IDS)
    plan = frozen_plan()
    plan["member_profile_ids"].append("synthetic-other")
    observed = list(MEMBER_IDS)
    MEMBER_IDS[:] = original
    assert observed == original


@pytest.mark.parametrize("start,end", [("2026-10-06", "2026-10-05"),
                                       ("2026-10-01", "2026-10-06")])
def test_plan_rejects_reversed_or_future_publication_window(start, end):
    with pytest.raises(ValueError):
        publication_window(NOW, published_from=start, published_to=end)


def test_plan_keyword_inventory_requires_nonempty_unique_keywords():
    assert configured_keywords({"lanes": [{"keywords": [" SOFTWARE "]}, {"keywords": ["CLOUD"]}]}) == ["SOFTWARE", "CLOUD"]
    for words in ([], [""], ["Software", "SOFTWARE"], ["Software", " Software "]):
        with pytest.raises(ValueError):
            configured_keywords({"lanes": [{"keywords": words}]})


@pytest.mark.parametrize("object_type,label", [("service", "Servicio"), ("good", "Bien")])
def test_plan_native_arguments_translate_only_transport_fields(object_type, label):
    query = {**QUERY, "object_type": object_type, "year": 2025}
    assert native_arguments(query) == {"year": 2025, "object_type": label,
                                       "published_from": "01/10/2026", "published_to": "05/10/2026"}
    assert query["year"] == 2025


def test_capture_maps_official_ids_without_exporting_session_material():
    result = page_result(total=1)
    result["rows"][0].update(onclick="not durable", cookie="not durable")
    capture = page_capture(QUERY, result, NOW)
    assert capture["rows"] == [{"procedure_id": 1000, "convocation_id": 2000,
        "nomenclature": "SYNTHETIC-2026-1", "buyer_name": "SYNTHETIC BUYER",
        "published_at": "03/10/2026 12:30", "description": "SOFTWARE", "object_label": "Servicio",
        "reference_amount": "1,234.56", "currency": "Soles", "seace_version": "3"}]
    assert validate_capture(capture, now=NOW).rows[0]["convocation_id"] == 2000
    assert capture["page"] == {"number": 1, "total_pages": 1, "total_results": 1, "page_size": 2}


def test_zero_capture_is_a_complete_empty_native_page_not_a_missing_result():
    capture = page_capture(QUERY, page_result(total=0), NOW)
    assert capture["rows"] == []
    assert validate_capture(capture, now=NOW).page["total_results"] == 0


@pytest.mark.parametrize("mutation", ["unverified", "counter_missing", "snapshot_changed"])
def test_capture_rejects_unverified_or_mismatched_counter(mutation):
    result = page_result()
    if mutation == "unverified":
        result["search_verified"] = False
    elif mutation == "counter_missing":
        result["counter"] = []
    else:
        result["snapshot"]["total_rows"] += 1
    with pytest.raises(ValueError):
        page_capture(QUERY, result, NOW)


class FakeSource:
    def __init__(self, pages, events, name="source"):
        self.pages, self.events, self.name = deepcopy(pages), events, name
        self.rows = []

    def search(self, keyword, **kwargs):
        self.events.append(("search", self.name, keyword, kwargs))
        self.rows = deepcopy(self.pages[1]["rows"])
        return deepcopy(self.pages[1])

    def search_page(self, number):
        self.events.append(("page", self.name, number))
        self.rows = deepcopy(self.pages[number]["rows"])
        return deepcopy(self.pages[number])

    def detail(self, index, *, all_documents=False):
        self.events.append(("detail", self.name, index, all_documents))
        expected = self.rows[index]
        return {**deepcopy(expected), "selection_type": "Concurso Público", "item_states": ["Convocado"],
                "schedule": [], "documents": [], "document_inventory_total": 0,
                "document_inventory_complete": True}


def source_factory(sources, events):
    pending = iter(sources)
    @contextmanager
    def fresh():
        source = next(pending)
        events.append(("enter_source", source.name))
        yield source
        events.append(("exit_source", source.name))
    return fresh


class FakeRepo:
    def __init__(self, events):
        self.events, self.captures, self.checkpoints = events, [], []
        self.saved_pass = None

    def discovery_checkpoint(self, conn, task_id, owner_token):
        return {"saved_pass": self.saved_pass}

    def renew_lease(self, conn, task_id, owner_token, **kwargs):
        self.events.append(("renew", task_id))
        return {}

    def update_checkpoint(self, conn, task_id, owner_token, checkpoint):
        self.checkpoints.append(deepcopy(checkpoint))
        self.events.append(("checkpoint", deepcopy(checkpoint)))

    def persist_page(self, conn, task_id, owner_token, capture):
        self.captures.append(deepcopy(capture))
        self.events.append(("persist", capture["page"]["number"]))
        number, pages = capture["page"]["number"], capture["page"]["total_pages"]
        return {"status": "complete" if number == pages else "running", "next_page": number + 1}

    def restart_discovery_task(self, conn, task_id, owner_token):
        self.events.append(("restart", task_id))
        return {"status": "restarted", "task_id": task_id}

    def split_capped_task(self, conn, task_id, owner_token, *, total_results):
        self.events.append(("split", total_results))
        return {"status": "split", "task_id": task_id}

    def discovery_locator(self, conn, run_id, conv):
        self.events.append(("locator", run_id, conv))
        native = search_row()
        return {"query_manifest": deepcopy(QUERY), "page_number": 1,
                "row_manifest": {"procedure_id": native["procedure_id"], "convocation_id": native["convocatoria_id"],
                        "nomenclature": native["nomenclature"], "buyer_name": native["entity"],
                        "description": native["description"], "object_label": native["object"]}}

    def save_participation_assessment(self, conn, conv, *, detail_hash, assessment):
        self.events.append(("assessment", conv, deepcopy(assessment)))
        assert assessment["eligibility_verified"] is False
        return {"id": 88}

    def complete_task(self, conn, task_id, owner_token, outcome):
        self.events.append(("complete", task_id, deepcopy(outcome)))
        return {"status": "complete"}


def task(checkpoint=None):
    return {"task_id": 7, "owner_token": "00000000-0000-0000-0000-000000000007",
            "query": deepcopy(QUERY), "checkpoint": deepcopy(checkpoint or {"next_page": 1}),
            "discovery_run_id": "synthetic-pass"}


def test_discovery_persists_all_pages_only_after_final_independent_reconciliation():
    events = []
    pages = {number: page_result(number, total=5) for number in (1, 2, 3)}
    repo = FakeRepo(events)
    factory = source_factory([FakeSource(pages, events, "initial"),
                              FakeSource({1: pages[1]}, events, "verification")], events)
    assert runner.discover_task(object(), task(), repo, factory) == "complete"
    assert [capture["page"]["number"] for capture in repo.captures] == [1, 2, 3]
    assert [len(capture["rows"]) for capture in repo.captures] == [2, 2, 1]
    final_persist = events.index(("persist", 3))
    final_search = next(i for i, event in enumerate(events) if event[:2] == ("search", "verification"))
    assert final_search < final_persist
    assert [event[2] for event in events if event[0] == "page"] == [2, 3]


@pytest.mark.parametrize("change", ["counter", "first_row", "capped"])
def test_changed_final_snapshot_restarts_before_last_page_can_be_complete(change):
    events = []
    pages = {number: page_result(number, total=5) for number in (1, 2, 3)}
    checked = page_result(total=6 if change == "counter" else 499 if change == "capped" else 5)
    if change == "first_row":
        checked["rows"][0]["description"] = "SOFTWARE UPDATED"
    repo = FakeRepo(events)
    factory = source_factory([FakeSource(pages, events, "initial"),
                              FakeSource({1: checked}, events, "verification")], events)
    assert runner.discover_task(object(), task(), repo, factory) == "restarted"
    assert [capture["page"]["number"] for capture in repo.captures] == [1, 2]
    assert ("restart", 7) in events
    assert ("persist", 3) not in events


def test_single_page_is_not_persisted_as_complete_before_verification():
    events = []
    initial, changed = page_result(total=1), page_result(total=2)
    repo = FakeRepo(events)
    factory = source_factory([FakeSource({1: initial}, events), FakeSource({1: changed}, events)], events)
    assert runner.discover_task(object(), task(), repo, factory) == "restarted"
    assert repo.captures == []


def test_zero_results_are_persisted_only_after_second_verified_empty_search():
    events = []
    empty = page_result(total=0)
    repo = FakeRepo(events)
    factory = source_factory([FakeSource({1: empty}, events, "initial"),
                              FakeSource({1: empty}, events, "verification")], events)
    assert runner.discover_task(object(), task(), repo, factory) == "complete"
    assert len(repo.captures) == 1
    assert repo.captures[0]["rows"] == []
    assert events.index(("persist", 1)) > next(i for i, e in enumerate(events) if e[:2] == ("search", "verification"))


def test_capped_discovery_splits_without_persisting_truncated_rows():
    events = []
    repo = FakeRepo(events)
    factory = source_factory([FakeSource({1: page_result(total=499)}, events)], events)
    assert runner.discover_task(object(), task(), repo, factory) == "split"
    assert ("split", 499) in events
    assert repo.captures == []


def test_resume_verifies_durable_first_page_then_persists_only_remaining_pages(monkeypatch):
    events = []
    pages = {number: page_result(number, total=5) for number in (1, 2, 3)}
    first_hash = validate_capture(page_capture(QUERY, pages[1], NOW), now=NOW).page_hash
    repo = FakeRepo(events)
    repo.saved_pass = {"first_page_hash": first_hash}
    factory = source_factory([FakeSource(pages, events, "resumed"),
                              FakeSource({1: pages[1]}, events, "verification")], events)
    assert runner.discover_task(object(), task({"next_page": 2}), repo, factory) == "complete"
    assert [capture["page"]["number"] for capture in repo.captures] == [2, 3]
    assert [event[2] for event in events if event[0] == "page"] == [2, 3]


@pytest.mark.parametrize("saved", [None, {"first_page_hash": "a" * 64}])
def test_resume_without_matching_first_page_restarts_before_sending_page_requests(monkeypatch, saved):
    events = []
    repo = FakeRepo(events)
    repo.saved_pass = saved
    factory = source_factory([FakeSource({1: page_result()}, events)], events)
    assert runner.discover_task(object(), task({"next_page": 2}), repo, factory) == "restarted"
    assert repo.captures == []
    assert not any(event[0] == "page" for event in events)


def test_enrichment_keeps_low_jev_relevance_and_unknown_calendar_without_pdf_or_gemini(monkeypatch, tmp_path):
    events = []
    repo = FakeRepo(events)
    monkeypatch.setattr(runner, "persist_detail", lambda conn, detail, **kwargs: {"detail_hash": "b" * 64})
    def cached_jev(conn, folder, conv, description, manifest, jev):
        events.append(("jev_cached", conv))
        return 99, {"affinity_index": 2.0}, True
    monkeypatch.setattr(runner, "paid_jev_stage", cached_jev)
    factory = source_factory([FakeSource({1: page_result(total=1)}, events)], events)
    owned = {**task(), "id_convocatoria": 2000, "run_id": "synthetic-run"}
    outcome = runner.enrich_task(object(), owned, repo, frozen_plan(), tmp_path, factory, object())
    assert outcome["affinity_index"] == 2.0
    assert outcome["temporal_state"] == "unknown"
    assert outcome["documents_downloaded"] == 0
    assert outcome["gemini_calls"] == 0
    assert outcome["jev_cache_hit"] is True
    assert ("detail", "source", 0, True) in events
    assert any(event[0] == "complete" for event in events)
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize("field", ["description", "entity", "nomenclature", "object"])
def test_ficha_locator_rejects_changed_frozen_identity_before_detail_or_payment(field):
    events = []
    repo = FakeRepo(events)
    locator = repo.discovery_locator(object(), "synthetic-run", 2000)
    result = page_result(total=1)
    result["rows"][0][field] = "SYNTHETIC CHANGED"
    source = FakeSource({1: result}, events)
    with pytest.raises(RuntimeError, match="changed"):
        runner._locate_ficha(source, locator["query_manifest"], locator["row_manifest"], 1)
    assert not any(event[0] == "detail" for event in events)


def test_ficha_locator_rechecks_procedure_and_convocation_pair():
    events = []
    locator = FakeRepo(events).discovery_locator(object(), "synthetic-run", 2000)
    result = page_result(total=1)
    result["rows"][0]["procedure_id"] += 1
    source = FakeSource({1: result}, events)
    with pytest.raises(RuntimeError, match="absent"):
        runner._locate_ficha(source, locator["query_manifest"], locator["row_manifest"], 1)
    assert not any(event[0] == "detail" for event in events)


def test_ficha_locator_follows_hint_page_with_current_page_row_index():
    events = []
    pages = {number: page_result(number, total=5) for number in (1, 2, 3)}
    target = search_row(4)
    row_manifest = {"procedure_id": target["procedure_id"], "convocation_id": target["convocatoria_id"],
                    "nomenclature": target["nomenclature"], "buyer_name": target["entity"],
                    "description": target["description"], "object_label": target["object"]}
    source = FakeSource(pages, events)
    detail = runner._locate_ficha(source, QUERY, row_manifest, 3)
    assert detail["convocatoria_id"] == target["convocatoria_id"]
    assert [event[2] for event in events if event[0] == "page"] == [3]
    assert ("detail", "source", 0, True) in events


class FakeConnection:
    def __init__(self):
        self.executed = []

    def execute(self, statement, params):
        self.executed.append((statement, params))


def paid_fixture(monkeypatch, *, status="claimed", result=None):
    claim = {"status": status, "id": 99, "claim_token": "00000000-0000-0000-0000-000000000099"}
    if result is not None:
        claim["result_json"] = result
    calls, completions = [], []
    def reserve(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(claim)
    def complete(*args, **kwargs):
        completions.append((args, kwargs))
    monkeypatch.setattr(runner, "claim_evaluation", reserve)
    monkeypatch.setattr(runner, "complete_evaluation", complete)
    return claim, calls, completions


def legitimate_jev_result(manifest=None, **overrides):
    manifest = manifest or frozen_plan()
    digest = runner.jev_request_hash("SOFTWARE", commercial_profiles=manifest["commercial_profiles"], model=manifest["jev_model"])
    return {"affinity_index": 52.0, "input_tokens": 900, "output_tokens": 12, "cost_usd": 0.003,
            "model": manifest["jev_model"], "policy_version": manifest["jev_policy_version"],
            "cache_key": "prod2:jev:" + digest, **overrides}


def test_paid_jev_durable_cache_returns_without_provider_call_or_private_write(monkeypatch, tmp_path):
    result = legitimate_jev_result(affinity_index=40.0, input_tokens=500, output_tokens=10, cost_usd=0.01)
    _, claims, completions = paid_fixture(monkeypatch, status="cached", result=result)
    jev = SimpleNamespace(classify=lambda *args, **kwargs: pytest.fail("cached Jev must not be called"))
    evaluation, actual, reused = runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", frozen_plan(), jev)
    assert (evaluation, actual, reused) == (99, result, True)
    assert len(claims) == 1
    assert completions == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", ["busy", "uncertain", "failed"])
def test_pending_billing_state_without_response_never_retries_paid_jev(monkeypatch, tmp_path, status):
    _, _, completions = paid_fixture(monkeypatch, status=status)
    jev = SimpleNamespace(classify=lambda *args, **kwargs: pytest.fail("uncertain Jev must not be called"))
    with pytest.raises(RuntimeError, match="reconciliation"):
        runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", frozen_plan(), jev)
    assert completions == []


def test_claim_is_saved_before_payment_and_usage_completed_once(monkeypatch, tmp_path):
    _, claims, completions = paid_fixture(monkeypatch)
    result = legitimate_jev_result()
    def classify(*args, **kwargs):
        assert len(claims) == 1
        assert len(list(tmp_path.glob("*.claim.private.json"))) == 1
        return deepcopy(result)
    jev = SimpleNamespace(classify=classify)
    evaluation, actual, reused = runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", frozen_plan(), jev)
    assert (evaluation, actual, reused) == (99, result, False)
    assert len(completions) == 1
    assert completions[0][1] == {"input_tokens": 900, "output_tokens": 12, "cost_usd": 0.003}
    assert len(list(tmp_path.glob("*.response.private.json"))) == 1


@pytest.mark.parametrize("status", ["busy", "uncertain"])
def test_saved_response_can_complete_original_claim_without_paying_again(monkeypatch, tmp_path, status):
    result = legitimate_jev_result()
    owner, _, completions = paid_fixture(monkeypatch, status=status)
    owner = deepcopy(owner)
    owner["status"] = "claimed"
    manifest = frozen_plan()
    digest = runner.jev_request_hash("SOFTWARE", commercial_profiles=manifest["commercial_profiles"], model=manifest["jev_model"])
    stem = tmp_path / f"jev-{digest}"
    runner.private_write(stem.with_suffix(".claim.private.json"), owner)
    runner.private_write(stem.with_suffix(".response.private.json"), result)
    jev = SimpleNamespace(classify=lambda *args, **kwargs: pytest.fail("saved response must not trigger payment"))
    assert runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", manifest, jev) == (99, result, True)
    assert len(completions) == 1


def test_foreign_local_claim_is_not_completed_or_paid_again(monkeypatch, tmp_path):
    owner, _, completions = paid_fixture(monkeypatch, status="uncertain")
    owner = {**owner, "id": 1234, "status": "claimed"}
    manifest = frozen_plan()
    digest = runner.jev_request_hash("SOFTWARE", commercial_profiles=manifest["commercial_profiles"], model=manifest["jev_model"])
    stem = tmp_path / f"jev-{digest}"
    runner.private_write(stem.with_suffix(".claim.private.json"), owner)
    runner.private_write(stem.with_suffix(".response.private.json"), {"affinity_index": 52.0})
    jev = SimpleNamespace(classify=lambda *args, **kwargs: pytest.fail("foreign receipt must not pay"))
    with pytest.raises(RuntimeError, match="does not bind"):
        runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", manifest, jev)
    assert completions == []


def test_provider_failure_marks_reservation_uncertain_and_does_not_retry(monkeypatch, tmp_path):
    _, _, completions = paid_fixture(monkeypatch)
    connection, calls = FakeConnection(), []
    def fail(*args, **kwargs):
        calls.append(1)
        raise ValueError("synthetic provider failure")
    with pytest.raises(RuntimeError, match="billing is uncertain"):
        runner.paid_jev_stage(connection, tmp_path, 2000, "SOFTWARE", frozen_plan(), SimpleNamespace(classify=fail))
    assert calls == [1]
    assert completions == []
    assert len(connection.executed) == 1
    assert "status='uncertain'" in connection.executed[0][0]
    assert len(list(tmp_path.glob("*.failure.private.json"))) == 1


@pytest.mark.parametrize("field", ["model", "policy_version", "cache_key"])
def test_foreign_cached_jev_binding_is_rejected_without_payment(monkeypatch, tmp_path, field):
    result = legitimate_jev_result(**{field: "SYNTHETIC-WRONG-BINDING"})
    _, _, completions = paid_fixture(monkeypatch, status="cached", result=result)
    jev = SimpleNamespace(classify=lambda *args, **kwargs: pytest.fail("bad cache must not cause payment"))
    with pytest.raises(RuntimeError):
        runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", frozen_plan(), jev)
    assert completions == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["model", "policy_version", "cache_key"])
def test_foreign_local_response_binding_is_rejected_without_payment(monkeypatch, tmp_path, field):
    result = legitimate_jev_result(**{field: "SYNTHETIC-WRONG-BINDING"})
    owner, _, completions = paid_fixture(monkeypatch, status="uncertain")
    owner = {**owner, "status": "claimed"}
    manifest = frozen_plan()
    digest = runner.jev_request_hash("SOFTWARE", commercial_profiles=manifest["commercial_profiles"], model=manifest["jev_model"])
    stem = tmp_path / f"jev-{digest}"
    runner.private_write(stem.with_suffix(".claim.private.json"), owner)
    runner.private_write(stem.with_suffix(".response.private.json"), result)
    jev = SimpleNamespace(classify=lambda *args, **kwargs: pytest.fail("bad receipt must not cause payment"))
    with pytest.raises(RuntimeError):
        runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", manifest, jev)
    assert completions == []


@pytest.mark.parametrize("field", ["model", "policy_version", "cache_key"])
def test_fresh_foreign_provider_binding_is_not_completed_or_retried(monkeypatch, tmp_path, field):
    _, _, completions = paid_fixture(monkeypatch)
    calls = []
    def wrong_result(*args, **kwargs):
        calls.append(1)
        return legitimate_jev_result(**{field: "SYNTHETIC-WRONG-BINDING"})
    with pytest.raises(RuntimeError):
        runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", frozen_plan(),
                              SimpleNamespace(classify=wrong_result))
    assert calls == [1]
    assert completions == []


@pytest.mark.parametrize("result", [None, [], "not a result", {}, {"affinity_index": 52.0}])
def test_cached_jev_must_be_a_bound_result_object(monkeypatch, tmp_path, result):
    claim, _, completions = paid_fixture(monkeypatch, status="cached")
    claim["result_json"] = result
    jev = SimpleNamespace(classify=lambda *args, **kwargs: pytest.fail("invalid cache must not cause payment"))
    with pytest.raises(RuntimeError):
        runner.paid_jev_stage(FakeConnection(), tmp_path, 2000, "SOFTWARE", frozen_plan(), jev)
    assert completions == []


def test_private_write_publishes_only_complete_json_with_private_permissions(monkeypatch, tmp_path):
    path = tmp_path / "private" / "receipt.json"
    value = {"label": "Recibo completo", "values": list(range(10))}
    original_dump, original_link = runner.json.dump, runner.os.link
    events = []
    def observed_dump(*args, **kwargs):
        assert not path.exists()
        events.append("serialize")
        return original_dump(*args, **kwargs)
    def observed_link(source, destination):
        assert not path.exists()
        assert json.loads(source.read_text()) == value
        events.append("publish")
        return original_link(source, destination)
    monkeypatch.setattr(runner.json, "dump", observed_dump)
    monkeypatch.setattr(runner.os, "link", observed_link)
    runner.private_write(path, value)
    assert json.loads(path.read_text()) == value
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert events == ["serialize", "publish"]
    assert not list(path.parent.glob(".writing-*"))


def test_private_write_failed_json_leaves_no_partial_receipt(monkeypatch, tmp_path):
    path = tmp_path / "receipt.json"
    with pytest.raises(ValueError):
        runner.private_write(path, {"valid_prefix": "written before failure", "invalid": float("nan")})
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_private_write_never_overwrites_existing_receipt(tmp_path):
    path = tmp_path / "receipt.json"
    runner.private_write(path, {"original": True})
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        runner.private_write(path, {"replacement": True})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".writing-*"))


def test_private_write_failure_before_publish_leaves_no_receipt(monkeypatch, tmp_path):
    path = tmp_path / "receipt.json"
    def fail_fsync(fd):
        raise OSError("synthetic flush failure")
    monkeypatch.setattr(runner.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        runner.private_write(path, {"complete": True})
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
