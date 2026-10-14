"""Offline payment safety for the current known Gemini truncation only."""
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from buenapro_worker.extraction.gemini import TruncatedResponseError, estimate_cost
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import luna_fallback as subject
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.pdf_preparation import SELECTED_DOCUMENTS_POLICY
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.selected_extractor import SELECTED_TRANSPORT_POLICY


class Conn:
    def __init__(self, row):
        self.row = deepcopy(row)
        self.unresolved, self.updates, self.fail_update = None, [], False

    @contextmanager
    def transaction(self):
        before = deepcopy(self.row)
        try:
            yield
        except Exception:
            self.row = before
            raise

    def execute(self, statement, params):
        if statement.startswith("SELECT *"):
            result = deepcopy(self.row)
        elif "SELECT id FROM" in statement:
            result = self.unresolved
        elif statement.startswith("UPDATE"):
            assert "status='failed'" in statement and "claim_token=%s" in statement
            self.updates.append((statement, params))
            if self.fail_update:
                result = None
            else:
                self.row.update(status="failed", result_json=json.loads(params[0]),
                    input_tokens=params[1], output_tokens=params[2], cost_usd=params[3])
                result = {"id": self.row["id"]}
        else:
            pytest.fail("Unexpected SQL; helper must only reconcile the owned failure")
        return SimpleNamespace(fetchone=lambda: result)


def rewrite(path, value):
    path.unlink()
    private_write(path, value)


@pytest.fixture
def case(tmp_path):
    model, profile = "gemini-3.1-flash-lite", str(uuid4())
    selection = {"ficha_description": "SYNTHETIC SOFTWARE"}
    document_set = [{"document_uuid": str(uuid4()), "original_sha256": "a" * 64}]
    identity = {"document_scope": SELECTED_DOCUMENTS_POLICY,
        "document_set": document_set, "selection_context": selection}
    doc_hash = canonical_hash(identity)
    context = {**identity, "document_set_identity": identity, "document_set_hash": doc_hash}
    inputs = {"original_sha": "a" * 64, "analysis_sha": "b" * 64,
        "manifest": [{**document_set[0], "start_page": 1, "end_page": 1}],
        "doc_class": "bases_service", "prompt_hash": "c" * 64,
        "prompt_version": "synthetic_prompt", "model": model,
        "fallback_model": "gemini-2.5-flash", "document_context": context,
        "selected_transport_policy": SELECTED_TRANSPORT_POLICY}
    request = canonical_hash(inputs)
    key = hash_request({"convocation": 20, "kind": "gemini_extraction", "request_hash": request,
        "model": model, "policy_version": subject.EXTRACTION_POLICY, "profile_id": profile})
    claim = {"status": "claimed", "id": 100, "claim_token": str(uuid4()), "evaluation_key": key}
    stem = tmp_path / ("gemini_extraction-" + request)
    private_write(stem.with_suffix(".claim.private.json"), claim)
    common = {"attempt_id": "gemini-selected-" + uuid4().hex,
        "model": model, "prompt_hash": inputs["prompt_hash"], "analysis_sha256": inputs["analysis_sha"],
        "document_set_hash": doc_hash, "document_set_identity": identity,
        "selection_context": selection, "document_scope": SELECTED_DOCUMENTS_POLICY,
        "transport_policy": SELECTED_TRANSPORT_POLICY, "max_output_tokens": 24576, "timeout_ms": 180000}
    usage = {"provider_usage_known": True, "input_tokens": 1000, "output_tokens": 24576,
        "cost_usd": estimate_cost(model, 1000, 24576), "cost_is_estimate": True,
        "output_tokens_include_thinking": True,
        "usage": {"prompt_token_count": 1000, "candidates_token_count": 24000,
            "thoughts_token_count": 576, "cached_content_token_count": 0},
        "finish_reason": "MAX_TOKENS", "finish_reasons": ["MAX_TOKENS"]}
    started = {**common, "status": "generation_reserved"}
    response = {**common, **usage, "status": "response_received"}
    attempt = {**response, "status": "response_parse_failure", "error_type": "TruncatedResponseError",
        "model_call_started": True, "ended_at": "2026-10-07T12:00:00+00:00",
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False, "remote_files_pending": []}
    for suffix, value in (("started", started), ("response", response), ("outcome", attempt)):
        private_write(tmp_path / (common["attempt_id"] + "." + suffix + ".private.json"), value)
    private_write(tmp_path / (common["attempt_id"] + ".cleanup.private.json"),
        {key: attempt[key] for key in ("attempt_id", "ended_at", "remote_cleanup_pending",
            "remote_cleanup_unknown", "remote_files_pending")})
    conn = Conn({"id": claim["id"], "id_convocatoria": 20, "profile_id": profile,
        "kind": "gemini_extraction", "model": model, "policy_version": subject.EXTRACTION_POLICY,
        "request_hash": request, "evaluation_key": key, "claim_token": claim["claim_token"],
        "status": "uncertain", "result_json": None})
    kwargs = {"convocation_id": 20, "profile_id": profile, "request_hash": request,
        "evaluation_key": key, "model": model, "prompt_hash": inputs["prompt_hash"],
        "analysis_sha256": inputs["analysis_sha"], "document_set_hash": doc_hash,
        "document_set_identity": identity, "selection_context": selection,
        "error": TruncatedResponseError("SECRET transport detail"), "attempt": attempt,
        "request_inputs": inputs}
    return SimpleNamespace(folder=tmp_path, conn=conn, claim=claim, kwargs=kwargs,
        stem=stem, response=response, started=started)


def close(case):
    return subject.close_current_gemini_max_tokens(case.conn, case.folder, **case.kwargs)


def test_current_known_failure_is_terminal_with_charge_and_no_fabricated_extraction(case):
    before = {path.name: path.read_bytes() for path in case.folder.glob("*.json")}
    failure = close(case)
    assert case.conn.row["status"] == "failed" and len(case.conn.updates) == 1
    assert case.conn.row["result_json"] == failure
    assert failure["evaluation_id"] == case.claim["id"] and failure["request_hash"] == case.kwargs["request_hash"]
    assert failure["reconciled"] and failure["finish_reason"] == "MAX_TOKENS"
    assert failure["cost_usd"] == case.conn.row["cost_usd"] == case.kwargs["attempt"]["cost_usd"]
    assert subject.validated_failure_receipt(case.claim, failure)
    assert read_private(case.stem.with_suffix(".failure.private.json")) == failure
    assert not case.stem.with_suffix(".response.private.json").exists()
    assert "SECRET" not in json.dumps(failure)
    assert all((case.folder / name).read_bytes() == content for name, content in before.items())
    assert close(case) == failure and len(case.conn.updates) == 1


@pytest.mark.parametrize("error", [TimeoutError(), ValueError(), RuntimeError()])
def test_other_current_errors_cannot_close_a_payment(case, error):
    case.kwargs["error"] = error
    with pytest.raises(subject.LunaFallbackError):
        close(case)
    assert not case.conn.updates and not case.stem.with_suffix(".failure.private.json").exists()


@pytest.mark.parametrize("field,value", [
    ("provider_usage_known", False), ("cost_usd", None), ("cost_usd", True),
    ("cost_usd", .1), ("output_tokens", 24577), ("finish_reason", "STOP"),
    ("model_call_started", False), ("prompt_hash", "d" * 64), ("model", "gemini-2.5-flash-lite"),
    ("analysis_sha256", "e" * 64), ("error_type", "ValueError"), ("max_output_tokens", True),
])
def test_unknown_or_changed_attempt_is_never_reconciled(case, field, value):
    case.kwargs["attempt"] = {**case.kwargs["attempt"], field: value}
    rewrite(case.folder / (case.kwargs["attempt"]["attempt_id"] + ".outcome.private.json"), case.kwargs["attempt"])
    with pytest.raises((subject.LunaFallbackError, ValueError)):
        close(case)
    assert case.conn.row["status"] == "uncertain" and not case.conn.updates


@pytest.mark.parametrize("field,value", [("request_hash", "f" * 64), ("analysis_sha", "f" * 64),
    ("model", "gpt-6-luna"), ("selected_transport_policy", subject.LUNA_TRANSPORT_POLICY)])
def test_actual_primary_input_hash_and_transport_must_be_exact(case, field, value):
    case.kwargs["request_inputs"] = {**case.kwargs["request_inputs"], field: value}
    with pytest.raises(subject.LunaFallbackError):
        close(case)
    assert not case.conn.updates


@pytest.mark.parametrize("field,value", [("id_convocatoria", 21), ("claim_token", str(uuid4())),
    ("request_hash", "f" * 64), ("evaluation_key", "f" * 64), ("status", "done"),
    ("profile_id", str(uuid4())), ("model", "gpt-6-luna")])
def test_exact_current_postgres_ownership_is_required(case, field, value):
    case.conn.row[field] = value
    with pytest.raises(subject.LunaFallbackError):
        close(case)
    assert not case.conn.updates


def test_another_historical_unresolved_payment_blocks_fallback(case):
    case.conn.unresolved = {"id": 99}
    with pytest.raises(subject.LunaFallbackError, match="Another unresolved"):
        close(case)
    assert case.conn.row["status"] == "uncertain" and not case.conn.updates


@pytest.mark.parametrize("field,value", [("remote_cleanup_pending", 1),
    ("remote_cleanup_pending", False), ("remote_cleanup_unknown", True),
    ("remote_files_pending", ["files/owned-pending"]), ("attempt_id", "gemini-selected-" + uuid4().hex)])
def test_pending_unknown_or_wrong_remote_cleanup_blocks_fallback(case, field, value):
    path = case.folder / (case.kwargs["attempt"]["attempt_id"] + ".cleanup.private.json")
    rewrite(path, {**read_private(path), field: value})
    with pytest.raises(subject.LunaFallbackError, match="remote cleanup"):
        close(case)
    assert not case.conn.updates


def test_final_cleanup_supersedes_pending_outcome_and_is_bound_to_failure(case):
    case.kwargs["attempt"]["remote_cleanup_pending"] = 1
    case.kwargs["attempt"]["remote_files_pending"] = ["files/owned"]
    rewrite(case.folder / (case.kwargs["attempt"]["attempt_id"] + ".outcome.private.json"), case.kwargs["attempt"])
    failure = close(case)
    assert failure["remote_cleanup_pending"] == 0
    assert failure["remote_files_pending"] == []
    assert failure["remote_cleanup_receipt_source"] == "cleanup"


def test_outcome_requires_complete_cleanup_proof_if_final_receipt_is_absent(case):
    (case.folder / (case.kwargs["attempt"]["attempt_id"] + ".cleanup.private.json")).unlink()
    failure = close(case)
    assert failure["remote_cleanup_receipt_source"] == "outcome"


def test_failed_update_keeps_uncertain_claim_and_all_inputs(case):
    case.conn.fail_update = True
    with pytest.raises(subject.LunaFallbackError, match="ownership changed"):
        close(case)
    assert case.conn.row["status"] == "uncertain"
    assert not case.stem.with_suffix(".failure.private.json").exists()


def test_linked_missing_and_conflicting_local_receipts_retain_payment(case):
    outcome = case.folder / (case.kwargs["attempt"]["attempt_id"] + ".outcome.private.json")
    target = case.folder / "linked.private.json"
    target.write_bytes(outcome.read_bytes())
    outcome.unlink()
    outcome.symlink_to(target)
    with pytest.raises(subject.LunaFallbackError, match="linked"):
        close(case)
    assert not case.conn.updates


@pytest.mark.parametrize("field,value", [("reconciled", False), ("provider", "openai"),
    ("claim_token", str(uuid4())), ("evaluation_key", "f" * 64), ("request_hash", "f" * 64),
    ("provider_usage_known", False), ("attempt_receipt_hash", "bad"), ("cost_usd", .5)])
def test_cleanup_accepts_only_an_exact_terminal_failure(case, field, value):
    failure = close(case)
    failure[field] = value
    assert not subject.validated_failure_receipt(case.claim, failure)


@pytest.mark.parametrize("field,value", [("remote_cleanup_pending", 1),
    ("remote_cleanup_pending", False), ("remote_cleanup_unknown", True),
    ("remote_files_pending", ["files/owned"]), ("remote_cleanup_receipt_hash", "bad")])
def test_cleanup_rejects_failure_receipts_with_remote_uncertainty(case, field, value):
    failure = close(case)
    failure[field] = value
    assert not subject.validated_failure_receipt(case.claim, failure)


def test_frozen_config_is_exact_and_does_not_mutate_defaults():
    config = subject.default_luna_fallback_config()
    assert subject.validate_luna_fallback_config(config) == config
    subject.validate_luna_fallback_config(config)["model"] = "mutated"
    assert subject.default_luna_fallback_config()["model"] == "gpt-6-luna"
    for value in (None, {}, {**config, "reasoning_effort": "medium"},
            {**config, "max_attempts": True}, {**config, "extra": "value"}):
        with pytest.raises(subject.LunaFallbackError):
            subject.validate_luna_fallback_config(value)


def legacy_absent_bounds(case):
    """Synthetic historical receipts omit fields; never fill missing facts."""
    attempt_id = case.kwargs["attempt"]["attempt_id"]
    for suffix in ("started", "response", "outcome"):
        path = case.folder / (attempt_id + "." + suffix + ".private.json")
        value = read_private(path)
        value.pop("max_output_tokens")
        value.pop("timeout_ms")
        rewrite(path, value)
        if suffix == "outcome":
            case.kwargs["attempt"] = value


def test_legacy_missing_bounds_requires_explicit_opt_in(case):
    legacy_absent_bounds(case)
    with pytest.raises(subject.LunaFallbackError):
        close(case)
    assert not case.conn.updates
    case.kwargs["legacy_generation_limits_policy"] = "legacy_selected_defaults_absent"
    before = {p.name: p.read_bytes() for p in case.folder.glob("*.json")}
    failure = close(case)
    assert failure["generation_limits_audit_policy"] == "limits_not_recorded"
    assert failure["legacy_generation_limits_policy"] == "legacy_selected_defaults_absent"
    assert "max_output_tokens" not in failure and "timeout_ms" not in failure
    assert subject.validated_failure_receipt(case.claim, failure)
    assert all((case.folder / name).read_bytes() == content for name, content in before.items())
    assert close(case) == failure and len(case.conn.updates) == 1


@pytest.mark.parametrize("suffix", ["started", "response", "outcome"])
@pytest.mark.parametrize("field,value", [("max_output_tokens", 24576), ("timeout_ms", 180000),
                                       ("max_output_tokens", None), ("timeout_ms", True)])
def test_legacy_opt_in_rejects_partial_or_recorded_generation_facts(case, suffix, field, value):
    legacy_absent_bounds(case)
    case.kwargs["legacy_generation_limits_policy"] = "legacy_selected_defaults_absent"
    path = case.folder / (case.kwargs["attempt"]["attempt_id"] + "." + suffix + ".private.json")
    receipt = {**read_private(path), field: value}
    rewrite(path, receipt)
    if suffix == "outcome":
        case.kwargs["attempt"] = receipt
    with pytest.raises(subject.LunaFallbackError):
        close(case)
    assert not case.conn.updates and case.conn.row["status"] == "uncertain"


@pytest.mark.parametrize("field,value", [("finish_reason", "STOP"), ("provider_usage_known", False),
                                       ("cost_usd", .5), ("claim_token", str(uuid4()))])
def test_legacy_opt_in_preserves_finish_usage_and_owner_guards(case, field, value):
    legacy_absent_bounds(case)
    case.kwargs["legacy_generation_limits_policy"] = "legacy_selected_defaults_absent"
    if field == "claim_token":
        case.conn.row[field] = value
    else:
        case.kwargs["attempt"][field] = value
        rewrite(case.folder / (case.kwargs["attempt"]["attempt_id"] + ".outcome.private.json"), case.kwargs["attempt"])
    with pytest.raises(subject.LunaFallbackError):
        close(case)
    assert not case.conn.updates


@pytest.mark.parametrize("field,value", [("generation_limits_audit_policy", "recorded"),
                                       ("legacy_generation_limits_policy", "invented"),
                                       ("max_output_tokens", 24576), ("timeout_ms", 180000)])
def test_cleanup_rejects_legacy_limit_inventions(case, field, value):
    legacy_absent_bounds(case)
    case.kwargs["legacy_generation_limits_policy"] = "legacy_selected_defaults_absent"
    failure = close(case)
    failure[field] = value
    assert not subject.validated_failure_receipt(case.claim, failure)


@pytest.mark.parametrize("policy", ["", "invented", True, {}])
def test_legacy_opt_in_accepts_only_the_exact_policy(case, policy):
    case.kwargs["legacy_generation_limits_policy"] = policy
    with pytest.raises(subject.LunaFallbackError, match="Unsupported legacy"):
        close(case)
    assert not case.conn.updates
