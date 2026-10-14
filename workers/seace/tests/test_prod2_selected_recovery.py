"""Accepted-only recovery conserves evidence and paid cache ownership."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_selected_recovery", ROOT / "scripts/recover_prod2_selected.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

CODE = "00000000-0000-4000-8000-000000000009"


@pytest.fixture(autouse=True)
def clear_stop():
    subject.STOP.clear()
    yield
    subject.STOP.clear()


def fixture(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    manifest = {"run_id": "synthetic-original", "pipeline": "prod2_selected_llm1_v1", "terminal_stage": "llm1",
        "gemini_model": "gemini-3.1-flash-lite", "profile_id": "00000000-0000-4000-8000-000000000001",
        "commercial_profiles": [{"descripcion": "Software", "lineas": ["Desarrollo"]},
                                {"descripcion": "Nube", "lineas": ["Cloud"]}],
        "published_from": "2026-04-06", "published_to": "2026-10-06", "keywords": ["software"], "years": [2026],
        "cache_folder": str(tmp_path / "cache"), "max_source_mib": 128, "max_expanded_mib": 256,
        "max_archive_members": 2000, "selector_high": 80, "selector_low": 20}
    subject.private_write(original / "manifest.private.json", manifest)
    subject.private_write(original / "report-1.private.json", {"run_id": "synthetic-original",
        "records": [{"convocation_id": 123, "accepted": True}]})
    args = subject.parser().parse_args(["--run-id", "synthetic-original", "--original-folder", str(original),
        "--convocation-id", "123", "--folder", str(tmp_path / "new-recovery")])
    frozen = subject.plan(args)
    row = {"convocation_id": 123, "procedure_id": 456, "nomenclature": "TEST-2026", "buyer_name": "SYNTHETIC BUYER",
           "object_label": "Servicio", "description": "SOFTWARE"}
    query = {"keyword": "software", "year": 2026, "object_type": "service", "published_from": "2026-04-06",
             "published_to": "2026-10-06", "seace_version": "3"}
    proof = {"policy": "prod2_accepted_detail_recovery_v1", "run_id": "synthetic-original", "convocation_id": 123,
             "opportunity_id": manifest["profile_id"], "affinity_index": 73, "row": row, "query": query,
             "request_hash": subject.jev_request_hash(row["description"], commercial_profiles=manifest["commercial_profiles"])}
    doc = {"uuid": CODE, "filename": "Bases.zip", "type": "Bases Administrativas", "stage": "Convocatoria",
           "published_at": "2026-10-06T15:00:00+00:00"}
    detail = {"convocatoria_id": 123, "procedure_id": 456, "nomenclature": "TEST-2026", "entity": "SYNTHETIC BUYER",
              "object": "Servicio", "description": "SOFTWARE", "seace_version": "3", "documents": [doc],
              "schedule": [], "document_inventory_complete": True, "document_inventory_total": 1, "item_states": []}
    capture = {"observed_at": "2026-10-06T17:00:00+00:00", "detail": detail}
    (original / "123").mkdir()
    subject.private_write(original / "123/detail.private.json", capture)
    original_outcome = original / "123/outcome.private.json"
    subject.private_write(original_outcome, {"status": "selection_needs_review", "accepted": True})
    return args, frozen, proof, capture, original_outcome


def cache_original(manifest):
    folder = Path(manifest["original_folder"]) / "123"
    data = b"PK\x03\x04synthetic-not-opened-archive"
    (folder / (CODE + ".zip")).write_bytes(data)
    receipt = {"document_uuid": CODE, "source_url": subject.METADATA_URL + "?id=" + CODE,
        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "content_type": "application/zip", "format": "zip"}
    subject.private_write(folder / (CODE + ".download.private.json"), receipt)
    return data, receipt


def test_new_plan_preserves_frozen_business_context_and_switches_preview_policy(tmp_path):
    args, manifest, *_ = fixture(tmp_path)
    assert manifest["gemini_model"] == "gemini-3.1-flash-lite"
    assert manifest["selector_policy_version"] == subject.document_selection.DOCUMENT_SELECTION_POLICY_V3
    assert manifest["document_previews"] and manifest["no_discovery"] and manifest["no_redis"]
    assert manifest["max_jev_calls"] == 20 and manifest["max_llm1_calls"] == 10
    assert manifest["request_interval"] == 2


def test_auto_year_plan_uses_frozen_resolved_native_years(tmp_path):
    args, _, proof, _, _ = fixture(tmp_path)
    path = args.original_folder / "manifest.private.json"
    old = subject.read_private(path)
    old.update(years="auto", resolved_years=[2026, 2025, 2024])
    path.write_text(json.dumps(old))
    manifest = subject.plan(args)
    assert manifest["years"] == "auto" and manifest["resolved_years"] == [2026, 2025, 2024]
    subject.prepare_member(proof, manifest, Path(manifest["recovery_folder"]) / "123")


def test_recovery_inherits_page_policy_only_from_original_manifest(tmp_path):
    args, legacy, *_ = fixture(tmp_path)
    assert "llm1_model_routing" not in legacy
    path = args.original_folder / "manifest.private.json"
    old = subject.read_private(path)
    old["llm1_model_routing"] = subject.selected.default_model_routing()
    path.write_text(json.dumps(old))
    manifest = subject.plan(args)
    assert manifest["llm1_model_routing"] == old["llm1_model_routing"]
    model, proof = subject.selected.resolve_llm1_model(manifest, 21, manifest["gemini_model"])
    assert model == "gemini-2.5-flash-lite" and proof["page_count"] == 21
    assert subject.read_private(path) == old


@pytest.mark.parametrize("policy", [None, {"policy_version": "untrusted"}])
def test_recovery_invalid_policy_cannot_start(tmp_path, policy):
    args, *_ = fixture(tmp_path)
    path = args.original_folder / "manifest.private.json"
    old = subject.read_private(path)
    old["llm1_model_routing"] = policy
    path.write_text(json.dumps(old))
    with pytest.raises(ValueError, match="policy fields are invalid"):
        subject.plan(args)


@pytest.mark.parametrize("years", ["auto", [], [2026, 2026], [True], ["2026"]])
def test_missing_or_invalid_resolved_years_cannot_start_recovery(tmp_path, years):
    args, *_ = fixture(tmp_path)
    path = args.original_folder / "manifest.private.json"
    old = subject.read_private(path)
    old.update(years="auto", resolved_years=years)
    path.write_text(json.dumps(old))
    with pytest.raises(ValueError, match="resolved native"):
        subject.plan(args)


@pytest.mark.parametrize("field,value", [("processing_workers", 3), ("model_workers", 3),
    ("max_selector_calls", 21), ("max_llm1_calls", 11), ("max_source_requests", 501),
    ("request_interval", 1), ("convocation_id", [123, 123]), ("convocation_id", [0])])
def test_invalid_bounds_stop_before_runtime(tmp_path, field, value):
    args, *_ = fixture(tmp_path)
    setattr(args, field, value)
    with pytest.raises(ValueError):
        subject.plan(args)


@pytest.mark.parametrize("placement", ["same", "child", "ancestor"])
def test_recovery_cannot_modify_any_original_tree(tmp_path, placement):
    args, *_ = fixture(tmp_path)
    args.folder = {"same": args.original_folder, "child": args.original_folder / "nested", "ancestor": tmp_path}[placement]
    with pytest.raises(ValueError, match="separate private recovery"):
        subject.plan(args)


def test_nonempty_unowned_folder_is_rejected(tmp_path):
    args, *_ = fixture(tmp_path)
    args.folder.mkdir()
    (args.folder / "unrelated.txt").write_text("preserve")
    with pytest.raises(ValueError, match="nonempty"):
        subject.plan(args)


def test_symlinked_source_or_destination_is_rejected(tmp_path):
    args, *_ = fixture(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(args.original_folder, target_is_directory=True)
    args.original_folder = linked
    with pytest.raises(ValueError, match="symlink"):
        subject.plan(args)


@pytest.mark.parametrize("field", ["convocatoria_id", "procedure_id", "nomenclature", "entity", "object", "description"])
def test_ficha_capture_requires_exact_accepted_identity(tmp_path, field):
    _, _, proof, capture, _ = fixture(tmp_path)
    capture["detail"][field] = 999 if isinstance(capture["detail"][field], int) else "changed"
    with pytest.raises(ValueError, match="identity"):
        subject.validate_capture(capture, proof)


def test_ficha_recovery_requires_exact_proof_hash(tmp_path):
    _, _, proof, capture, _ = fixture(tmp_path)
    capture["proof_hash"] = "f" * 64
    with pytest.raises(ValueError, match="proof"):
        subject.validate_capture(capture, proof)
    capture["proof_hash"] = subject.hash_request(proof)
    assert subject.validate_capture(capture, proof) == capture


@pytest.mark.parametrize("override", [{"document_inventory_complete": False}, {"seace_version": "4"},
                                      {"documents": None}, {"schedule": None}])
def test_incomplete_capture_stays_pending(tmp_path, override):
    _, _, proof, capture, _ = fixture(tmp_path)
    capture["detail"].update(override)
    with pytest.raises(ValueError):
        subject.validate_capture(capture, proof)


def test_copies_only_verified_original_and_never_overwrites_original_outcome(tmp_path):
    _, manifest, proof, capture, original_outcome = fixture(tmp_path)
    data, receipt = cache_original(manifest)
    old = original_outcome.read_bytes()
    source = Path(manifest["original_folder"]) / "123"
    (source / "untrusted.zip").write_bytes(b"do not copy")
    private = Path(manifest["recovery_folder"]) / "123"
    item = subject.prepare_member(proof, manifest, private)
    assert item["cached_original_ids"] == [CODE]
    assert (private / (CODE + ".zip")).read_bytes() == data
    assert subject.read_private(private / (CODE + ".download.private.json")) == receipt
    assert subject.read_private(private / "detail.private.json") == capture
    assert not (private / "untrusted.zip").exists()
    assert original_outcome.read_bytes() == old
    assert subject.prepare_member(proof, manifest, private) == item


def test_prefers_bound_recovered_ficha_and_preserves_events(tmp_path):
    args, _, proof, capture, _ = fixture(tmp_path)
    recovered = tmp_path / "detail-recovery"
    (recovered / "123").mkdir(parents=True)
    capture["proof_hash"] = subject.hash_request(proof)
    capture["detail"]["document_events"] = [capture["detail"]["documents"][0]] * 2
    subject.private_write(recovered / "123/detail.private.json", capture)
    args.detail_recovery_folder = [recovered]
    manifest = subject.plan(args)
    private = Path(manifest["recovery_folder"]) / "123"
    subject.prepare_member(proof, manifest, private)
    assert subject.read_private(private / "detail.private.json") == capture


@pytest.mark.parametrize("mutation", ["bytes", "sha", "uuid", "format", "url", "symlink"])
def test_bad_cached_original_stops_without_source_or_provider(tmp_path, mutation):
    _, manifest, proof, _, _ = fixture(tmp_path)
    _, receipt = cache_original(manifest)
    root = Path(manifest["original_folder"]) / "123"
    path = root / (CODE + ".zip")
    if mutation == "sha":
        path.write_bytes(b"changed-data-with-same-len-123456")
    elif mutation == "symlink":
        original = tmp_path / "outside.zip"
        original.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(original)
    else:
        key, value = {"bytes": ("bytes", 999), "uuid": ("document_uuid", "wrong"),
                      "format": ("format", "pdf"), "url": ("source_url", "https://evil.invalid/")}[mutation]
        receipt[key] = value
        (root / (CODE + ".download.private.json")).write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        subject.prepare_member(proof, manifest, Path(manifest["recovery_folder"]) / "123")


@pytest.mark.parametrize("mutation", ["query", "affinity", "run"])
def test_original_commercial_query_binding_is_required(tmp_path, mutation):
    _, manifest, proof, _, _ = fixture(tmp_path)
    if mutation == "query":
        proof["query"]["published_from"] = "2025-01-01"
    elif mutation == "affinity":
        proof["request_hash"] = "f" * 64
    else:
        proof["run_id"] = "another-run"
    with pytest.raises(ValueError, match="commercial search manifest"):
        subject.prepare_member(proof, manifest, Path(manifest["recovery_folder"]) / "123")


def test_download_budget_stops_all_future_source_calls():
    pacer = subject.RecoveryPacer(1, 2)
    pacer.pacer = MagicMock()
    pacer(None)
    with pytest.raises(subject.LimitReached):
        pacer(None)
    assert pacer.requests == 1 and subject.STOP.is_set()
    assert pacer.pacer.call_count == 1


def test_dry_run_has_no_connections_or_creates_no_recovery_folder(tmp_path, monkeypatch, capsys):
    args, *_ = fixture(tmp_path)
    monkeypatch.setattr(subject.sys, "argv", ["recover", "--run-id", args.run_id,
        "--original-folder", str(args.original_folder), "--convocation-id", "123",
        "--folder", str(args.folder), "--dry-run"])
    runtime = MagicMock(side_effect=AssertionError("No external runtime in dry run"))
    monkeypatch.setattr(subject, "production_settings", runtime)
    monkeypatch.setattr(subject.psycopg, "connect", runtime)
    assert subject.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source_calls"] == result["provider_calls"] == result["redis_calls"] == 0
    assert not args.folder.exists()
    runtime.assert_not_called()


def test_success_outcome_replay_performs_no_enrichment_or_connection(tmp_path, monkeypatch):
    _, manifest, proof, _, original = fixture(tmp_path)
    private = Path(manifest["recovery_folder"]) / "123"
    private.mkdir(parents=True)
    success = {"status": "llm1_complete", "convocation_id": 123,
               "accepted_proof_hash": subject.hash_request(proof), "recovery_manifest_hash": subject.hash_request(manifest)}
    subject.private_write(private / "outcome.private.json", success)
    connect, enrich = MagicMock(), MagicMock()
    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject.selected, "enrich", enrich)
    assert subject.recover_member(None, proof, manifest, private, None, None, "synthetic") == success
    connect.assert_not_called()
    enrich.assert_not_called()
    assert subject.read_private(original)["status"] == "selection_needs_review"


def test_incomplete_outcome_retry_preserves_history_and_reuses_same_executor(tmp_path, monkeypatch):
    _, manifest, proof, _, original = fixture(tmp_path)
    private = Path(manifest["recovery_folder"]) / "123"
    private.mkdir(parents=True)
    previous = {"status": "needs_review", "convocation_id": 123,
                "accepted_proof_hash": subject.hash_request(proof), "recovery_manifest_hash": subject.hash_request(manifest)}
    subject.private_write(private / "outcome.private.json", previous)
    conn = MagicMock()
    monkeypatch.setattr(subject.psycopg, "connect", MagicMock(return_value=conn))
    # Durable payment wrappers inside enrich own reconciliation; executor never
    # clears uncertain claims or retries provider callbacks itself.
    enrich = MagicMock(return_value={"status": "llm1_complete", "extraction_id": 42, "projection_pending": True})
    monkeypatch.setattr(subject.selected, "enrich", enrich)
    result = subject.recover_member(SimpleNamespace(database_url="synthetic"), proof, manifest, private, None, None, "synthetic")
    assert result["status"] == "llm1_complete" and result["projection_pending"]
    assert len(list(private.glob("outcome-attempt-*.private.json"))) == 1
    assert subject.read_private(next(private.glob("outcome-attempt-*.private.json"))) == previous
    assert subject.read_private(original)["status"] == "selection_needs_review"
    enrich.assert_called_once()


def test_uncertain_paid_stage_is_recorded_without_automatic_retry(tmp_path, monkeypatch):
    _, manifest, proof, _, _ = fixture(tmp_path)
    conn = MagicMock()
    monkeypatch.setattr(subject.psycopg, "connect", MagicMock(return_value=conn))
    enrich = MagicMock(side_effect=RuntimeError("Selector needs reconciliation; no automatic repayment"))
    monkeypatch.setattr(subject.selected, "enrich", enrich)
    private = Path(manifest["recovery_folder"]) / "123"
    result = subject.recover_member(SimpleNamespace(database_url="synthetic"), proof, manifest, private, None, None, "synthetic")
    assert result["status"] == "needs_review" and result["error_type"] == "RuntimeError"
    enrich.assert_called_once()


def prior_folder(tmp_path, manifest, *, ids=None, name="prior"):
    root = tmp_path / name
    root.mkdir()
    value = {**manifest, "recovery_folder": str(root),
        "selector_policy_version": subject.document_selection.DOCUMENT_SELECTION_POLICY_V2,
        "recovery_ids": ids or manifest["recovery_ids"]}
    subject.private_write(root / "manifest.private.json", value)
    return root, value


def test_auto_replay_respects_original_v2_frozen_manifest(tmp_path):
    args, manifest, *_ = fixture(tmp_path)
    old = {**manifest, "selector_policy_version": subject.document_selection.DOCUMENT_SELECTION_POLICY_V2}
    args.folder.mkdir()
    subject.private_write(args.folder / "manifest.private.json", old)
    assert subject.plan(args) == old
    args.selector_policy = "v3"
    with pytest.raises(ValueError, match="Pinned recovery inputs changed"):
        subject.plan(args)


def test_selector_only_one_id_and_stage_are_explicit(tmp_path):
    args, *_ = fixture(tmp_path)
    args.selector_only = True
    manifest = subject.plan(args)
    assert manifest["selection_only"] and manifest["terminal_stage"] == "document_selection"
    args.convocation_id = [123, 124]
    with pytest.raises(ValueError, match="exactly one"):
        subject.plan(args)


def test_bound_prior_subset_can_include_other_original_accepted_ids_without_processing_them(tmp_path):
    args, manifest, *_ = fixture(tmp_path)
    path = args.original_folder / "report-1.private.json"
    original = subject.read_private(path)
    original["records"].append({"convocation_id": 124, "accepted": True})
    path.write_text(json.dumps(original))
    prior, _ = prior_folder(tmp_path, manifest, ids=[123, 124])
    args.prior_recovery_folder = [prior]
    args.selector_only = True
    plan = subject.plan(args)
    assert plan["recovery_ids"] == [123] and plan["prior_authorized_ids"] == [123, 124]


@pytest.mark.parametrize("field,value", [("original_manifest_hash", "f" * 64), ("run_id", "different"),
    ("profile_id", "different"), ("gemini_model", "different"), ("commercial_profiles", []),
    ("published_from", "2025-01-01"), ("recovery_ids", [999])])
def test_prior_context_or_unauthorized_ids_cannot_reset_budget(tmp_path, field, value):
    args, manifest, *_ = fixture(tmp_path)
    root, prior = prior_folder(tmp_path, manifest)
    prior[field] = value
    (root / "manifest.private.json").write_text(json.dumps(prior))
    args.prior_recovery_folder = [root]
    with pytest.raises(ValueError, match="same authorized"):
        subject.plan(args)


def test_folder_caps_and_nested_prior_roots_are_rejected(tmp_path):
    args, manifest, *_ = fixture(tmp_path)
    args.download_cache_folder = [args.original_folder] * 9
    with pytest.raises(ValueError, match="eight"):
        subject.plan(args)
    args.download_cache_folder = []
    root, _ = prior_folder(tmp_path, manifest)
    child = root / "nested"
    child.mkdir()
    args.prior_recovery_folder = [root]
    args.download_cache_folder = [child]
    with pytest.raises(ValueError, match="must not nest"):
        subject.plan(args)


def test_download_cache_shares_verified_v2_originals_without_source(tmp_path):
    args, manifest, proof, _, _ = fixture(tmp_path)
    data, receipt = cache_original(manifest)
    prior, _ = prior_folder(tmp_path, manifest)
    (prior / "123").mkdir()
    root = args.original_folder / "123"
    for suffix in (".zip", ".download.private.json"):
        (root / (CODE + suffix)).rename(prior / "123" / (CODE + suffix))
    args.download_cache_folder = [prior]
    args.prior_recovery_folder = [prior]
    plan = subject.plan(args)
    private = args.folder / "123"
    assert subject.prepare_member(proof, plan, private)["cached_original_ids"] == [CODE]
    assert (private / (CODE + ".zip")).read_bytes() == data
    assert subject.read_private(private / (CODE + ".download.private.json")) == receipt


def test_conflicting_verified_caches_are_not_silently_chosen(tmp_path):
    args, manifest, proof, _, _ = fixture(tmp_path)
    _, receipt = cache_original(manifest)
    prior, _ = prior_folder(tmp_path, manifest)
    (prior / "123").mkdir()
    data = b"PK\x03\x04different-official-revision"
    (prior / "123" / (CODE + ".zip")).write_bytes(data)
    receipt.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    subject.private_write(prior / "123" / (CODE + ".download.private.json"), receipt)
    args.download_cache_folder = [prior]
    with pytest.raises(ValueError, match="Pinned cached original changed"):
        subject.prepare_member(proof, subject.plan(args), args.folder / "123")


def write_jev(root, identity="jev-attempt-test", *, cost=.001):
    receipt = {"attempt_id": identity, "provider": "jev", "usage_known": True,
               "cost_usd": cost, "status": "response_received", "elapsed_seconds": 1}
    subject.private_write(root / (identity + ".outcome.private.json"), receipt)
    subject.private_write(root / (identity + ".started.private.json"),
        {**receipt, "status": "error_unknown", "usage_known": False})
    return receipt


def test_combined_audit_counts_raw_attempts_once_across_reports_folders_and_parts(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    same = write_jev(a)
    subject.private_write(b / "jev-attempt-test.outcome.private.json", same)
    write_jev(b, "jev-attempt-new", cost=.002)
    subject.private_write(b / "report-1.private.json", {"costs": {"jev_calls": 100, "known_cost_usd": 100}})
    result = subject.combined_audit(a, [b, b])
    assert result["attempts"] == result["jev_calls"] == 2
    assert result["known_cost_usd"] == pytest.approx(.003)
    assert result["usage_unknown_attempts"] == 0


def test_conflicting_attempt_receipts_stop_before_any_budget_repayment(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    write_jev(a)
    write_jev(b, cost=.999)
    with pytest.raises(ValueError, match="conflicting receipts"):
        subject.combined_audit(a, [b])


def test_combined_audit_gemini_usage_prefers_response_and_ignores_upload_only_failures(tmp_path):
    identity = "gemini-selected-test"
    common = {"attempt_id": identity, "model_call_started": True, "elapsed_seconds": 1}
    subject.private_write(tmp_path / (identity + ".started.private.json"), common)
    subject.private_write(tmp_path / (identity + ".response.private.json"),
        {**common, "provider_usage_known": True, "cost_usd": .03})
    subject.private_write(tmp_path / (identity + ".outcome.private.json"),
        {**common, "provider_usage_known": False, "cost_usd": None})
    subject.private_write(tmp_path / "gemini-selected-upload.outcome.private.json",
        {"attempt_id": "gemini-selected-upload", "model_call_started": False})
    result = subject.combined_audit(tmp_path)
    assert result["llm1_calls"] == 1 and result["usage_unknown_attempts"] == 0
    assert result["known_cost_usd"] == .03


def test_combined_audit_both_model_providers_union_paid_parts_and_prior_copies_once(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for provider, cost in (("gemini", .02), ("openai", .1)):
        identity = provider + "-selected-one"
        common = {"attempt_id": identity, "model_call_started": True, "elapsed_seconds": 1}
        parts = {"started": common,
            "response": {**common, "provider_usage_known": True, "cost_usd": cost},
            "outcome": {**common, "provider_usage_known": False, "cost_usd": None}}
        for part, receipt in parts.items():
            for root in (a, b):
                subject.private_write(root / (identity + "." + part + ".private.json"), receipt)
    subject.private_write(b / "openai-selected-upload.outcome.private.json",
        {"attempt_id": "openai-selected-upload", "model_call_started": False, "cost_usd": 0})
    write_jev(b)
    subject.private_write(b / "report-1.private.json", {"costs": {"known_cost_usd": 999}})
    result = subject.combined_audit(a, [b, b])
    assert result["attempts"] == 3 and result["jev_calls"] == 1
    assert result["llm1_calls"] == 2 and result["openai_calls"] == 1
    assert result["known_cost_usd"] == pytest.approx(.121)
    assert result["usage_unknown_attempts"] == 0 and result["elapsed_seconds"] == 3


def test_combined_audit_openai_uncertain_start_and_conflicting_prior_stop_repayment(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    receipt = {"attempt_id": "openai-selected-uncertain", "status": "generation_reserved"}
    filename = "openai-selected-uncertain.started.private.json"
    subject.private_write(a / filename, receipt)
    result = subject.combined_audit(a)
    assert result["attempts"] == result["llm1_calls"] == result["openai_calls"] == 1
    assert result["usage_unknown_attempts"] == 1 and result["known_cost_usd"] == 0
    subject.private_write(b / filename, {**receipt, "status": "different"})
    with pytest.raises(ValueError, match="conflicting receipts"):
        subject.combined_audit(a, [b])


def test_selector_only_success_replay_never_calls_llm1(tmp_path, monkeypatch):
    args, _, proof, _, _ = fixture(tmp_path)
    args.selector_only = True
    manifest = subject.plan(args)
    private = args.folder / "123"
    private.mkdir(parents=True)
    success = {"status": "selection_complete", "accepted_proof_hash": subject.hash_request(proof),
               "recovery_manifest_hash": subject.hash_request(manifest)}
    subject.private_write(private / "outcome.private.json", success)
    external = MagicMock(side_effect=AssertionError("Success replay must stay offline"))
    monkeypatch.setattr(subject.psycopg, "connect", external)
    monkeypatch.setattr(subject.selected, "enrich", external)
    assert subject.recover_member(None, proof, manifest, private, None, None, None) == success
    external.assert_not_called()


@pytest.mark.parametrize("error", [subject.document_selection.DocumentSelectionError("Invalid document-action probability distribution"),
    subject.DocumentPreviewError("Document preview cache hash mismatch"), subject.PdfPreparationError("Selected source hash changed"),
    subject.ArchiveInventoryError("Unsafe member path"), subject.Llm1CompletionError("Current official inventory changed")])
def test_only_structured_document_errors_have_safe_reasons(error):
    assert subject.safe_error_reason(error) == str(error)
    assert subject.safe_error_reason(RuntimeError("postgresql://user:password@server SECRET")) is None


def test_structured_reason_redacts_transport_credentials():
    error = subject.DocumentPreviewError("Unsafe https://evil.invalid/?token=secret password=private apikey_abc_123")
    reason = subject.safe_error_reason(error)
    assert "secret" not in reason and "private" not in reason and "apikey_" not in reason


def test_copies_only_verified_current_preview_samples_and_rejects_tamper(tmp_path):
    root, private = tmp_path / "cache", tmp_path / "destination"
    private.mkdir()
    identity = {"source_sha256": "a" * 64, "preview_policy": subject.document_previews.PREVIEW_POLICY,
                "extension": ".pdf", "max_characters": 3500, "max_ocr_pages": 3}
    value = {**identity, "sample_only": True, "preview_text": "Bases de contratación",
             "page_count": 2, "preview_pages": [1], "method": "native_text"}
    value["content_hash"] = subject.hash_request(value)
    path = root / "text-previews" / ("b" * 64) / "document-preview-cache" / (subject.hash_request(identity) + ".private.json")
    subject.private_write(path, value)
    assert subject.cached_previews([root], private) == 1
    assert subject.read_private(private / path.relative_to(root)) == value
    value["preview_text"] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(subject.DocumentPreviewError, match="hash mismatch"):
        subject.cached_previews([root], private)


def test_sevenzip_recovery_opt_in_is_separate_and_cached_original_exact(tmp_path):
    args, legacy, proof, capture, original_outcome = fixture(tmp_path)
    original_manifest = args.original_folder / 'manifest.private.json'
    immutable = {p: p.read_bytes() for p in (original_manifest, original_outcome)}
    capture['detail']['documents'][0]['filename'] = 'BASES INTEGRADAS.7z'
    source = args.original_folder / '123'
    (source / 'detail.private.json').write_text(json.dumps(capture))
    data = b'7z\xbc\xaf\x27\x1csynthetic-cache-not-unpacked'
    (source / (CODE + '.7z')).write_bytes(data)
    facts = {'document_uuid': CODE, 'source_url': subject.METADATA_URL + '?id=' + CODE,
             'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
             'content_type': 'application/x-7z-compressed', 'format': '7z'}
    subject.private_write(source / (CODE + '.download.private.json'), facts)
    before = subject.prepare_member(proof, legacy, Path(legacy['recovery_folder']) / '123')
    assert before['cached_original_ids'] == []
    args.folder = tmp_path / 'modern-recovery'
    args.archive_7z = True
    modern = subject.plan(args)
    assert modern['archive_inventory_policy'] == subject.POLICY_7Z
    private = Path(modern['recovery_folder']) / '123'
    after = subject.prepare_member(proof, modern, private)
    assert after['cached_original_ids'] == [CODE]
    assert (private / (CODE + '.7z')).read_bytes() == data
    assert subject.read_private(private / (CODE + '.download.private.json')) == facts
    assert all(p.read_bytes() == value for p, value in immutable.items())
    subject.private_write(args.folder / 'manifest.private.json', modern)
    args.archive_7z = False
    with pytest.raises(ValueError, match='Pinned recovery inputs changed'):
        subject.plan(args)
