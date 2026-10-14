"""Offline Luna persistence and disposable-input cleanup with exact PG parent proof."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from buenapro_worker.extraction.gemini import estimate_cost
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import analysis, llm1_completion, storage_policy, temporary_storage
from buenapro_worker.prod2.batch_runner import private_write
from buenapro_worker.prod2.luna_fallback import LUNA_FALLBACK_POLICY, validated_failure_receipt
from buenapro_worker.prod2.luna_policy import (
    LUNA_REQUEST_POLICY, LUNA_SELECTED_TRANSPORT_POLICY, luna_settings, validate_luna_bundle,
)
from buenapro_worker.prod2.repository import hash_request
from test_prod2_selected_completion import selected_fixture
from test_prod2_llm1_completion import finish
from test_prod2_completed_read_reuse import previous, reuse, CODE as REUSE_CODE, CONV as REUSE_CONV, PROFILE
from test_prod2_luna_integration import as_luna


def failed_parent(bundle, convocation):
    """A terminal exact synthetic failure, not an authorization to pay again."""
    model, request = "gemini-3.1-flash-lite", "e" * 64
    key = hash_request({"convocation": convocation, "kind": "gemini_extraction",
        "request_hash": request, "model": model, "policy_version": "prod2_document_extraction_v1",
        "profile_id": PROFILE})
    claim = {"status": "claimed", "id": 6, "claim_token": "00000000-0000-4000-8000-000000000088",
             "evaluation_key": key}
    attempt = "gemini-selected-" + "1" * 32
    failure = {"policy_version": LUNA_FALLBACK_POLICY, "kind": "gemini_extraction",
        "provider": "gemini", "status": "failed", "reconciled": True,
        "failure_reason": "MAX_TOKENS", "finish_reason": "MAX_TOKENS",
        "finish_reasons": ["MAX_TOKENS"], "model_call_started": True,
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False, "remote_files_pending": [],
        "remote_cleanup_receipt_source": "outcome", "source_attempt_id": attempt, "attempt_id": attempt,
        "document_scope": bundle["document_scope"], "transport_policy": "prod2_selected_files_transport_v1",
        "evaluation_id": claim["id"], "claim_token": claim["claim_token"], "evaluation_key": key,
        "request_hash": request, "convocation_id": convocation, "profile_id": PROFILE,
        "prompt_hash": "d" * 64, "analysis_sha256": bundle["analysis_sha256"],
        "document_set_hash": bundle["document_set_hash"],
        "source_document_manifest_hash": canonical_hash(bundle["raw_extraction_json"]["source_document_manifest"]),
        "attempt_receipt_hash": "a" * 64, "started_receipt_hash": "b" * 64,
        "response_receipt_hash": "c" * 64, "remote_cleanup_receipt_hash": "d" * 64,
        "model": model, "provider_usage_known": True, "cost_is_estimate": True,
        "output_tokens_include_thinking": True, "input_tokens": 1000, "output_tokens": 24576,
        "usage": {"prompt_token_count": 1000, "candidates_token_count": 24576, "thoughts_token_count": 0},
        "cost_usd": estimate_cost(model, 1000, 24576)}
    assert validated_failure_receipt(claim, failure)
    return claim, failure


def luna_fixture(monkeypatch):
    conn, bundle = selected_fixture(monkeypatch)
    settings = luna_settings(SimpleNamespace(gemini_model="gemini-3.1-flash-lite",
                                             gemini_fallback_model="gemini-2.5-flash"))
    context = {key: deepcopy(bundle[key]) for key in ("document_scope", "document_set",
        "document_set_hash", "document_set_identity", "selection_context")}
    inputs = analysis._document_input_from_verified_sha(settings, bundle["analysis_sha256"],
        "service", bundle["original_sha256"], bundle["raw_extraction_json"]["source_document_manifest"], context)
    bundle.update(model="gpt-6-luna", cache_key="prod2:llm1:" + canonical_hash(inputs),
        prompt_hash=inputs["prompt_hash"], prompt_version=inputs["prompt_version"],
        provider_request=deepcopy(LUNA_REQUEST_POLICY), selected_transport_policy=LUNA_SELECTED_TRANSPORT_POLICY,
        allowed_models=["gemini-3.1-flash-lite", "gemini-2.5-flash", "gpt-6-luna"])
    bundle["raw_extraction_json"].update(provider_request=deepcopy(LUNA_REQUEST_POLICY),
        selected_transport_policy=LUNA_SELECTED_TRANSPORT_POLICY)
    _claim, failure = failed_parent(bundle, conn.reservation["id_convocatoria"])
    bundle["llm1_fallback_from"] = {key: failure[key] for key in
                                  ("evaluation_id", "request_hash", "attempt_id", "finish_reason", "cost_usd")}
    conn.reservation.update(model="gpt-6-luna", request_hash=canonical_hash(inputs), result_json=deepcopy(bundle))
    conn.stored["raw_extraction_json"] = deepcopy(bundle["raw_extraction_json"])
    validate_luna_bundle(bundle)
    return conn, bundle


def test_exact_luna_result_persists_and_projects_pending_with_its_own_reservation(monkeypatch):
    conn, bundle = luna_fixture(monkeypatch)
    before, reserved = deepcopy(bundle), deepcopy(conn.reservation)
    result = finish(conn, bundle)
    assert result["projection_pending"] and result["requires_human_review"]
    assert conn.events == ["begin", "commit"]
    assert len(conn.persist_calls) == len(conn.updates) == 1
    assert conn.persist_calls[0][4].model == "gpt-6-luna"
    assert "gpt-6-luna" in conn.persist_calls[0][5]
    metadata = json.loads(conn.updates[0][1][2])
    assert metadata["model"] == "gpt-6-luna"
    assert metadata["selected_transport_policy"] == LUNA_SELECTED_TRANSPORT_POLICY
    assert metadata["request_hash"] == conn.reservation["request_hash"]
    assert bundle == before and conn.reservation == reserved


@pytest.mark.parametrize("field,value", [("provider", "gemini"), ("reasoning_effort", "medium"),
    ("max_output_tokens", 128000), ("store", True), ("response_format", "json_schema"),
    ("service_tier", "priority")])
def test_provider_policy_tampering_rejected_before_persistence(monkeypatch, field, value):
    conn, bundle = luna_fixture(monkeypatch)
    bundle["provider_request"][field] = value
    bundle["raw_extraction_json"]["provider_request"][field] = value
    with pytest.raises(llm1_completion.Llm1CompletionError, match="provider identity"):
        finish(conn, bundle)
    assert not conn.events and not conn.persist_calls and not conn.updates


@pytest.mark.parametrize("mutation", ["transport", "cache", "prompt", "own_model", "parent", "parent_missing"])
def test_tampered_exact_identity_or_parent_binding_never_projects(monkeypatch, mutation):
    conn, bundle = luna_fixture(monkeypatch)
    if mutation == "transport":
        bundle["selected_transport_policy"] = "wrong"
        bundle["raw_extraction_json"]["selected_transport_policy"] = "wrong"
    elif mutation == "cache":
        bundle["cache_key"] = "prod2:llm1:" + "f" * 64
    elif mutation == "prompt":
        bundle["prompt_hash"] = "f" * 64
    elif mutation == "own_model":
        conn.reservation["model"] = "gemini-3.1-flash-lite"
    elif mutation == "parent":
        bundle["llm1_fallback_from"]["request_hash"] = "f" * 64
    else:
        bundle.pop("llm1_fallback_from")
    with pytest.raises(llm1_completion.Llm1CompletionError):
        finish(conn, bundle)
    assert not conn.persist_calls and not conn.updates


class ReadbackConnection:
    """Only SELECTs; no external PostgreSQL connection or provider exists."""
    def __init__(self, row, failed, documents, download):
        self.row, self.failed = row, failed
        self.documents, self.download = documents, download
        self.statements = []

    def execute(self, statement, params):
        assert statement.lstrip().startswith("SELECT")
        self.statements.append((statement, params))
        if "FROM prod2_document_extractions" in statement:
            value = self.row
        elif "FROM prod2_evaluations WHERE id=" in statement:
            value = self.failed
        elif "FROM prod2_document_downloads" in statement:
            value = self.download
        elif "FROM prod2_documents" in statement:
            value = self.documents
        elif "count(*)" in statement:
            value = {"count": 0}
        else:
            raise AssertionError("Unexpected readback query")
        return SimpleNamespace(fetchone=lambda: deepcopy(value), fetchall=lambda: deepcopy(value))


@pytest.fixture
def reused_case(previous):
    f = previous
    bundle = as_luna(f)
    claim, failure = failed_parent(bundle, REUSE_CONV)
    bundle["llm1_fallback_from"] = {key: failure[key] for key in
                                  ("evaluation_id", "request_hash", "attempt_id", "finish_reason", "cost_usd")}
    # A new case retains an original with a real SHA, but no previous PDF or
    # payment directory. Reuse authenticates the stored derivative identity.
    case = f.folder / str(REUSE_CONV)
    case.mkdir()
    current = next(iter(f.files.values()))
    source = case / current.name
    current.replace(source)
    f.files = {key: source for key in f.files}
    f.folder = case
    assert reuse(f)["bundle"] == bundle
    f.converter.assert_not_called()
    documents = [{"document_uuid": doc["document_uuid"], "source_url": doc["source_url"]}
                 for doc in bundle["selection_context"]["official_inventory"]]
    outcome = {"document_storage_policy": storage_policy.OFFICIAL_LINKS_POLICY,
        "status": "llm1_complete", "accepted": True, "convocation_id": REUSE_CONV,
        "extraction_id": f.row["extraction_id"], "llm1_evaluation_id": f.row["evaluation_id"],
        "llm1_request_hash": f.row["request_hash"], "llm1_receipt_hash": canonical_hash(bundle),
        "detail_hash": "f" * 64, "document_set_hash": bundle["document_set_hash"],
        "facet_count": 1, "source_document_manifest": bundle["raw_extraction_json"]["source_document_manifest"],
        "documents": documents}
    private_write(case / "outcome.private.json", outcome)
    private_write(case / (REUSE_CODE + ".download.private.json"), {"document_uuid": REUSE_CODE,
        "sha256": f.sha, "bytes": source.stat().st_size, "format": "docx"})
    row = {"id_convocatoria": REUSE_CONV, "evaluation_id": f.row["evaluation_id"], "model": "gpt-6-luna",
        "raw_extraction_json": deepcopy(bundle["raw_extraction_json"]), "facet_count": 1,
        "is_current": True, "quality": "complete", "evaluation_status": "done", "kind": "gemini_extraction",
        "request_hash": f.row["request_hash"], "result_json": bundle,
        "detail_hash": outcome["detail_hash"], "document_inventory_complete": True}
    failed = {"id": claim["id"], "status": "failed", "result_json": failure,
              "claim_token": claim["claim_token"], "evaluation_key": claim["evaluation_key"]}
    conn = ReadbackConnection(row, failed,
        [{"codigo_alfresco": doc["document_uuid"], "source_url": doc["source_url"]} for doc in documents],
        {"bytes": source.stat().st_size, "format": "docx"})
    return SimpleNamespace(case=case, conn=conn, outcome=outcome, bundle=bundle, claim=claim,
                           failure=failure, source=source, f=f)


def test_new_folder_pg_parent_proof_allows_verified_cleanup_without_old_local_history(reused_case):
    f = reused_case
    assert not list(f.case.glob("gemini_extraction-*.claim.private.json"))
    assert not list(f.case.glob("gemini_extraction-*.failure.private.json"))
    proof = storage_policy.verify_completed_storage(f.conn, f.case, f.outcome)
    assert proof["pg_verified"] and proof["active_payments"] == proof["usage_unknown_attempts"] == 0
    frozen = {p: p.read_bytes() for p in f.case.glob("*.json")}
    result = temporary_storage.cleanup_completed_case(f.case, verification=proof,
                                                       reference_roots=(f.case.parent,))
    assert result["deleted_files"] == 1 and not f.source.exists()
    assert all(p.read_bytes() == data for p, data in frozen.items())
    assert all(sql.lstrip().startswith("SELECT") for sql, _ in f.conn.statements)


@pytest.mark.parametrize("mutation", ["missing_pg", "status", "cost", "analysis", "documents", "owner"])
def test_parent_pg_changes_block_cleanup_and_preserve_current_original(reused_case, mutation):
    f = reused_case
    before = f.source.read_bytes()
    if mutation == "missing_pg":
        f.conn.failed = None
    elif mutation == "status":
        f.conn.failed["status"] = "uncertain"
    elif mutation == "cost":
        f.conn.failed["result_json"]["cost_usd"] += .01
    elif mutation == "analysis":
        f.conn.failed["result_json"]["analysis_sha256"] = "a" * 64
    elif mutation == "documents":
        f.conn.failed["result_json"]["document_set_hash"] = "a" * 64
    else:
        f.conn.failed["claim_token"] = "00000000-0000-4000-8000-000000000077"
    with pytest.raises(ValueError):
        storage_policy.verify_completed_storage(f.conn, f.case, f.outcome)
    assert f.source.read_bytes() == before


@pytest.mark.parametrize("mutation", ["claim", "failure", "one_missing"])
def test_local_parent_evidence_if_present_must_equal_pg_exactly(reused_case, mutation):
    f = reused_case
    stem = f.case / ("gemini_extraction-" + f.failure["request_hash"])
    claim_path, failure_path = stem.with_suffix(".claim.private.json"), stem.with_suffix(".failure.private.json")
    private_write(claim_path, f.claim)
    private_write(failure_path, f.failure)
    assert storage_policy.verify_completed_storage(f.conn, f.case, f.outcome)["pg_verified"]
    if mutation == "claim":
        claim_path.write_text(json.dumps({**f.claim, "evaluation_key": "0" * 64}))
    elif mutation == "failure":
        failure_path.write_text(json.dumps({**f.failure, "cost_usd": 99}))
    else:
        claim_path.unlink()
    with pytest.raises(ValueError, match="Local Luna parent"):
        storage_policy.verify_completed_storage(f.conn, f.case, f.outcome)
    assert f.source.exists()
