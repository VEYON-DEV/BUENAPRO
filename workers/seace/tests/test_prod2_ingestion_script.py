"""Pure CLI/preflight contracts; no source, database or paid-provider calls."""
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2.batch_plan import digest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("prod2_ingestion_cli_contract", ROOT / "scripts/run_prod2_ingestion.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(script, "datetime", FrozenDatetime)
    # Exercise historical recovery contracts without enabling the public CLI.
    monkeypatch.setattr(script, "RETIRED_EXECUTION", False)
    previous_umask = os.umask(0o077)
    os.umask(previous_umask)
    yield
    os.umask(previous_umask)


@pytest.mark.parametrize("years,keyword", [("all", "software"), ("2026,2025", "SOFTWARE")])
def test_dry_run_is_local_only_and_does_not_create_run_folder(monkeypatch, tmp_path, capsys, years, keyword):
    folder = tmp_path / "not-created"
    def forbidden(*args, **kwargs):
        pytest.fail("Dry-run must not access source, database, settings or provider")
    monkeypatch.setattr(script, "settings_context", forbidden)
    monkeypatch.setattr(script, "source_factory", forbidden)
    monkeypatch.setattr(script.psycopg, "connect", forbidden)
    monkeypatch.setattr(script, "JevDescriptionAnalyzer", forbidden)
    monkeypatch.setattr(script.sys, "argv", ["run_prod2_ingestion.py", "--folder", str(folder),
        "--dry-run", "--years", years, "--keyword", keyword, "--objects", "service"])
    assert script.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["published_from"] == "2026-04-06" and report["published_to"] == "2026-10-06"
    assert report["years"] == years
    assert report["native_years_require_http_discovery"] is (years == "all")
    assert report["keywords"] == 1 and report["objects"] == ["service"]
    assert report["pipeline"] == "metadata_jev_schedule_only"
    assert report["pdf_downloads"] is False and report["gemini"] is False
    assert not folder.exists()


@pytest.mark.parametrize("years", ["foo", "2026,2026", "2026,", "2026,2025,foo", "1999", "2101"])
def test_dry_run_rejects_malformed_or_duplicate_years_before_source_io(monkeypatch, tmp_path, years):
    monkeypatch.setattr(script, "source_factory", lambda *args: pytest.fail("Invalid years must fail before I/O"))
    monkeypatch.setattr(script.sys, "argv", ["run_prod2_ingestion.py", "--folder", str(tmp_path / "unused"),
        "--dry-run", "--years", years])
    with pytest.raises(ValueError):
        script.main()


def preflight_factory(results, calls):
    pending = iter(results)
    @contextmanager
    def context():
        value = next(pending)
        def search(keyword, **arguments):
            calls.append((keyword, arguments))
            return value
        yield SimpleNamespace(search=search)
    return context


def test_preflight_zero_requires_two_fresh_unfiltered_receipts_and_resumes_cache(tmp_path, capsys):
    calls = []
    empty = {"counter": [0, 0, 0, 1, 1], "rows": [], "search_verified": True}
    factory = preflight_factory([empty, empty], calls)
    args = (factory, [2025], ["service"], "2026-04-05", "2026-10-05", tmp_path)
    proofs = script.preflight(*args)
    assert len(calls) == 2 and len(proofs) == 1
    assert all(keyword == "" and fields["allow_empty_keyword"] is True for keyword, fields in calls)
    assert calls[0][1]["object_type"] == "Servicio" and calls[0][1]["year"] == 2025
    assert calls[0][1]["published_from"] == "05/04/2026"
    assert script.preflight(*args) == proofs  # An exhausted factory proves there was no new I/O.
    assert len(calls) == 2
    receipt = tmp_path / "preflight/2025-service.private.json"
    assert receipt.stat().st_mode & 0o777 == 0o600
    capsys.readouterr()


def test_preflight_changed_second_zero_does_not_skip_keywords(tmp_path, capsys):
    calls = []
    first = {"counter": [0, 0, 0, 1, 1], "rows": []}
    second = {"counter": [1, 1, 1, 1, 1], "rows": [{"synthetic": True}]}
    proofs = script.preflight(preflight_factory([first, second], calls), [2025], ["service"],
                              "2026-04-05", "2026-10-05", tmp_path)
    assert proofs == [] and len(calls) == 2
    receipt = json.loads((tmp_path / "preflight/2025-service.private.json").read_text())
    assert receipt["zero_verified"] is False
    capsys.readouterr()


def test_preflight_cache_cannot_be_reused_for_another_date_scope(tmp_path):
    path = tmp_path / "preflight/2025-service.private.json"
    script.private_write(path, {"query": {"published_from": "2026-01-01"}})
    @contextmanager
    def forbidden():
        pytest.fail("Mismatched preflight cache must fail before source I/O")
        yield
    with pytest.raises(RuntimeError, match="another frozen interval"):
        script.preflight(forbidden, [2025], ["service"], "2026-04-05", "2026-10-05", tmp_path)


def prepared_resume(monkeypatch, folder):
    config = json.loads((ROOT / "docs/sources/prod2-convenio-keywords.v4.json").read_text())
    inventory = json.loads((ROOT / "docs/sources" / config["keyword_inventory_file"]).read_text())
    requested = {"months": 6, "from_date": "2026-04-05", "to_date": "2026-10-05", "years": "2026,2025",
        "objects": ["service"], "keywords": ["software"], "config_hash": digest(config),
        "inventory_hash": digest(inventory), "skip_preflight": False}
    manifest = {"commercial_profiles": [{"descripcion": "Frozen software scope", "lineas": ["Software"]},
        {"descripcion": "Frozen industrial scope", "lineas": ["Automation"]}], "jev_model": "frozen-model"}
    script.private_write(folder / "manifest.private.json", {"requested": requested, "manifest": manifest})
    events = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, *args):
            events.append(sql)
            return SimpleNamespace(fetchone=lambda: {"locked": True}, fetchall=lambda: [])
    @contextmanager
    def settings(args):
        yield SimpleNamespace(database_url="synthetic-unused-database")
    monkeypatch.setattr(script, "settings_context", settings)
    monkeypatch.setattr(script.psycopg, "connect", lambda *args, **kwargs: Connection())
    # source_factory is constructed before reading a manifest; the factory itself must be lazy.
    monkeypatch.setattr(script, "source_factory", lambda *args: lambda: pytest.fail("No fresh source expected"))
    monkeypatch.setattr(script, "commercial_snapshot", lambda *args: pytest.fail("Resume must retain frozen commercial text"))
    monkeypatch.setattr(script.repo, "create_batch", lambda conn, run_id, actual: events.append(("manifest", actual)))
    monkeypatch.setattr(script.repo, "advance_phase", lambda *args: {"status": "complete"})
    def analyzer(*args, **kwargs):
        events.append(("jev_model", kwargs.get("model")))
        return SimpleNamespace()
    monkeypatch.setattr(script, "JevDescriptionAnalyzer", analyzer)
    monkeypatch.setattr(script, "dotenv_values", lambda *args: {"TYPESAFE_API_KEY": "synthetic-unused-key"})
    monkeypatch.setattr(script, "recover_paid_receipts", lambda *args: None)
    monkeypatch.setattr(script, "public_report", lambda *args: {"status": "complete"})
    return manifest, events


def resume_arguments(folder):
    return ["run_prod2_ingestion.py", "--folder", str(folder), "--years", "2026,2025", "--keyword", "software",
            "--objects", "service", "--from-date", "2026-04-05", "--to-date", "2026-10-05"]


def test_resume_preserves_frozen_commercial_descriptions_and_allows_operational_bounds(monkeypatch, tmp_path, capsys):
    manifest, events = prepared_resume(monkeypatch, tmp_path / "synthetic-resume")
    monkeypatch.setattr(script.sys, "argv", resume_arguments(tmp_path / "synthetic-resume")
                        + ["--request-interval", "3", "--max-work-items", "1"])
    assert script.main() == 0
    assert ("manifest", manifest) in events
    assert ("jev_model", manifest["jev_model"]) in events
    assert len(list((tmp_path / "synthetic-resume").glob("report-*.private.json"))) == 1
    capsys.readouterr()


@pytest.mark.parametrize("flag,value", [("--years", "2026"), ("--objects", "good,service"),
                                       ("--keyword", "cloud"), ("--from-date", "2026-04-06")])
def test_resume_refuses_changed_scope_before_creating_batch(monkeypatch, tmp_path, flag, value):
    _, events = prepared_resume(monkeypatch, tmp_path / "synthetic-resume")
    argv = resume_arguments(tmp_path / "synthetic-resume")
    argv[argv.index(flag) + 1] = value
    monkeypatch.setattr(script.sys, "argv", argv)
    with pytest.raises(ValueError):
        script.main()
    assert not any(isinstance(event, tuple) and event[0] == "manifest" for event in events)


@pytest.mark.parametrize("state,expected", [(None, "unpaid"), ("pending", "unpaid"), ("done", "paid"),
                                           ("running", None), ("uncertain", None), ("failed", None)])
def test_resume_reconciles_unpaid_or_completed_ledger_without_repaying(monkeypatch, tmp_path, state, expected):
    task = {"id": 7, "id_convocatoria": 2000, "status": "blocked", "error_code": "provider_reconciliation"}
    evaluation = None if state is None else {"id": 99, "status": state}
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): pass
        def fetchall(self): return [task]
        def fetchone(self): return evaluation
    connection = SimpleNamespace(cursor=lambda **kwargs: Cursor())
    events = []
    monkeypatch.setattr(script.repo, "discovery_locator", lambda *args: {"row_manifest": {"description": "SOFTWARE"}})
    monkeypatch.setattr(script.repo, "recover_enrichment_task",
                        lambda *args, **kwargs: events.append(("paid", args[1], kwargs)))
    monkeypatch.setattr(script.repo, "recover_unpaid_enrichment_task",
                        lambda *args, **kwargs: events.append(("unpaid", args[1], kwargs)))
    monkeypatch.setattr(script, "paid_jev_stage", lambda *args: pytest.fail("Missing receipts must never call the paid stage"))
    manifest = {"commercial_profiles": [{"descripcion": "Technology", "lineas": ["Software"]}],
                "jev_model": "synthetic-model", "jev_policy_version": "synthetic-policy", "profile_id": "synthetic-profile"}
    script.recover_paid_receipts(connection, "synthetic-run", manifest, tmp_path,
                                 SimpleNamespace(classify=lambda *args: pytest.fail("No provider I/O")))
    assert [event[0] for event in events] == ([expected] if expected is not None else [])
    assert not list(tmp_path.iterdir())


def test_public_report_includes_paid_ledger_without_completed_task_and_unknown_billing(monkeypatch):
    from datetime import timedelta
    config = {"profile_id": "synthetic-profile", "commercial_profiles": [{"descripcion": "Technology", "lineas": ["Software"]}],
        "jev_model": "synthetic-model", "jev_policy_version": "synthetic-policy", "published_from": "2026-04-05",
        "published_to": "2026-10-05", "procedure_years": [2026, 2025], "keywords": ["software"],
        "object_types": ["service"], "planned_query_count": 2, "preflight_skipped_query_count": 0,
        "coverage_scope": "configured_scope"}
    created = NOW - timedelta(days=1)
    digest_value = script.jev_request_hash("SOFTWARE", commercial_profiles=config["commercial_profiles"], model=config["jev_model"])
    def row(identity, status, cost, claimed, request=digest_value):
        return {"id": identity, "status": status, "request_hash": request, "input_tokens": 100,
                "cost_usd": cost, "claimed_at": claimed, "completed_at": NOW if status == "done" else None,
                "description": "SOFTWARE"}
    ledger = [row(1, "done", 0.01, created - timedelta(hours=1)),
              row(2, "done", 0.02, NOW),  # A failed metadata task cannot erase the paid response.
              row(3, "uncertain", None, NOW), row(4, "done", None, NOW),
              row(5, "done", 100.0, NOW, "f" * 64), row(6, "pending", None, None)]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, *args):
            self.ledger = "JOIN prod2_evaluations" in sql
        def fetchall(self):
            return ledger if self.ledger else []
    connection = SimpleNamespace(cursor=lambda **kwargs: Cursor())
    monkeypatch.setattr(script.repo, "report_batch", lambda *args: {"frozen_manifest": config,
        "created_at": created, "status": "blocked", "phase": "enrichment", "coverage_status": "complete",
        "blocked_reason": "provider_reconciliation", "tasks": [], "unique_candidates": 6})
    report = script.public_report(connection, "synthetic-run")
    usage = report["jev_usage_including_reused_evaluations"]
    assert usage["by_status"] == {"done": 3, "uncertain": 1, "pending": 1}
    assert usage["known_attributed_estimate_usd"] == pytest.approx(0.03)
    assert usage["known_started_during_run_estimate_usd"] == pytest.approx(0.02)
    assert usage["reservations_started_during_run"] == 3
    assert usage["unknown_billing_reservations"] == 2 and usage["invoice_total_usd"] is None
    assert report["pdf_downloads_this_pipeline"] == report["gemini_calls_this_pipeline"] == 0
    assert "Technology" not in json.dumps(report)  # Commercial text stays in the private frozen manifest.


def test_resume_during_initialization_keeps_original_window_and_commercial_snapshot(monkeypatch, tmp_path, capsys):
    from datetime import timedelta
    folder = tmp_path / "synthetic-initializing"
    frozen, events = prepared_resume(monkeypatch, folder)
    envelope = json.loads((folder / "manifest.private.json").read_text())
    requested = {**envelope["requested"], "from_date": None, "to_date": None, "years": "all"}
    (folder / "manifest.private.json").unlink()
    init = {"requested": requested, "as_of": (NOW - timedelta(days=1)).isoformat(),
            "start": "2026-04-05", "end": "2026-10-05", "years": [2026, 2025],
            "commercial_profiles": frozen["commercial_profiles"]}
    script.private_write(folder / "initialization.private.json", init)
    def preflight(factory, years, objects, start, end, cache_folder):
        assert years == [2026, 2025] and objects == ["service"]
        assert (start, end) == ("2026-04-05", "2026-10-05")
        return []
    monkeypatch.setattr(script, "preflight", preflight)
    monkeypatch.setattr(script.sys, "argv", ["run_prod2_ingestion.py", "--folder", str(folder),
                        "--years", "all", "--keyword", "software", "--objects", "service"])
    assert script.main() == 0
    actual = next(item[1] for item in events if isinstance(item, tuple) and item[0] == "manifest")
    assert actual["published_from"] == init["start"] and actual["published_to"] == init["end"]
    assert actual["commercial_profiles"] == frozen["commercial_profiles"]
    assert actual["procedure_years"] == [2026, 2025]
    capsys.readouterr()


def snapshot_task_fixture(failure_site, failure):
    from buenapro_worker.prod2 import batch_runner
    query = {"keyword": "software", "year": 2026, "object_type": "service", "published_from": "2026-10-01",
             "published_to": "2026-10-05", "seace_version": "3"}
    events = []
    def result(number):
        return {"counter": [number, number, 3, number, 3], "search_verified": True,
            "snapshot": {"total_rows": 3, "total_pages": 3, "page_size": 1},
            "rows": [{"procedure_id": 1000 + number, "convocatoria_id": 2000 + number, "entity": "SYNTHETIC BUYER",
                "published_at": "03/10/2026 12:30", "nomenclature": f"SYNTHETIC-2026-{number}", "object": "Servicio",
                "description": "software", "amount_text": "100", "currency": "Soles", "seace_version": "3"}]}
    def page(number):
        events.append(("page", number))
        if failure_site == "page":
            raise failure
        return result(number)
    @contextmanager
    def factory():
        yield SimpleNamespace(search=lambda *args, **kwargs: result(1), search_page=page)
    def persist(conn, identity, token, capture):
        number = capture["page"]["number"]
        events.append(("persist", number))
        if failure_site == "persist" and number == 2:
            raise failure
        return {"status": "partial"}
    repository = SimpleNamespace(renew_lease=lambda *args, **kwargs: None, persist_page=persist,
                                 restart_discovery_task=lambda *args: events.append(("restart",)))
    owned = {"task_id": 7, "owner_token": "synthetic-owner", "query": query, "checkpoint": {"next_page": 1}}
    return batch_runner, owned, repository, factory, events


def test_source_snapshot_change_restarts_after_previous_page_and_before_next_page():
    from buenapro_worker.prod2.client import Prod2ProtocolError
    runner, task, repo, factory, events = snapshot_task_fixture("page", Prod2ProtocolError("Search counter changed during pagination"))
    assert runner.discover_task(object(), task, repo, factory) == "restarted"
    assert events == [("persist", 1), ("page", 2), ("restart",)]


def test_persisted_duplicate_after_resume_is_a_snapshot_restart():
    from buenapro_worker.prod2.discovery import CaptureError
    runner, task, repo, factory, events = snapshot_task_fixture("persist", CaptureError("run: duplicate convocation across pages"))
    assert runner.discover_task(object(), task, repo, factory) == "restarted"
    assert events == [("persist", 1), ("page", 2), ("persist", 2), ("restart",)]


@pytest.mark.parametrize("message", ["PROD2 HTTP status 403", "PROD2 HTTP status 429", "PROD2 HTTP transport failed"])
def test_source_access_and_transport_errors_do_not_trigger_snapshot_retry(message):
    from buenapro_worker.prod2.client import Prod2ProtocolError
    runner, task, repo, factory, events = snapshot_task_fixture("page", Prod2ProtocolError(message))
    with pytest.raises(Prod2ProtocolError, match=message):
        runner.discover_task(object(), task, repo, factory)
    assert events == [("persist", 1), ("page", 2)]
