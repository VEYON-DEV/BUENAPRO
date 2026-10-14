"""Offline audit checks; no production credentials, source or provider requests."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_pending_verification", ROOT / "scripts/verify_prod2_pending.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
import continue_prod2_pending as runner


def replace(path, value):
    path.unlink(missing_ok=True)
    subject.private_write(path, value)


@pytest.fixture
def audit(tmp_path, monkeypatch):
    original, folder, preflight, baseline = (tmp_path / name for name in ("original", "continuation", "preflight.json", "baseline.json"))
    old = {"run_id": "original", "pipeline": "prod2_selected_llm1_v1", "document_scope": "prod2_selected_documents_v1",
        "terminal_stage": "llm1", "gemini_model": "gemini-3.1-flash-lite", "jev_threshold": 60,
        "profile_id": "bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "commercial_profiles": [{"descripcion": "software", "lineas": []}],
        "published_from": "2026-04-06", "published_to": "2026-10-06", "resolved_years": [2026],
        "keywords": ["software"], "objects": ["service"], "cache_folder": str(tmp_path / "cache"),
        "max_source_mib": 128, "max_expanded_mib": 256, "max_archive_members": 2000,
        "max_candidates": 100, "max_accepted": 10, "selector_high": 80, "selector_low": 20,
        "keyword_tracking": {"sweep_id": "original", "policy": "prod2_keyword_tracking_v1"}, "requested": {"legacy": True}}
    subject.private_write(original / "manifest.private.json", old)
    for conv in range(1, 11):
        subject.private_write(original / str(conv) / "outcome.private.json", {"convocation_id": conv, "accepted": True, "status": "llm1_complete"})
    options = runner.parser().parse_args(["--original-folder", str(original), "--folder", str(folder)])
    base, _ = runner.plan(options)
    ids = list(range(100, 190))
    items = {}
    for conv in ids:
        items[str(conv)] = {"row": {"procedure_id": 1000 + conv, "convocation_id": conv, "nomenclature": "SYNTHETIC",
            "buyer_name": "SYNTHETIC BUYER", "published_at": "2026-05-01T12:00:00-05:00", "description": "SOPORTE DE SOFTWARE",
            "object_type": "service", "object_label": "Servicio", "reference_amount": "100.00", "currency": "Soles", "seace_version": "3"},
            "query": {"keyword": "software", "object_type": "service", "year": 2026, "published_from": "2026-04-06",
                "published_to": "2026-10-06", "seace_version": "3"}, "observed_at": "2026-10-06T22:00:00+00:00", "page_number": 1}
    queue = {"states": {**{str(i): "done" for i in range(1, 11)}, **{str(i): "queued" for i in ids}},
        "payloads": items, "queue": list(items), "pending": []}
    manifest = runner.bind_snapshot(base, old, queue)
    subject.private_write(folder / "manifest.private.json", manifest)
    subject.private_write(preflight, {"pending_ids": ids, "done_ids": list(range(1, 11)), "original_manifest_hash": subject.hash_request(old)})
    subject.private_write(baseline, {"pending_ids": ids, "prior_ids": list(range(1, 11)), "original_manifest_hash": subject.hash_request(old)})
    for conv in ids:
        subject.private_write(folder / str(conv) / "outcome.private.json", {"convocation_id": conv,
            "accepted": False, "status": "rejected_below_60", "affinity_index": 20})
    stage = SimpleNamespace(stats=lambda: {"seen": 100, "payloads": 0, "queued": 0, "pending": 0})
    value = runner.report(folder, manifest, stage, SimpleNamespace(requests=0), began=runner.time.monotonic())
    subject.private_write(folder / "report-1.private.json", value)
    args = SimpleNamespace(original_folder=original, folder=folder, preflight=preflight, baseline_file=baseline,
        prior_folder=tmp_path / "prior", output=tmp_path / "audit.json")
    def forbidden(*a, **k):
        pytest.fail("Local audit must not call an external service")
    monkeypatch.setattr(subject.selected, "runtime", forbidden)
    monkeypatch.setattr(subject.psycopg, "connect", forbidden)
    return SimpleNamespace(args=args, manifest=manifest, old=old, report=value, ids=ids, stage=stage)


def refresh_report(audit):
    value = runner.report(audit.args.folder, audit.manifest, audit.stage, SimpleNamespace(requests=0), began=runner.time.monotonic())
    replace(audit.args.folder / "report-1.private.json", value)


def test_exact_90_audit_uses_only_private_captures(audit):
    result = subject.local_run(audit.args)
    assert len(result["rejected"]) == 90 and not result["complete"] and not result["blocked"]
    assert result["costs"]["attempts"] == 0


@pytest.mark.parametrize("mutate,code", [
    (lambda v: v["pending_ids"].append(100), "pending_duplicate_ids"),
    (lambda v: v["pending_ids"].pop(), "exact_90_10_scope"),
    (lambda v: v["done_ids"].__setitem__(0, 100), "exact_90_10_scope"),
    (lambda v: v.__setitem__("original_manifest_hash", "0" * 64), "original_manifest_changed"),
])
def test_preflight_exact_scope_cannot_expand_or_mutate(audit, mutate, code):
    value = subject.read_private(audit.args.preflight)
    mutate(value)
    replace(audit.args.preflight, value)
    with pytest.raises(subject.PendingVerificationError, match=code):
        subject.local_run(audit.args)


def test_report_cannot_claim_a_different_paid_outcome(audit):
    value = deepcopy(audit.report)
    value["records"][0]["affinity_index"] = 30
    replace(audit.args.folder / "report-1.private.json", value)
    with pytest.raises(subject.PendingVerificationError, match="report_outcome_binding"):
        subject.local_run(audit.args)


def test_report_cannot_claim_finished_while_redis_payloads_remain(audit):
    value = deepcopy(audit.report)
    value["queue_after"]["payloads"] = 1
    replace(audit.args.folder / "report-1.private.json", value)
    with pytest.raises(subject.PendingVerificationError, match="report_queue_not_drained"):
        subject.local_run(audit.args)


def unknown_attempt(audit):
    folder = audit.args.folder / "100"
    outcome = {"convocation_id": 100, "accepted": True, "status": "needs_review", "affinity_index": 80,
        "error_type": "ReadTimeout"}
    replace(folder / "outcome.private.json", outcome)
    receipt = {"attempt_id": "synthetic-timeout", "model": "gemini-3.1-flash-lite", "status": "error",
        "model_call_started": True, "provider_usage_known": False, "input_tokens": None,
        "output_tokens": None, "cost_usd": None, "remote_cleanup_pending": 0,
        "remote_cleanup_unknown": False, "error_type": "ReadTimeout"}
    subject.private_write(folder / "gemini-selected-synthetic-timeout.started.private.json", receipt)
    subject.private_write(folder / "gemini-selected-synthetic-timeout.outcome.private.json", receipt)
    refresh_report(audit)


def test_unknown_billing_remains_blocked_by_default(audit):
    unknown_attempt(audit)
    with pytest.raises(subject.PendingVerificationError, match="provider_usage_unknown"):
        subject.local_run(audit.args)


def test_optin_audits_finished_subset_without_omitting_unknown_call(audit):
    unknown_attempt(audit)
    audit.args.allow_incomplete = True
    result = subject.local_run(audit.args)
    assert result["allow_incomplete"] is True and result["report"]["complete"] is False
    assert result["complete"] == [] and result["blocked"] == [100] and len(result["rejected"]) == 89
    assert result["costs"]["attempts"] == result["costs"]["llm1_calls"] == result["costs"]["usage_unknown_attempts"] == 1
    assert result["costs"]["known_cost_usd"] == 0


def test_optin_still_requires_all_90_durable_outcomes(audit):
    audit.args.allow_incomplete = True
    (audit.args.folder / "100/outcome.private.json").unlink()
    with pytest.raises(subject.PendingVerificationError, match="not_all_90_have_outcomes"):
        subject.local_run(audit.args)


def test_full_incomplete_audit_retains_unknown_cost_and_uncertain_ledger(audit, monkeypatch, capsys):
    unknown_attempt(audit)
    audit.args.allow_incomplete = True
    baseline = subject.read_private(audit.args.baseline_file)
    baseline["pending_table_counts"] = {"prod2_match_provenance": 4}
    replace(audit.args.baseline_file, baseline)
    frozen_baseline = audit.args.baseline_file.read_bytes()
    @contextmanager
    def runtime(args):
        yield SimpleNamespace(database_url="synthetic"), None
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement):
            assert statement in {"SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout='30s'"}
    def pg(conn, local, folder):
        local["payment_ledger"] = {"running_count": 0, "uncertain_count": 2, "coordinator_active": False,
            "scope": "all_authorized_ids_including_preexisting_history"}
        return [], {"prod2_match_provenance": 4}
    monkeypatch.setattr(subject.selected, "runtime", runtime)
    monkeypatch.setattr(subject.psycopg, "connect", lambda *a, **k: Connection())
    monkeypatch.setattr(subject, "pg_readback", pg)
    monkeypatch.setattr(subject, "r2_readback", lambda rows, *args: len(rows))
    result = subject.verify(audit.args)
    assert result["complete"] is False and result["completed_subset_verified"] is True
    assert result["usage_unknown_attempts"] == result["costs"]["usage_unknown_attempts"] == 1
    assert result["active_payments"] == 0 and result["uncertain_count"] == 2
    assert result["costs_complete"] is False and result["costs"]["llm1_calls"] == 1
    assert subject.read_private(audit.args.output)["usage_unknown_attempts"] == 1
    assert audit.args.baseline_file.read_bytes() == frozen_baseline
    assert json.loads(capsys.readouterr().out)["complete"] is False


@pytest.mark.parametrize("score", [60, True, float("inf")])
def test_rejected_affinity_threshold_is_validated(audit, score):
    path = audit.args.folder / "100/outcome.private.json"
    outcome = subject.read_private(path)
    outcome["affinity_index"] = score
    if score == float("inf"):
        # Simulate a hostile receipt while normal writers reject non-finite JSON.
        path.write_text(json.dumps(outcome))
        value = runner.report(audit.args.folder, audit.manifest, audit.stage, SimpleNamespace(requests=0), began=runner.time.monotonic())
        (audit.args.folder / "report-1.private.json").write_text(json.dumps(value))
    else:
        replace(path, outcome)
        refresh_report(audit)
    with pytest.raises(subject.PendingVerificationError, match="affinity"):
        subject.local_run(audit.args)


def make_llm1(audit, pages=21):
    conv, docset, analysis = 100, "a" * 64, b"%PDF-synthetic-offline"
    sha = hashlib.sha256(analysis).hexdigest()
    model, route = subject.resolve_llm1_model(audit.manifest, pages, audit.manifest["gemini_model"])
    cache = Path(audit.manifest["cache_folder"]) / ("selected-analysis-" + docset)
    info = {"page_count": pages, "document_set_hash": docset, "analysis_sha256": sha,
        "analysis_bytes": len(analysis), "document_set_identity": {"synthetic": True}, "selection_context": {"synthetic": True}}
    subject.private_write(cache / "manifest.private.json", info)
    (cache / "analysis.pdf").write_bytes(analysis)
    outcome = {"convocation_id": conv, "accepted": True, "status": "llm1_complete", "affinity_index": 80,
        "analysis_page_count": pages, "llm1_model": model, "llm1_model_routing": route,
        "document_set_hash": docset, "analysis_bytes": len(analysis), "llm1_cached": False}
    replace(audit.args.folder / str(conv) / "outcome.private.json", outcome)
    request_hash = "b" * 64
    bundle = {"document_set_hash": docset, "model": model, "cache_key": "prod2:llm1:" + request_hash,
        "analysis_sha256": sha, "document_set_identity": info["document_set_identity"],
        "prompt_hash": "d" * 64,
        "input_tokens": 1000, "output_tokens": 200, "cost_usd": subject.estimate_cost(model, 1000, 200)}
    path = audit.args.folder / str(conv) / ("gemini_extraction-" + request_hash + ".response.private.json")
    subject.private_write(path, bundle)
    refresh_report(audit)
    return SimpleNamespace(outcome=outcome, bundle=bundle, bundle_path=path, info=info, cache=cache)


def make_recovery(audit, *, known_failure=False, copy_old_attempt=False):
    import recover_prod2_selected as recovery
    case = make_llm1(audit)
    root = audit.args.folder.parent / "recovery"
    root.mkdir()
    value = {**audit.manifest, "recovery_policy": recovery.POLICY,
        "original_manifest_hash": subject.hash_request(audit.manifest), "original_folder": str(audit.args.folder.resolve()),
        "recovery_folder": str(root.resolve()), "recovery_ids": [100], "no_discovery": True, "no_redis": True}
    subject.private_write(root / "manifest.private.json", value)
    payload = next(row["payload"] for row in audit.manifest["pending_snapshot"] if row["candidate_id"] == "100")
    proof = {"policy": "prod2_accepted_detail_recovery_v1", "run_id": audit.manifest["run_id"], "convocation_id": 100,
        "opportunity_id": "bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "row": payload["row"], "query": payload["query"],
        "affinity_index": 80, "request_hash": subject.jev_request_hash(payload["row"]["description"], commercial_profiles=audit.manifest["commercial_profiles"])}
    subject.private_write(root / "100/accepted-proof.private.json", proof)
    record = {**case.outcome, "accepted_proof_hash": subject.hash_request(proof), "recovery_manifest_hash": subject.hash_request(value)}
    subject.private_write(root / "100/outcome.private.json", record)
    subject.private_write(root / "100" / case.bundle_path.name, case.bundle)
    case.bundle_path.unlink()
    failed = {"attempt_id": "original-failed-call", "model": case.bundle["model"],
        "status": "response_parse_failure" if known_failure else "error_unknown", "model_call_started": True,
        "provider_usage_known": known_failure, "input_tokens": 70616 if known_failure else None,
        "output_tokens": 24564 if known_failure else None, "cost_usd": 0.0168872 if known_failure else None,
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False,
        "error_type": "TruncatedResponseError" if known_failure else "ReadTimeout",
        "finish_reason": "MAX_TOKENS" if known_failure else None, "page_count": case.info["page_count"],
        "analysis_sha256": case.info["analysis_sha256"], "document_set_hash": case.info["document_set_hash"],
        "document_set_identity": case.info["document_set_identity"], "selection_context": case.info["selection_context"],
        "prompt_hash": case.bundle["prompt_hash"]}
    for part in ("started", "outcome"):
        subject.private_write(audit.args.folder / "100" / ("gemini-selected-original-failed-call." + part + ".private.json"), failed)
        if copy_old_attempt:
            subject.private_write(root / "100" / ("gemini-selected-original-failed-call." + part + ".private.json"), failed)
    success = {"attempt_id": "recovery-success", "model": case.bundle["model"], "status": "success",
        "model_call_started": True, "provider_usage_known": True, "input_tokens": case.bundle["input_tokens"],
        "output_tokens": case.bundle["output_tokens"], "cost_usd": case.bundle["cost_usd"],
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False}
    subject.private_write(root / "100/gemini-selected-recovery-success.outcome.private.json", success)
    replace(audit.args.folder / "100/outcome.private.json", {"convocation_id": 100, "accepted": True,
        "affinity_index": 80, "status": "needs_review", "error_type": failed["error_type"]})
    refresh_report(audit)
    costs = recovery.combined_audit(root)
    report = {"policy": recovery.POLICY, "run_id": audit.manifest["run_id"], "terminal_stage": "llm1", "accepted": 1,
        "llm1_complete": 1, "records": [record], "costs": costs, "local_costs": costs,
        "search_calls": 0, "affinity_calls": 0, "redis_calls": 0}
    subject.private_write(root / "report-1.private.json", report)
    audit.args.recovery_folder = [root]
    return SimpleNamespace(root=root, manifest=value, proof=proof, outcome=record, report=report, original_failed=failed, case=case)


@pytest.mark.parametrize("known_failure", [False, True])
@pytest.mark.parametrize("copy_old_attempt", [False, True])
def test_recovery_final_status_preserves_original_failure_and_unique_costs(audit, known_failure, copy_old_attempt):
    recovery = make_recovery(audit, known_failure=known_failure, copy_old_attempt=copy_old_attempt)
    original_before = (audit.args.folder / "100/outcome.private.json").read_bytes()
    audit.args.allow_incomplete = True
    result = subject.local_run(audit.args)
    assert result["complete"] == [100] and not result["blocked"] and result["recovered_ids"] == [100]
    assert result["original_outcomes"][100]["status"] == "needs_review"
    assert result["costs"]["llm1_calls"] == result["costs"]["attempts"] == 2
    assert result["costs"]["usage_unknown_attempts"] == (0 if known_failure else 1)
    assert result["costs"]["known_cost_usd"] == pytest.approx(recovery.case.bundle["cost_usd"] + (0.0168872 if known_failure else 0))
    assert result["failure_costs"]["known_failure_calls"] == int(known_failure)
    assert result["failure_costs"]["unknown_failure_calls"] == int(not known_failure)
    assert (audit.args.folder / "100/outcome.private.json").read_bytes() == original_before


def test_recovery_does_not_make_timeout_billing_known_by_default(audit):
    make_recovery(audit)
    with pytest.raises(subject.PendingVerificationError, match="provider_usage_unknown"):
        subject.local_run(audit.args)


@pytest.mark.parametrize("field,value", [("recovery_ids", [1]), ("recovery_ids", [101]),
    ("gemini_model", "gemini-2.5-flash-lite"), ("original_manifest_hash", "0" * 64),
    ("llm1_model_routing", {"changed": True}), ("commercial_profiles", [{"changed": True}]),
    ("selector_policy_version", "changed"), ("terminal_stage", "document_selection")])
def test_recovery_cannot_expand_scope_or_change_frozen_context(audit, field, value):
    recovery = make_recovery(audit, known_failure=True)
    recovery.manifest[field] = value
    replace(recovery.root / "manifest.private.json", recovery.manifest)
    with pytest.raises(subject.PendingVerificationError):
        subject.local_run(audit.args)


def test_recovery_cannot_change_previously_attempted_pdf_or_prompt(audit):
    recovery = make_recovery(audit, known_failure=True)
    path = audit.args.folder / "100/gemini-selected-original-failed-call.started.private.json"
    value = subject.read_private(path)
    value["analysis_sha256"] = "0" * 64
    replace(path, value)
    # A receipt's model-input binding is checked independently from its billing.
    refresh_report(audit)
    with pytest.raises(subject.PendingVerificationError, match="recovery_original_paid_input_changed"):
        subject.local_run(audit.args)


@pytest.mark.parametrize("known_failure", [False, True])
def test_recovered_completion_and_complete_billing_are_independent(audit, monkeypatch, capsys, known_failure):
    make_recovery(audit, known_failure=known_failure)
    audit.args.allow_incomplete = True
    baseline = subject.read_private(audit.args.baseline_file)
    baseline["pending_table_counts"] = {"prod2_match_provenance": 4}
    replace(audit.args.baseline_file, baseline)
    @contextmanager
    def runtime(args):
        yield SimpleNamespace(database_url="synthetic"), None
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement):
            assert statement.startswith("SET ")
    def pg(conn, local, folder):
        local["payment_ledger"] = {"running_count": 0, "uncertain_count": 1, "coordinator_active": False,
            "scope": "all_authorized_ids_including_preexisting_history"}
        return [{"id_convocatoria": 100, "id": 41, "model": "gemini-2.5-flash-lite", "actual_facets": 0,
            "schedule_count": 0, "document_count": 0, "publication_count": 0}], {"prod2_match_provenance": 4}
    monkeypatch.setattr(subject.selected, "runtime", runtime)
    monkeypatch.setattr(subject.psycopg, "connect", lambda *a, **k: Connection())
    monkeypatch.setattr(subject, "pg_readback", pg)
    monkeypatch.setattr(subject, "r2_readback", lambda rows, *args: len(rows))
    result = subject.verify(audit.args)
    assert result["complete"] is True and result["costs_complete"] is known_failure
    assert result["original_llm1_complete"] == 0 and result["llm1_complete"] == result["recovered_count"] == 1
    assert result["usage_unknown_attempts"] == int(not known_failure) and result["uncertain_count"] == 1
    assert result["costs"]["llm1_calls"] == 2 and result["original_costs"]["llm1_calls"] == 1
    assert result["original_status_counts"]["needs_review"] == 1 and result["status_counts"]["llm1_complete"] == 1
    assert json.loads(capsys.readouterr().out)["complete"] is True


def make_retry(audit, *, known_failure=True):
    import retry_prod2_llm1 as retry
    import recover_prod2_selected as recovery
    recovered = make_recovery(audit, known_failure=known_failure)
    source, root = audit.args.folder, audit.args.folder.parent / "retry"
    root.mkdir()
    claim = {"id": 91, "evaluation_key": "e" * 64, "status": "claimed",
        "claim_token": "bc4da6f4-41be-4c60-9421-c14e6b5c95d3"}
    request_hash = recovered.case.bundle["cache_key"].removeprefix("prod2:llm1:")
    claim_path = source / "100" / ("gemini_extraction-" + request_hash + ".claim.private.json")
    subject.private_write(claim_path, claim)
    if known_failure:
        subject.private_write(source / "100/gemini-selected-original-failed-call.response.private.json", recovered.original_failed)
    info_path = recovered.case.cache / "manifest.private.json"
    model, routing = subject.resolve_llm1_model(audit.manifest, recovered.case.info["page_count"], audit.manifest["gemini_model"])
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    value = {"policy": retry.POLICY, "source_folder": str(source.resolve()), "retry_folder": str(root.resolve()),
        "convocation_id": 100, "source_manifest_hash": subject.hash_request(audit.manifest),
        "original_outcome_sha256": sha(source / "100/outcome.private.json"), "owner_claim_sha256": sha(claim_path),
        "owner_attempt_sha256": sha(source / "100/gemini-selected-original-failed-call.outcome.private.json"),
        "owner_started_sha256": sha(source / "100/gemini-selected-original-failed-call.started.private.json"),
        "owner_response_audit_sha256": sha(source / "100/gemini-selected-original-failed-call.response.private.json") if known_failure else None,
        "prepared_manifest_sha256": sha(info_path), "evaluation_id": 91, "evaluation_key": claim["evaluation_key"],
        "request_hash": request_hash, "model": model, "page_count": recovered.case.info["page_count"],
        "analysis_sha256": recovered.case.info["analysis_sha256"], "prompt_hash": recovered.case.bundle["prompt_hash"],
        "model_routing": routing, "original_usage_known": known_failure, "allow_unknown_billing": not known_failure,
        "max_output_tokens": 65536, "timeout_ms": 300000, "max_attempts": 1, "global_max_llm1_calls": 90}
    subject.private_write(root / "manifest.private.json", value)
    subject.private_write(source / "100" / retry.AUTHORIZATION_NAME, value)
    subject.private_write(root / "retry-issued.private.json", {"policy": retry.POLICY, "request_hash": request_hash,
        "evaluation_id": 91, "max_attempts": 1})
    success_path = recovered.root / "100/gemini-selected-recovery-success.outcome.private.json"
    success = subject.read_private(success_path)
    success_path.unlink()
    success.update(model=model, page_count=value["page_count"], analysis_sha256=value["analysis_sha256"],
        prompt_hash=value["prompt_hash"], max_output_tokens=65536, timeout_ms=300000)
    subject.private_write(root / "gemini-selected-recovery-success.started.private.json", success)
    subject.private_write(root / "gemini-selected-recovery-success.outcome.private.json", success)
    response_path = recovered.root / "100" / recovered.case.bundle_path.name
    response_path.unlink()
    subject.private_write(root / "retry-response.private.json", recovered.case.bundle)
    subject.private_write(source / "100" / recovered.case.bundle_path.name, recovered.case.bundle)
    result = {"policy": retry.POLICY, "status": "evaluation_reconciled", "convocation_id": 100,
        "evaluation_id": 91, "request_hash": request_hash, "model": model,
        "input_tokens": recovered.case.bundle["input_tokens"], "output_tokens": recovered.case.bundle["output_tokens"],
        "cost_usd": recovered.case.bundle["cost_usd"], "original_outcome_preserved": True,
        "requires_normal_recovery": True, "attempt_id": success["attempt_id"],
        "retry_response_sha256": sha(root / "retry-response.private.json")}
    subject.private_write(root / "retry-result.private.json", result)
    recovered.outcome["llm1_cached"] = True
    replace(recovered.root / "100/outcome.private.json", recovered.outcome)
    recovered.report.update(records=[recovered.outcome], costs=recovery.combined_audit(recovered.root), local_costs=recovery.combined_audit(recovered.root))
    replace(recovered.root / "report-1.private.json", recovered.report)
    refresh_report(audit)
    audit.args.retry_folder = [root]
    return SimpleNamespace(root=root, manifest=value, result=result, recovery=recovered, source=source)


@pytest.mark.parametrize("known_failure", [False, True])
def test_explicit_retry_cost_is_counted_once_beside_recovery(audit, known_failure):
    retry = make_retry(audit, known_failure=known_failure)
    audit.args.allow_incomplete = True
    result = subject.local_run(audit.args)
    assert result["complete"] == [100] and result["costs"]["llm1_calls"] == 2
    assert result["costs"]["usage_unknown_attempts"] == int(not known_failure)
    assert result["retry_records"][0]["status"] == "evaluation_reconciled"
    assert result["retry_records"][0]["new_model_calls"] == 1
    assert result["costs"]["known_cost_usd"] == pytest.approx(retry.recovery.case.bundle["cost_usd"] + (0.0168872 if known_failure else 0))


def test_owner_retry_reference_cannot_be_omitted_from_cost_audit(audit):
    make_retry(audit)
    audit.args.retry_folder = []
    with pytest.raises(subject.PendingVerificationError, match="retry_authorization_unaccounted"):
        subject.local_run(audit.args)


@pytest.mark.parametrize("known_retry_failure", [False, True])
def test_failed_explicit_retry_keeps_its_real_known_or_unknown_billing(audit, known_retry_failure):
    import recover_prod2_selected as recovery_module
    retry = make_retry(audit, known_failure=True)
    for path in (retry.root / "retry-result.private.json", retry.root / "retry-response.private.json",
        retry.source / "100" / retry.recovery.case.bundle_path.name):
        path.unlink()
    path = retry.root / "gemini-selected-recovery-success.outcome.private.json"
    failed = subject.read_private(path)
    failed.update(status="response_parse_failure" if known_retry_failure else "error_unknown",
        error_type="TruncatedResponseError" if known_retry_failure else "ReadTimeout",
        provider_usage_known=known_retry_failure, finish_reason="MAX_TOKENS" if known_retry_failure else None,
        input_tokens=70616 if known_retry_failure else None, output_tokens=24564 if known_retry_failure else None,
        cost_usd=0.0168872 if known_retry_failure else None)
    replace(path, failed)
    retry.recovery.outcome["status"] = "needs_review"
    replace(retry.recovery.root / "100/outcome.private.json", retry.recovery.outcome)
    retry.recovery.report.update(records=[retry.recovery.outcome], llm1_complete=0,
        costs=recovery_module.combined_audit(retry.recovery.root), local_costs=recovery_module.combined_audit(retry.recovery.root))
    replace(retry.recovery.root / "report-1.private.json", retry.recovery.report)
    audit.args.allow_incomplete = True
    result = subject.local_run(audit.args)
    assert result["complete"] == [] and result["blocked"] == [100]
    assert result["retry_records"][0]["status"] == "attempt_failed"
    assert result["costs"]["llm1_calls"] == 2 and result["costs"]["usage_unknown_attempts"] == int(not known_retry_failure)
    assert result["costs"]["known_cost_usd"] == pytest.approx(0.0168872 * (2 if known_retry_failure else 1))
    assert result["failure_costs"]["known_failure_calls"] == (2 if known_retry_failure else 1)
    assert result["failure_costs"]["unknown_failure_calls"] == int(not known_retry_failure)


@pytest.mark.parametrize("field,value", [("convocation_id", 1), ("source_manifest_hash", "0" * 64),
    ("owner_attempt_sha256", "0" * 64), ("model", "gemini-3.1-flash-lite"), ("max_attempts", 2)])
def test_retry_owner_reference_and_original_input_cannot_change(audit, field, value):
    retry = make_retry(audit)
    retry.manifest[field] = value
    replace(retry.root / "manifest.private.json", retry.manifest)
    with pytest.raises(subject.PendingVerificationError):
        subject.local_run(audit.args)


def test_canonical_penalty_repair_keeps_paid_summary_and_raw_immutable():
    raw = {"penalties": [{"cap_pct": 10, "description": "Disponibilidad 99%, tope máximo de penalidades10%."}]}
    before = subject.fingerprint(raw)
    canonical = subject.derive_summary(raw)
    paid = {**canonical, "penalidad_tope_pct": 99}
    subject.validate_canonical_summary(raw, canonical, paid)
    assert canonical["penalidad_tope_pct"] == 10 and paid["penalidad_tope_pct"] == 99
    assert subject.fingerprint(raw) == before


def test_canonical_penalty_repair_rejects_unrelated_summary_change():
    raw = {"penalties": [{"cap_pct": 10}]}
    canonical = subject.derive_summary(raw)
    with pytest.raises(subject.PendingVerificationError, match="pg_unexpected_canonical_summary_change"):
        subject.validate_canonical_summary(raw, canonical, {**canonical, "modalidad": "unexpected"})
    with pytest.raises(subject.PendingVerificationError, match="pg_current_summary_derivation"):
        subject.validate_canonical_summary(raw, {**canonical, "penalidad_tope_pct": 99}, canonical)


def set_recovery_manifest(recovery, value):
    replace(recovery.root / "manifest.private.json", value)
    recovery.outcome["recovery_manifest_hash"] = subject.hash_request(value)
    replace(recovery.root / "100/outcome.private.json", recovery.outcome)
    recovery.report["records"] = [recovery.outcome]
    replace(recovery.root / "report-1.private.json", recovery.report)


def test_explicit_preview_anchors_validate_without_changing_frozen_profile_or_model(audit):
    from buenapro_worker.prod2.document_previews import ANCHORED_PREVIEW_POLICY
    recovery = make_recovery(audit, known_failure=True)
    source_before = (audit.args.folder / "100/outcome.private.json").read_bytes()
    value = {**recovery.manifest, "document_preview_anchors": {"policy_version": ANCHORED_PREVIEW_POLICY,
        "pages_by_source_sha256": {"a" * 64: [58, 59, 60]}}}
    set_recovery_manifest(recovery, value)
    result = subject.local_run(audit.args)
    assert result["complete"] == [100] and result["costs"]["llm1_calls"] == 2
    assert result["manifest"]["profile_id"] == value["profile_id"]
    assert result["manifest"]["gemini_model"] == value["gemini_model"]
    assert (audit.args.folder / "100/outcome.private.json").read_bytes() == source_before


@pytest.mark.parametrize("mapping", [None, {}, {"policy_version": "unknown", "pages_by_source_sha256": {"a" * 64: [58]}},
    {"policy_version": "prod2_local_document_anchor_sample_v1", "pages_by_source_sha256": {"A" * 64: [58]}},
    {"policy_version": "prod2_local_document_anchor_sample_v1", "pages_by_source_sha256": {"a" * 64: [58, 58]}},
    {"policy_version": "prod2_local_document_anchor_sample_v1", "pages_by_source_sha256": {"a" * 64: [True]}},
    {"policy_version": "prod2_local_document_anchor_sample_v1", "pages_by_source_sha256": {"a" * 64: [0]}},
    {"policy_version": "prod2_local_document_anchor_sample_v1", "pages_by_source_sha256": {"a" * 64: [1, 2, 3, 4]}},
])
def test_invalid_preview_anchor_mapping_rejected_before_recovery_overlay(audit, mapping):
    recovery = make_recovery(audit, known_failure=True)
    replace(recovery.root / "manifest.private.json", {**recovery.manifest, "document_preview_anchors": mapping})
    with pytest.raises(subject.PendingVerificationError, match="recovery_preview_anchor_policy_invalid"):
        subject.local_run(audit.args)


@pytest.mark.parametrize("policy", [None, True, "unknown", {"policy": "word_footnotes_at_end_v1"}])
def test_invalid_word_recovery_policy_cannot_be_an_unchecked_extra_field(audit, policy):
    recovery = make_recovery(audit, known_failure=True)
    replace(recovery.root / "manifest.private.json", {**recovery.manifest, "word_conversion_policy": policy})
    with pytest.raises(subject.PendingVerificationError, match="recovery_word_policy_invalid"):
        subject.local_run(audit.args)


def word_case(monkeypatch):
    from test_prod2_selected_completion import word_fixture
    _, bundle = word_fixture(monkeypatch)
    info = {"document_set_identity": deepcopy(bundle["document_set_identity"]),
        "source_document_manifest": deepcopy(bundle["raw_extraction_json"]["source_document_manifest"]),
        "page_count": bundle["raw_extraction_json"]["source_document_manifest"][-1]["end_page"]}
    return bundle, info, {"word_conversion_policy": "word_footnotes_at_end_v1"}


def test_word_recovery_uses_pure_exact_completion_contract_and_preserves_originals(monkeypatch):
    bundle, info, manifest = word_case(monkeypatch)
    before = subject.fingerprint({"bundle": bundle, "prepared": info})
    subject.validate_word_derivative(None, info, manifest)
    subject.validate_word_derivative(bundle, info, manifest)
    assert subject.fingerprint({"bundle": bundle, "prepared": info}) == before


@pytest.mark.parametrize("mutation", ["undeclared", "unknown", "identity_extra", "missing_receipt", "receipt_extra",
    "receipt_policy", "pdf_sha", "derived_placement", "notes_boolean", "page_count", "pin_provenance"])
def test_word_recovery_rejects_every_unbound_policy_or_provenance(monkeypatch, mutation):
    bundle, info, manifest = word_case(monkeypatch)
    record = bundle["raw_extraction_json"]["source_document_manifest"][1]
    if mutation == "undeclared": manifest = {}
    elif mutation == "unknown": manifest["word_conversion_policy"] = "unknown"
    elif mutation == "identity_extra":
        bundle["document_set_identity"]["unapproved_extra"] = True
        info["document_set_identity"] = deepcopy(bundle["document_set_identity"])
    elif mutation == "missing_receipt": record.pop("word_conversion")
    elif mutation == "receipt_extra": record["word_conversion"]["unapproved_extra"] = True
    elif mutation == "receipt_policy": record["word_conversion"]["policy"] = "unknown"
    elif mutation == "pdf_sha": record["word_conversion"]["pdf_sha256"] = "f" * 64
    elif mutation == "derived_placement": record["word_conversion"]["derived_placement"] = "page"
    elif mutation == "notes_boolean": record["word_conversion"]["footnote_count"] = True
    elif mutation == "page_count": info["page_count"] += 1
    elif mutation == "pin_provenance": info["source_document_manifest"][1]["word_conversion"]["footnote_count"] += 1
    if mutation not in {"pin_provenance", "undeclared", "unknown", "identity_extra"}:
        info["source_document_manifest"] = deepcopy(bundle["raw_extraction_json"]["source_document_manifest"])
    with pytest.raises(subject.PendingVerificationError):
        subject.validate_word_derivative(bundle, info, manifest)


def test_legacy_preparation_without_word_flag_keeps_existing_contract():
    subject.validate_word_derivative(None, {"document_set_identity": {"synthetic": True}}, {})


@pytest.mark.parametrize("paid_llm1", [False, True])
def test_anchored_selection_may_change_only_before_any_original_gemini_attempt(tmp_path, paid_llm1):
    from buenapro_worker.prod2.document_previews import ANCHORED_PREVIEW_POLICY
    folder = tmp_path / "original"
    old_selection = {"request_hash": "a" * 64, "selected_ids": []}
    subject.private_write(folder / "100/selection.private.json", old_selection)
    prepared = {"page_count": 1, "analysis_sha256": "b" * 64, "document_set_hash": "c" * 64,
        "document_set_identity": {}, "selection_context": {}}
    outcome = {"llm1_model": "gemini-3.1-flash-lite", "selector_request_hash": "d" * 64, "selected_ids": ["loose-new"]}
    if paid_llm1:
        subject.private_write(folder / "100/gemini-selected-original.started.private.json",
            {**prepared, "model": outcome["llm1_model"], "prompt_hash": "e" * 64})
    explicit = {"document_previews": True, "selector_policy_version": "prod2_jev_document_selection_v3",
        "document_preview_anchors": {"policy_version": ANCHORED_PREVIEW_POLICY, "pages_by_source_sha256": {"f" * 64: [58]}}}
    if paid_llm1:
        with pytest.raises(subject.PendingVerificationError, match="recovery_original_selection_changed"):
            subject.original_paid_inputs(folder, 100, outcome, prepared, explicit)
    else:
        assert subject.original_paid_inputs(folder, 100, outcome, prepared, explicit) == []
    with pytest.raises(subject.PendingVerificationError, match="recovery_original_selection_changed"):
        subject.original_paid_inputs(folder, 100, outcome, prepared)
    assert subject.read_private(folder / "100/selection.private.json") == old_selection


@pytest.mark.parametrize("policy", [None, True, "unknown", {"policy": "compact_unique_requirements_v1"}])
def test_invalid_compact_policy_cannot_be_an_unchecked_extra_field(audit, policy):
    recovery = make_recovery(audit, known_failure=True)
    replace(recovery.root / "manifest.private.json", {**recovery.manifest, "llm1_reading_policy": policy})
    with pytest.raises(subject.PendingVerificationError, match="recovery_reading_policy_invalid"):
        subject.local_run(audit.args)


def compact_case(monkeypatch):
    from test_prod2_selected_completion import sync
    bundle, info, manifest = word_case(monkeypatch)
    manifest["llm1_reading_policy"] = "compact_unique_requirements_v1"
    bundle["selection_context"]["llm1_reading_policy"] = manifest["llm1_reading_policy"]
    sync(bundle)
    info.update(document_set_identity=deepcopy(bundle["document_set_identity"]),
        selection_context=deepcopy(bundle["selection_context"]))
    return bundle, info, manifest


def test_compact_reading_binds_manifest_prepared_identity_and_raw_selected_context(monkeypatch):
    bundle, info, manifest = compact_case(monkeypatch)
    before = subject.fingerprint({"bundle": bundle, "prepared": info})
    subject.validate_reading_derivative(None, info, manifest)
    subject.validate_reading_derivative(bundle, info, manifest)
    subject.validate_word_derivative(bundle, info, manifest)
    assert subject.fingerprint({"bundle": bundle, "prepared": info}) == before


@pytest.mark.parametrize("mutation", ["undeclared", "context_missing", "identity_missing", "context_unknown", "raw_changed", "pages_changed"])
def test_compact_policy_is_not_accepted_from_manifest_label_alone(monkeypatch, mutation):
    bundle, info, manifest = compact_case(monkeypatch)
    if mutation == "undeclared": manifest.pop("llm1_reading_policy")
    elif mutation == "context_missing": info["selection_context"].pop("llm1_reading_policy")
    elif mutation == "identity_missing": info["document_set_identity"]["selection_context"].pop("llm1_reading_policy")
    elif mutation == "context_unknown": info["selection_context"]["llm1_reading_policy"] = "unknown"
    elif mutation == "raw_changed": bundle["raw_extraction_json"]["selection_context"].pop("llm1_reading_policy")
    else: info["page_count"] += 1
    with pytest.raises(subject.PendingVerificationError):
        subject.validate_reading_derivative(bundle, info, manifest)


@pytest.mark.parametrize("mutation", ["missing_roots", "missing_binding", "changed_binding", "undeclared"])
def test_compact_recovery_requires_exact_recomputed_paid_source_binding(audit, monkeypatch, mutation):
    from buenapro_worker.prod2 import compact_reading
    recovery = make_recovery(audit, known_failure=True)
    source = {"100": {"policy": "synthetic exact source", "convocation_id": 100, "fallback_model": "gemini-2.5-flash"}}
    def bind(original, manifest, roots, ids, *, fallback_model=None):
        assert original == audit.args.folder.resolve() and manifest == audit.manifest and ids == [100]
        assert roots == [original]
        assert fallback_model == "gemini-2.5-flash"
        return source
    monkeypatch.setattr(compact_reading, "compact_source_bindings", bind)
    value = {**recovery.manifest, "llm1_reading_policy": "compact_unique_requirements_v1",
        "compact_source_folders": [str(audit.args.folder.resolve())], "compact_sources": deepcopy(source)}
    if mutation == "missing_roots": value.pop("compact_source_folders")
    elif mutation == "missing_binding": value.pop("compact_sources")
    elif mutation == "changed_binding": value["compact_sources"]["100"]["convocation_id"] = 101
    else: value.pop("llm1_reading_policy")
    set_recovery_manifest(recovery, value)
    with pytest.raises(subject.PendingVerificationError, match="compact"):
        subject.recovery_overlay(audit.args, audit.manifest, runner.read_outcomes(audit.args.folder, audit.manifest))


@pytest.mark.parametrize("maximum", [24576, 65536, None])
def test_compact_can_change_prompt_context_only_while_old_paid_bytes_stay_exact(tmp_path, maximum):
    from test_prod2_compact_reading import truncation_fixture
    from buenapro_worker.prod2 import compact_reading
    args, _, _, previous, attempt, _ = truncation_fixture(tmp_path, maximum=maximum or 24576)
    if maximum is None:
        for suffix in ("started", "response", "outcome"):
            path = args.original_folder / "123" / (attempt["attempt_id"] + "." + suffix + ".private.json")
            receipt = subject.read_private(path)
            receipt.pop("max_output_tokens")
            receipt.pop("timeout_ms")
            replace(path, receipt)
    original = subject.read_private(args.original_folder / "manifest.private.json")
    binding = compact_reading.compact_source_bindings(args.original_folder, original, [args.original_folder], [123])["123"]
    assert binding["original_max_output_tokens"] == maximum
    if maximum is None:
        assert binding["original_timeout_ms"] is None
        assert binding["original_generation_limits_policy"] == "legacy_selected_defaults_absent"
        assert binding["original_cost_usd"] == attempt["cost_usd"]
    new_folder = tmp_path / "compact-work"
    new_folder.mkdir()
    copied = new_folder / "source.pdf"
    copied.write_bytes((args.original_folder / "123/source.pdf").read_bytes())
    row = previous["document_set"][0]
    current = compact_reading.prepare_compact_document_set(new_folder, previous["document_set"], {row["candidate_id"]: copied},
        {**previous["selection_context"], "llm1_reading_policy": "compact_unique_requirements_v1"}, binding,
        cache_folder=tmp_path / "compact-cache", max_source_bytes=previous["max_source_bytes"])
    manifest = {"llm1_reading_policy": "compact_unique_requirements_v1", "compact_sources": {"123": binding}}
    frozen = {path: path.read_bytes() for path in (args.original_folder / "123").glob("*.private.json")}
    outcome = {"llm1_model": binding["model"]}
    assert subject.original_paid_inputs(args.original_folder, 123, outcome, current, manifest) == []
    assert all(path.read_bytes() == content for path, content in frozen.items())
    current["analysis_sha256"] = "f" * 64
    with pytest.raises(subject.PendingVerificationError, match="recovery_compact_prepared_changed"):
        subject.original_paid_inputs(args.original_folder, 123, outcome, current, manifest)


@pytest.mark.parametrize("mutation", [None, "invented_output_limit", "invented_timeout", "invented_policy"])
def test_compact_overlay_preserves_absent_provider_limits_in_exact_binding(audit, monkeypatch, mutation):
    from buenapro_worker.prod2 import compact_reading
    recovery = make_recovery(audit, known_failure=True)
    source = {"100": {"convocation_id": 100, "fallback_model": "gemini-2.5-flash",
        "original_max_output_tokens": None, "original_timeout_ms": None,
        "original_generation_limits_policy": "legacy_selected_defaults_absent"}}
    monkeypatch.setattr(compact_reading, "compact_source_bindings", lambda *args, **kwargs: deepcopy(source))
    value = {**recovery.manifest, "llm1_reading_policy": "compact_unique_requirements_v1",
        "compact_source_folders": [str(audit.args.folder.resolve())], "compact_sources": deepcopy(source)}
    if mutation == "invented_output_limit": value["compact_sources"]["100"]["original_max_output_tokens"] = 24576
    elif mutation == "invented_timeout": value["compact_sources"]["100"]["original_timeout_ms"] = 180000
    elif mutation == "invented_policy": value["compact_sources"]["100"]["original_generation_limits_policy"] = "legacy_selected_defaults_recorded"
    set_recovery_manifest(recovery, value)
    original = runner.read_outcomes(audit.args.folder, audit.manifest)
    if mutation is not None:
        with pytest.raises(subject.PendingVerificationError, match="recovery_compact_source_binding_changed"):
            subject.recovery_overlay(audit.args, audit.manifest, original)
    else:
        overlay = subject.recovery_overlay(audit.args, audit.manifest, original)
        assert overlay[0][100]["status"] == "llm1_complete"
        assert subject.read_private(overlay[1][100] / "manifest.private.json")["compact_sources"] == source


@pytest.mark.parametrize("reuse", ["request", "prompt", "evaluation", "model"])
def test_compact_reading_cannot_reuse_an_old_uncertain_payment(reuse):
    source = {"model": "gemini-2.5-flash-lite", "prompt_hash": "a" * 64, "request_hash": "b" * 64, "evaluation_id": 10}
    manifest = {"llm1_reading_policy": "compact_unique_requirements_v1", "compact_sources": {"100": source}}
    bundle = {"model": source["model"], "prompt_hash": "c" * 64, "cache_key": "prod2:llm1:" + "d" * 64}
    evaluation = 11
    subject.validate_compact_new_payment(100, bundle, manifest, evaluation_id=evaluation)
    if reuse == "request": bundle["cache_key"] = "prod2:llm1:" + source["request_hash"]
    elif reuse == "prompt": bundle["prompt_hash"] = source["prompt_hash"]
    elif reuse == "model": bundle["model"] = "gemini-3.1-flash-lite"
    else: evaluation = source["evaluation_id"]
    with pytest.raises(subject.PendingVerificationError, match="recovery_compact_reused_legacy"):
        subject.validate_compact_new_payment(100, bundle, manifest, evaluation_id=evaluation)


@pytest.mark.parametrize("pages,model", [(20, "gemini-3.1-flash-lite"), (21, "gemini-2.5-flash-lite")])
def test_verifier_binds_total_pages_to_actual_model_and_cost(audit, pages, model):
    make_llm1(audit, pages)
    result = subject.local_run(audit.args)
    assert result["complete"] == [100] and result["bundles"][100]["model"] == model


@pytest.mark.parametrize("field,value,code", [
    ("model", "gemini-3.1-flash-lite", "llm1_response_missing"),
    ("analysis_sha256", "0" * 64, "llm1_analysis_hash_binding"),
    ("cache_key", "prod2:llm1:" + "0" * 64, "llm1_cache_key_binding"),
    ("cost_usd", 1, "llm1_price_mismatch"),
])
def test_paid_llm1_bundle_cannot_change_model_hash_or_price(audit, field, value, code):
    case = make_llm1(audit)
    case.bundle[field] = value
    replace(case.bundle_path, case.bundle)
    with pytest.raises(subject.PendingVerificationError, match=code):
        subject.local_run(audit.args)


def test_optin_does_not_weaken_actual_model_price_verification(audit):
    case = make_llm1(audit)
    audit.args.allow_incomplete = True
    case.bundle["cost_usd"] = 1
    replace(case.bundle_path, case.bundle)
    with pytest.raises(subject.PendingVerificationError, match="llm1_price_mismatch"):
        subject.local_run(audit.args)


def test_verified_local_pdf_bytes_cannot_be_replaced(audit):
    case = make_llm1(audit)
    (case.cache / "analysis.pdf").write_bytes(b"%PDF-different")
    with pytest.raises(subject.PendingVerificationError, match="analysis_pdf_bytes_changed"):
        subject.local_run(audit.args)


@pytest.mark.parametrize("failure", [None, "prior_changed", "threshold_mismatch", "missing_member", "new_match", "active_payment", "coordinator_active", "uncertain_ledger"])
def test_pg_readback_is_read_only_and_preserves_prior_paid_history(audit, monkeypatch, failure):
    local = subject.local_run(audit.args)
    prior = {"current_extraction_ids": {i: i + 30 for i in range(1, 11)}, "extractions_sha256": "synthetic"}
    local["baseline"].update(prior_snapshot=prior, pending_table_counts={"prod2_match_provenance": 4})
    monkeypatch.setattr(subject, "prior_snapshot", lambda *args: prior if failure != "prior_changed" else {**prior, "extractions_sha256": "changed"})
    monkeypatch.setattr(subject, "counts", lambda *args: {"prod2_match_provenance": 5 if failure == "new_match" else 4})
    statements = []
    class Result:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows
        def fetchone(self): return self.rows[0] if self.rows else None
    class Connection:
        def execute(self, statement, params=()):
            statements.append(statement)
            assert statement.lstrip().startswith("SELECT")
            if "SELECT * FROM prod2_accepted_run_members" in statement:
                result = [{"convocation_id": conv, "request_hash": subject.jev_request_hash("SOPORTE DE SOFTWARE", commercial_profiles=audit.manifest["commercial_profiles"]),
                    "accepted": False, "result_json": {"affinity_index": 20}, "opportunity_id": None} for conv in audit.ids]
                if failure == "missing_member": result.pop()
                if failure == "threshold_mismatch": result[0]["result_json"]["affinity_index"] = 80
                return Result(result)
            if "FROM prod2_jev_decision_cache WHERE cache_key" in statement:
                return Result([{"status": "done", "result_json": {"affinity_index": 20}}])
            if "SELECT e.*" in statement: return Result([])
            if "status='uncertain'" in statement: return Result([{"n": 2 if failure == "uncertain_ledger" else 0}])
            if "AS n" in statement: return Result([{"n": 1 if failure == "active_payment" else 0}])
            if "FROM pg_locks" in statement: return Result([{"active": failure == "coordinator_active"}])
            pytest.fail("Unexpected audit query")
    if failure and failure != "uncertain_ledger":
        with pytest.raises(subject.PendingVerificationError):
            subject.pg_readback(Connection(), local, audit.args.prior_folder)
    else:
        rows, counts = subject.pg_readback(Connection(), local, audit.args.prior_folder)
        assert rows == [] and counts["prod2_match_provenance"] == 4
        assert local["payment_ledger"]["running_count"] == 0
        assert local["payment_ledger"]["uncertain_count"] == (2 if failure == "uncertain_ledger" else 0)
    assert all(statement.lstrip().startswith("SELECT") for statement in statements)


@pytest.mark.parametrize("sha,length", [("a" * 64, 123), ("changed", 123), ("a" * 64, 124)])
def test_r2_verification_only_heads_expected_hash_and_size(tmp_path, monkeypatch, sha, length):
    import boto3
    env = tmp_path / "env"
    env.write_text("R2_ENDPOINT=https://example.invalid\nR2_ACCESS_KEY_ID=synthetic\nR2_SECRET_ACCESS_KEY=synthetic\nR2_BUCKET=synthetic\n")
    calls = []
    class Client:
        def head_object(self, **kwargs):
            calls.append(kwargs)
            return {"ContentLength": length, "Metadata": {"sha256": sha}}
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Client())
    rows, info = [{"id_convocatoria": 100, "storage_key": "expected.pdf"}], {100: {"analysis_bytes": 123, "analysis_sha256": "a" * 64}}
    if sha == "a" * 64 and length == 123:
        assert subject.r2_readback(rows, info, env) == 1
    else:
        with pytest.raises(subject.PendingVerificationError, match="r2_analysis_missing_or_changed"):
            subject.r2_readback(rows, info, env)
    assert calls == [{"Bucket": "synthetic", "Key": "expected.pdf"}, "closed"]
