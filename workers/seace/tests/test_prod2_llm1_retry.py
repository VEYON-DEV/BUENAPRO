"""Offline retry safety: frozen input, one-shot authorization and crash replay."""
from contextlib import contextmanager
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import retry_prod2_llm1 as subject
from buenapro_worker.prod2 import pdf_preparation
from buenapro_worker.settings import Settings


def payload(conv):
    return {"row": {"procedure_id": 1000 + conv, "convocation_id": conv, "nomenclature": "SYNTHETIC",
        "buyer_name": "SYNTHETIC", "published_at": "2026-05-01T12:00:00-05:00", "description": "SOFTWARE",
        "object_type": "service", "object_label": "Servicio", "reference_amount": "100.00", "currency": "Soles", "seace_version": "3"},
        "query": {"keyword": "software", "object_type": "service", "year": 2026, "published_from": "2026-04-06",
            "published_to": "2026-10-06", "seace_version": "3"},
        "observed_at": "2026-10-06T22:00:00+00:00", "page_number": 1}


@pytest.fixture
def context(tmp_path, monkeypatch):
    original, source, retry, cache = (tmp_path / name for name in ("original", "continuation", "retry-20", "cache"))
    old = {"run_id": "original", "pipeline": "prod2_selected_llm1_v1", "terminal_stage": "llm1",
        "document_scope": "prod2_selected_documents_v1", "gemini_model": "gemini-3.1-flash-lite", "jev_threshold": 60,
        "profile_id": "bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "commercial_profiles": [{"descripcion": "software", "lineas": []}],
        "published_from": "2026-04-06", "published_to": "2026-10-06", "resolved_years": [2026],
        "keywords": ["software"], "objects": ["service", "good"], "cache_folder": str(cache),
        "max_source_mib": 128, "max_expanded_mib": 256, "max_archive_members": 2000,
        "max_candidates": 100, "max_accepted": 10, "selector_high": 80, "selector_low": 20}
    subject.private_write(original / "manifest.private.json", old)
    for conv in range(1, 11):
        subject.private_write(original / str(conv) / "outcome.private.json", {"convocation_id": conv, "accepted": True, "status": "needs_review"})
    args = subject.continuation.parser().parse_args(["--original-folder", str(original), "--folder", str(source)])
    base, old = subject.continuation.plan(args)
    ids = list(range(20, 110))
    queue = {"states": {**{str(c): "done" for c in range(1, 11)}, **{str(c): "queued" for c in ids}},
        "payloads": {str(c): payload(c) for c in ids}, "queue": list(map(str, ids)), "pending": []}
    manifest = subject.continuation.bind_snapshot(base, old, queue)
    subject.private_write(source / "manifest.private.json", manifest)
    owner = source / "20"
    owner.mkdir()
    writer, buffer = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=612, height=792)
    writer.write(buffer)
    pdf = buffer.getvalue()
    path = owner / "source.pdf"
    path.write_bytes(pdf)
    code = "00000000-0000-4000-8000-000000000001"
    row = {"candidate_id": "loose:" + code, "document_uuid": code, "source_kind": "loose", "filename": "bases.pdf",
        "type": "Bases", "stage": "Convocatoria", "published_at": "06/10/2026 12:00",
        "source_url": "https://example.invalid/official/" + code, "original_sha256": subject.sha(path)}
    prepared = pdf_preparation.prepare_selected_document_set(owner, [row], {row["candidate_id"]: path},
        {"official_inventory": [row], "ficha_description": "SOFTWARE"}, optimize_lossless=False, cache_folder=cache)
    settings = Settings(_env_file=None, database_url="postgresql://fake.invalid/offline",
        gemini_api_key="fake-unit-key", gemini_model="gemini-3.1-flash-lite")
    monkeypatch.setattr(subject, "Settings", lambda **kw: settings)
    document_context = {k: prepared[k] for k in ("document_scope", "document_set", "document_set_hash", "document_set_identity", "selection_context")}
    inputs = subject._document_input(settings, prepared["pdf_path"].read_bytes(), "service", row["original_sha256"],
        prepared["source_document_manifest"], document_context)
    request_hash = subject.extraction_request_hash(settings, prepared["pdf_path"].read_bytes(), object_type="service",
        original_sha256=row["original_sha256"], manifest=prepared["source_document_manifest"], document_context=document_context)
    evaluation_key = subject.hash_request({"convocation": 20, "kind": "gemini_extraction", "request_hash": request_hash,
        "model": settings.gemini_model, "policy_version": "prod2_document_extraction_v1", "profile_id": manifest["profile_id"]})
    claim = {"status": "claimed", "id": 123, "claim_token": str(uuid4()), "evaluation_key": evaluation_key}
    subject.private_write(owner / ("gemini_extraction-" + request_hash + ".claim.private.json"), claim)
    attempt_id = "gemini-selected-" + uuid4().hex
    attempt = {"attempt_id": attempt_id, "model": settings.gemini_model, "prompt_hash": inputs["prompt_hash"],
        **{k: prepared[k] for k in ("analysis_sha256", "document_set_hash", "document_set_identity", "selection_context", "page_count")},
        "status": "response_parse_failure", "finish_reason": "MAX_TOKENS", "model_call_started": True,
        "provider_usage_known": True, "input_tokens": 1000, "output_tokens": 24576,
        "cost_usd": subject.estimate_cost(settings.gemini_model, 1000, 24576)}
    for suffix in ("started", "response", "outcome"):
        subject.private_write(owner / (attempt_id + "." + suffix + ".private.json"), attempt)
    subject.private_write(owner / "outcome.private.json", {"convocation_id": 20, "accepted": True,
        "status": "needs_review", "error_type": "TruncatedResponseError"})
    args = subject.parser().parse_args(["--source-folder", str(source), "--folder", str(retry), "--convocation-id", "20"])
    return SimpleNamespace(source=source, original=original, retry=retry, owner=owner, cache=cache, args=args,
        manifest=manifest, settings=settings, request_hash=request_hash, claim=claim, attempt=attempt)


def overwrite(path, value):
    path.unlink()
    subject.private_write(path, value)


def test_plan_offline_binds_exact_input_and_never_opens_services(context, monkeypatch, capsys):
    def forbidden(*a, **k):
        pytest.fail("Offline retry must not reach services")
    monkeypatch.setattr(subject.selected, "runtime", forbidden)
    monkeypatch.setattr(subject.psycopg, "connect", forbidden)
    monkeypatch.setattr(sys, "argv", ["retry", "--source-folder", str(context.source), "--folder", str(context.retry), "--convocation-id", "20"])
    assert subject.main() == 0
    value = json.loads(capsys.readouterr().out)
    assert value["request_hash"] == context.request_hash and value["evaluation_id"] == context.claim["id"]
    assert value["max_output_tokens"] == 65536 and value["timeout_ms"] == 300000
    assert value["provider_calls"] == value["database_writes"] == value["redis_calls"] == value["source_calls"] == 0
    assert not context.retry.exists()
    assert "fake-unit-key" not in json.dumps(value)


@pytest.mark.parametrize("conv", [1, 19, 110])
def test_old_ten_and_outside_ninety_cannot_retry(context, conv):
    context.args.convocation_id = conv
    with pytest.raises(ValueError, match="outside the exact 90"):
        subject.plan(context.args)


def test_timeout_requires_specific_unknown_billing_authorization(context):
    attempt = {**context.attempt, "provider_usage_known": False, "status": "error_unknown", "cost_usd": None,
        "input_tokens": None, "output_tokens": None, "finish_reason": None}
    overwrite(context.owner / (attempt["attempt_id"] + ".outcome.private.json"), attempt)
    overwrite(context.owner / "outcome.private.json", {"convocation_id": 20, "accepted": True, "status": "needs_review", "error_type": "ReadTimeout"})
    with pytest.raises(ValueError, match="explicit authorization"):
        subject.plan(context.args)
    context.args.allow_unknown_billing = True
    assert subject.plan(context.args)["frozen"]["original_usage_known"] is False


def test_known_truncation_requires_larger_output_and_real_usage(context):
    context.args.max_output_tokens = 24576
    with pytest.raises(ValueError, match="larger output"):
        subject.plan(context.args)
    context.args.max_output_tokens = 65536
    bad = {**context.attempt, "cost_usd": None}
    overwrite(context.owner / (bad["attempt_id"] + ".outcome.private.json"), bad)
    with pytest.raises(ValueError, match="MAX_TOKENS"):
        subject.plan(context.args)


@pytest.mark.parametrize("field,value", [("prompt_hash", "a" * 64), ("model", "gemini-2.5-flash-lite"), ("analysis_sha256", "a" * 64)])
def test_changed_prompt_model_or_pdf_receipt_is_rejected(context, field, value):
    attempt = {**context.attempt, field: value}
    for suffix in ("started", "response", "outcome"):
        overwrite(context.owner / (attempt["attempt_id"] + "." + suffix + ".private.json"), attempt)
    with pytest.raises(ValueError):
        subject.plan(context.args)


def test_authorization_binds_id_to_only_one_retry_folder(context):
    data = subject.plan(context.args)
    subject.private_write(data["authorization"], data["frozen"])
    context.args.folder = context.retry.parent / "other-retry"
    with pytest.raises(ValueError, match="different retry authorization"):
        subject.plan(context.args)


def live_row(data):
    frozen = data["frozen"]
    return {"id": data["claim"]["id"], "id_convocatoria": frozen["convocation_id"], "kind": "gemini_extraction",
        "policy_version": "prod2_document_extraction_v1",
        "request_hash": frozen["request_hash"], "evaluation_key": frozen["evaluation_key"], "model": frozen["model"],
        "profile_id": data["source_manifest"]["profile_id"], "claim_token": data["claim"]["claim_token"], "status": "uncertain"}


@pytest.mark.parametrize("field,value", [("status", "running"), ("claim_token", str(uuid4())),
    ("request_hash", "a" * 64), ("id_convocatoria", 21), ("evaluation_key", "a" * 64), ("id", 124)])
def test_live_claim_requires_exact_uncertain_owner(context, field, value):
    data = subject.plan(context.args)
    row = live_row(data)
    row[field] = value
    with pytest.raises(ValueError, match="exact owned uncertain"):
        subject.validate_claim(row, data)


def install_execution(context, monkeypatch, *, locked=True, fail=False):
    data = subject.plan(context.args)
    state = {"calls": 0, "queries": [], "reconciles": 0}
    row = live_row(data)
    class Connection:
        closed = broken = False
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=None):
            state["queries"].append(sql)
            if "pg_try_advisory_lock" in sql: result = {"locked": locked}
            elif "FROM prod2_evaluations" in sql: result = row
            elif "FROM prod2_accepted_run_members" in sql: result = {"accepted": True, "opportunity_id": 99}
            elif "pg_advisory_unlock" in sql: result = {}
            else: pytest.fail("Unexpected database operation: " + sql)
            return SimpleNamespace(fetchone=lambda: result)
    @contextmanager
    def runtime(*a): yield context.settings, None
    monkeypatch.setattr(subject.selected, "runtime", runtime)
    monkeypatch.setattr(subject.psycopg, "connect", lambda *a, **k: Connection())
    class Extractor:
        def __init__(self, settings, prepared, folder, before_request, **limits):
            assert state["queries"][0].startswith("SELECT pg_try_advisory_lock")
            assert limits == {"max_output_tokens": 65536, "timeout_ms": 300000}
            self.before_request = before_request
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(subject, "SelectedGeminiExtractor", Extractor)
    def extract(settings, pdf, *, extractor, **kw):
        extractor.before_request()
        state["calls"] += 1
        if fail: raise TimeoutError("Private provider failure must stay out of logs")
        folder = context.args.folder
        frozen = data["frozen"]
        cost = subject.estimate_cost(frozen["model"], 1000, 200)
        identity = "gemini-selected-" + uuid4().hex
        audit = {"attempt_id": identity, "status": "success", "model_call_started": True, "provider_usage_known": True,
            "input_tokens": 1000, "output_tokens": 200, "cost_usd": cost,
            **{k: frozen[k] for k in ("model", "prompt_hash", "analysis_sha256", "page_count", "max_output_tokens", "timeout_ms")}}
        for suffix in ("started", "response", "outcome"):
            subject.private_write(folder / (identity + "." + suffix + ".private.json"), audit)
        return {"cache_key": "prod2:llm1:" + context.request_hash, "model": frozen["model"], "prompt_hash": frozen["prompt_hash"],
            "analysis_sha256": frozen["analysis_sha256"], "original_sha256": data["anchor_sha"],
            "raw_extraction_json": {"summary": {"descripcion_corta": "Software"}}, "requires_human_review": True,
            "input_tokens": 1000, "output_tokens": 200, "cost_usd": cost, **data["document_context"]}
    monkeypatch.setattr(subject, "extract_document", extract)
    def reconcile(conn, owner, conv, request_hash, settings, profile, callback):
        with pytest.raises(RuntimeError, match="never issue"):
            callback()
        state["reconciles"] += 1
        row["status"] = "done"
        return data["claim"]["id"], subject.read_private(data["response_path"]), True
    monkeypatch.setattr(subject, "paid_llm1", reconcile)
    monkeypatch.setattr(sys, "argv", ["retry", "--source-folder", str(context.source), "--folder", str(context.retry), "--convocation-id", "20", "--execute"])
    return data, state


def test_execution_reconciles_same_claim_and_preserves_old_failure(context, monkeypatch, capsys):
    data, state = install_execution(context, monkeypatch)
    old = {p.name: subject.sha(p) for p in context.owner.glob("*.private.json")}
    assert subject.main() == 0
    assert state["calls"] == state["reconciles"] == 1
    assert all(subject.sha(context.owner / name) == digest for name, digest in old.items())
    result = json.loads(capsys.readouterr().out)
    assert result["evaluation_id"] == data["claim"]["id"] and result["requires_normal_recovery"]
    assert result["global_llm1_calls"] == 2
    assert subject.read_private(data["response_path"]) == subject.read_private(context.retry / "retry-response.private.json")
    assert subject.main() == 0
    assert state["calls"] == 1 and state["reconciles"] == 2


def test_coordinator_lock_rejection_prevents_authorization_and_model(context, monkeypatch):
    _, state = install_execution(context, monkeypatch, locked=False)
    with pytest.raises(RuntimeError, match="coordinator"):
        subject.main()
    assert state["calls"] == 0 and not context.retry.exists()


def test_crash_after_saved_bundle_replays_without_second_generation(context, monkeypatch, capsys):
    data, state = install_execution(context, monkeypatch)
    real_write = subject.private_write
    def crash(path, value):
        if path == data["response_path"]: raise OSError("simulated crash before publication")
        return real_write(path, value)
    monkeypatch.setattr(subject, "private_write", crash)
    with pytest.raises(OSError): subject.main()
    assert state["calls"] == 1 and not data["response_path"].exists()
    monkeypatch.setattr(subject, "private_write", real_write)
    assert subject.main() == 0
    assert state["calls"] == state["reconciles"] == 1


def test_failed_issued_retry_cannot_be_issued_again(context, monkeypatch):
    _, state = install_execution(context, monkeypatch, fail=True)
    with pytest.raises(TimeoutError): subject.main()
    assert state["calls"] == 1 and (context.retry / "retry-issued.private.json").exists()
    assert subject.budget(subject.plan(context.args)) == 2
    with pytest.raises(RuntimeError, match="already issued"): subject.main()
    assert state["calls"] == 1


def test_global_limit_blocks_before_generation_and_keeps_original(context, monkeypatch):
    data, state = install_execution(context, monkeypatch)
    # Every additional synthetic receipt is a distinct original SDK attempt.
    for _ in range(89):
        identity = "gemini-selected-" + uuid4().hex
        subject.private_write(context.source / "synthetic-audit" / (identity + ".started.private.json"), {"attempt_id": identity})
    with pytest.raises(RuntimeError, match="global budget"): subject.main()
    assert state["calls"] == 0 and subject.budget(data) == 90
    assert not data["response_path"].exists()


def recovery(context):
    folder = context.retry.parent / "recovery"
    manifest = {**context.manifest, "recovery_policy": "prod2_accepted_selected_recovery_v1",
        "original_folder": str(context.source), "original_manifest_hash": subject.hash_request(context.manifest),
        "no_discovery": True, "no_redis": True, "recovery_ids": [20]}
    subject.private_write(folder / "manifest.private.json", manifest)
    identity = "gemini-selected-" + uuid4().hex
    subject.private_write(folder / "20" / (identity + ".started.private.json"), {"attempt_id": identity})
    return folder, manifest


def test_budget_counts_bound_recovery_attempts_and_rejects_changed_manifest(context):
    folder, manifest = recovery(context)
    context.args.recovery_folder = [folder]
    data = subject.plan(context.args)
    assert subject.budget(data) == 2
    overwrite(folder / "manifest.private.json", {**manifest, "no_redis": False})
    with pytest.raises(ValueError, match="exact frozen"):
        subject.budget(data)


@pytest.mark.parametrize("changes", [{"original_manifest_hash": "a" * 64}, {"recovery_ids": [1]},
    {"llm1_model_routing": {}}, {"published_from": "2026-01-01"}, {"no_discovery": False}])
def test_recovery_references_must_belong_to_exact_ninety(context, changes):
    folder, manifest = recovery(context)
    overwrite(folder / "manifest.private.json", {**manifest, **changes})
    context.args.recovery_folder = [folder]
    with pytest.raises(ValueError, match="exact frozen"):
        subject.plan(context.args)


def test_copied_paid_attempt_is_rejected_across_audit_roots(context):
    folder, _ = recovery(context)
    context.args.recovery_folder = [folder]
    attempt = context.attempt
    subject.private_write(folder / "20" / (attempt["attempt_id"] + ".started.private.json"), attempt)
    with pytest.raises(ValueError, match="copied"):
        subject.budget(subject.plan(context.args))


def test_paid_response_usage_tampering_is_rejected_on_replay(context, monkeypatch, capsys):
    _, state = install_execution(context, monkeypatch)
    assert subject.main() == 0
    path = next(context.retry.glob("gemini-selected-*.response.private.json"))
    receipt = subject.read_private(path)
    overwrite(path, {**receipt, "cost_usd": 0})
    with pytest.raises(ValueError, match="receipts disagree"):
        subject.main()
    assert state["calls"] == 1 and state["reconciles"] == 1


def test_reconciliation_cannot_return_another_owned_evaluation(context, monkeypatch):
    data, state = install_execution(context, monkeypatch)
    def mismatched(conn, owner, conv, request_hash, settings, profile, callback):
        return data["claim"]["id"] + 1, subject.read_private(data["response_path"]), True
    monkeypatch.setattr(subject, "paid_llm1", mismatched)
    with pytest.raises(ValueError, match="Reconciled evaluation"):
        subject.main()
    assert state["calls"] == 1
    assert not (context.retry / "retry-result.private.json").exists()


def test_consistent_but_false_cost_is_rejected_before_retry(context):
    changed = {**context.attempt, "cost_usd": 0}
    for suffix in ("response", "outcome"):
        overwrite(context.owner / (changed["attempt_id"] + "." + suffix + ".private.json"), changed)
    with pytest.raises(ValueError, match="Known-usage"):
        subject.plan(context.args)
