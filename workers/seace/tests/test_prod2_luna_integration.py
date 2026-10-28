"""Offline end-to-end payment/identity tests: actual readers, mocked HTTP/ledger."""
import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from buenapro_worker.extraction.gemini import TruncatedResponseError
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import accepted_runner, analysis, selected_read, selected_extractor, selected_luna
from buenapro_worker.prod2 import pdf_preparation
from buenapro_worker.prod2.luna_fallback import default_luna_fallback_config
from buenapro_worker.prod2.luna_policy import LUNA_REQUEST_POLICY, luna_settings, validate_luna_bundle
from buenapro_worker.prod2.luna_transport import LunaResponsesClient
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.selected_control import RunLimits, LimitReached
from buenapro_worker.prod2.selected_payments import audit_summary
from test_prod2_selected_luna import prepared, body
from test_prod2_completed_read_reuse import previous, Connection, CONV, PROFILE, reuse, settings as old_settings


class Ledger:
    def __init__(self):
        self.rows = {}

    @contextmanager
    def transaction(self):
        saved = copy.deepcopy(self.rows)
        try:
            yield
        except Exception:
            self.rows = saved
            raise

    def execute(self, sql, params):
        if "WHERE evaluation_key=%s" in sql:
            result = next((r for r in self.rows.values() if r["evaluation_key"] == params[0]), None)
        elif sql.startswith("SELECT *"):
            result = self.rows.get(params[0])
        elif "SELECT id FROM" in sql:
            conv, profile, exclude = params
            result = next((r for r in self.rows.values() if r["id"] != exclude
                and r["id_convocatoria"] == conv and r["profile_id"] == profile
                and r["status"] in {"pending", "running", "uncertain"}), None)
        elif "status='pending'" in sql:
            row = self.rows[params[0]]
            assert row["claim_token"] == params[1] and row["status"] == "running"
            row.update(status="pending", claim_token=None)
            result = None
        elif "status='uncertain'" in sql:
            row = self.rows[params[0]]
            assert row["claim_token"] == params[1]
            row["status"] = "uncertain"
            result = None
        elif "status='failed'" in sql:
            payload, inputs, outputs, cost, identity, claim = params
            row = self.rows[identity]
            assert row["claim_token"] == claim and row["status"] in {"running", "uncertain"}
            row.update(status="failed", result_json=json.loads(payload), input_tokens=inputs,
                       output_tokens=outputs, cost_usd=cost)
            result = {"id": identity}
        else:
            raise AssertionError("Unexpected payment SQL")
        return SimpleNamespace(fetchone=lambda: copy.deepcopy(result))

    def claim(self, conn, conv, **kwargs):
        key = hash_request({"convocation": conv, **{k: kwargs[k] for k in
            ("kind", "request_hash", "model", "policy_version", "profile_id")}})
        existing = next((r for r in self.rows.values() if r["evaluation_key"] == key), None)
        if existing:
            if existing["status"] == "done":
                return {"status": "cached", "id": existing["id"], "result_json": existing["result_json"]}
            if existing["status"] == "pending":  # Released after a provider rejection: claim again.
                existing.update(status="running", claim_token=str(uuid4()))
                return {"status": "claimed", "id": existing["id"], "claim_token": existing["claim_token"],
                        "evaluation_key": key}
            return {"status": existing["status"], "id": existing["id"]}
        identity, token = len(self.rows) + 1, str(uuid4())
        self.rows[identity] = {"id": identity, "claim_token": token, "evaluation_key": key,
            "id_convocatoria": conv, "status": "running", "result_json": None,
            **{k: kwargs[k] for k in ("kind", "request_hash", "model", "policy_version", "profile_id")}}
        return {"status": "claimed", "id": identity, "claim_token": token, "evaluation_key": key}

    def complete(self, conn, identity, token, result, **usage):
        row = self.rows[identity]
        assert row["claim_token"] == token and row["status"] == "running"
        row.update(status="done", result_json=copy.deepcopy(result), **usage)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_preparation, "prepare_pdf", lambda path, *a, **kw: path)
    value = prepared(tmp_path)
    context = {k: value[k] for k in ("document_scope", "document_set", "document_set_hash",
        "document_set_identity", "selection_context")}
    source = value["document_set"][0]
    options = SimpleNamespace(gemini_api_key="offline", openai_api_key="offline",
        gemini_model="gemini-3.1-flash-lite", gemini_fallback_model="gemini-2.5-flash")
    google = MagicMock()
    google.models.generate_content.return_value = SimpleNamespace(text="{\"partial\":",
        candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
        usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=24576,
            thoughts_token_count=0, total_token_count=25576, cached_content_token_count=0))
    monkeypatch.setattr(selected_extractor.genai, "Client", MagicMock(return_value=google))
    requests, response = [], {"body": body()}
    def endpoint(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response["body"])
    http = httpx.Client(transport=httpx.MockTransport(endpoint))
    monkeypatch.setattr(selected_luna, "LunaResponsesClient", lambda key: LunaResponsesClient(key, client=http))
    ledger = Ledger()
    monkeypatch.setattr(accepted_runner, "claim_evaluation", ledger.claim)
    monkeypatch.setattr(accepted_runner, "complete_evaluation", ledger.complete)
    limits = RunLimits(max_accepted=1, max_candidates=1, max_jev_calls=1, max_llm1_calls=3)
    kwargs = {"convocation_id": 20, "profile_id": "00000000-0000-4000-8000-000000000099",
        "object_type": "service", "anchor": source["document_uuid"], "anchor_sha": source["original_sha256"],
        "pdf": value["pdf_path"].read_bytes(), "prepared": value, "document_context": context,
        "limits": limits, "before_request_factory": lambda issue: issue,
        "fallback_config": default_luna_fallback_config()}
    yield SimpleNamespace(folder=tmp_path, settings=options, ledger=ledger, kwargs=kwargs,
        google=google, requests=requests, response=response)
    http.close()


def run(f):
    return selected_read.paid_selected_read(f.ledger, f.settings, f.folder, **f.kwargs)


def test_known_truncation_completes_luna_in_its_own_reservation_once(pipeline):
    f = pipeline
    before = analysis.extraction_request_hash(f.settings, f.kwargs["pdf"], object_type="service",
        original_sha256=f.kwargs["anchor_sha"], manifest=f.kwargs["prepared"]["source_document_manifest"],
        document_context=f.kwargs["document_context"])
    identity, bundle, hit, digest, execution = run(f)
    assert identity == 2 and not hit and digest != before
    assert f.ledger.rows[1]["status"] == "failed" and f.ledger.rows[2]["status"] == "done"
    assert f.ledger.rows[1]["model"] == "gemini-3.1-flash-lite"
    assert f.ledger.rows[2]["model"] == bundle["model"] == "gpt-6-luna"
    assert bundle["llm1_fallback_from"]["evaluation_id"] == 1
    assert execution["primary_cost_usd"] == f.ledger.rows[1]["cost_usd"]
    validate_luna_bundle(bundle)
    f.google.models.generate_content.assert_called_once()
    assert len(f.requests) == 1 and f.requests[0]["reasoning"] == {"effort": "low"}
    assert f.requests[0]["max_output_tokens"] == 65536 and f.requests[0]["store"] is False
    assert f.kwargs["limits"].calls["llm1"] == 2
    costs = audit_summary(f.folder)
    assert costs["llm1_calls"] == 2 and costs["openai_calls"] == 1 and costs["usage_unknown_attempts"] == 0
    assert costs["known_cost_usd"] == pytest.approx(f.ledger.rows[1]["cost_usd"] + bundle["cost_usd"])
    assert f.ledger.rows[1]["request_hash"] == before
    receipt = json.loads(next(f.folder.glob("openai-selected-*.response.private.json")).read_text())
    assert receipt["prompt_hash"] == bundle["prompt_hash"] and receipt["request_hash"] == digest


def test_luna_cached_result_keeps_old_parent_and_current_primary_charge(pipeline):
    f = pipeline
    _, old, _, digest, _ = run(f)
    frozen = copy.deepcopy(old)
    f.settings.gemini_model = "gemini-2.5-flash-lite"
    # The existing paid Luna identity is independent of Gemini's primary model.
    _, result, hit, reused_digest, execution = run(f)
    assert hit and reused_digest == digest and result == frozen
    assert result["llm1_fallback_from"]["evaluation_id"] == 1
    assert execution["primary_failure"]["evaluation_id"] == 3
    assert execution["primary_cost_usd"] == f.ledger.rows[3]["cost_usd"] > 0
    assert len(f.requests) == 1 and f.kwargs["limits"].calls["llm1"] == 3
    assert f.kwargs["limits"].reserved["llm1"] == 0


@pytest.mark.parametrize("error", [TimeoutError(), ValueError("invalid JSON")])
def test_unknown_or_non_truncation_gemini_failure_never_issues_luna(pipeline, error):
    f = pipeline
    f.google.models.generate_content.side_effect = error
    with pytest.raises(type(error)):
        run(f)
    assert len(f.ledger.rows) == 1 and f.ledger.rows[1]["status"] == "uncertain"
    assert not f.requests and audit_summary(f.folder)["usage_unknown_attempts"] == 1


def test_total_llm1_budget_counts_failed_primary_and_blocks_backup_before_claim(pipeline):
    f = pipeline
    f.kwargs["limits"].maximum["llm1"] = 1
    with pytest.raises(LimitReached):
        run(f)
    assert len(f.ledger.rows) == 1 and f.ledger.rows[1]["status"] == "failed"
    assert not f.requests and audit_summary(f.folder)["llm1_calls"] == 1


def test_luna_truncation_retains_both_paid_attempts_and_never_adds_a_third(pipeline):
    f = pipeline
    f.response["body"] = body("{\"partial\":", status="incomplete", reason="max_output_tokens")
    with pytest.raises(TruncatedResponseError):
        run(f)
    assert [r["status"] for r in f.ledger.rows.values()] == ["failed", "uncertain"]
    assert len(f.requests) == 1 and audit_summary(f.folder)["llm1_calls"] == 2
    before = len(list(f.folder.glob("*.json")))
    with pytest.raises(RuntimeError, match="reconciliation"):
        run(f)
    assert len(f.requests) == 1 and len(list(f.folder.glob("*.json"))) == before


def as_luna(f):
    bundle = f.row["result_json"]
    config = luna_settings(old_settings())
    inputs = analysis._document_input(config, f.pdf, "service", f.sha,
        f.prepared["source_document_manifest"], f.dc)
    bundle.update(model="gpt-6-luna", prompt_hash=inputs["prompt_hash"],
        cache_key="prod2:llm1:" + canonical_hash(inputs), provider_request=copy.deepcopy(LUNA_REQUEST_POLICY),
        selected_transport_policy=inputs["selected_transport_policy"])
    bundle["raw_extraction_json"].update(provider_request=copy.deepcopy(LUNA_REQUEST_POLICY),
        selected_transport_policy=inputs["selected_transport_policy"])
    f.row.update(reserved_model=bundle["model"], extraction_model=bundle["model"],
        request_hash=canonical_hash(inputs), raw_extraction_json=copy.deepcopy(bundle["raw_extraction_json"]))
    f.manifest["llm1_luna_fallback"] = default_luna_fallback_config()
    return bundle


def test_exact_luna_done_reused_after_disposable_pdf_removal_without_repayment(previous):
    f = previous
    bundle = as_luna(f)
    validate_luna_bundle(bundle)
    result = reuse(f)
    assert result["bundle"] == bundle and result["model"] == "gpt-6-luna"
    f.converter.assert_not_called()


@pytest.mark.parametrize("field,value", [("model", "gpt-5.6-luna"), ("prompt_hash", "f" * 64),
    ("selected_transport_policy", "wrong"), ("cache_key", "prod2:llm1:" + "a" * 64)])
def test_luna_receipt_tampering_rejected(previous, field, value):
    bundle = as_luna(previous)
    bundle[field] = value
    with pytest.raises(ValueError):
        validate_luna_bundle(bundle)



# --- Gemini HTTP errors: free rejections, saturation goes once to the fallback model (28/10) ----

def gemini_error(code, status):
    from google.genai import errors as genai_errors
    kind = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return kind(code, {"error": {"code": code, "message": "DO NOT STORE", "status": status}})


def test_saturation_reads_once_with_the_fallback_model_under_its_own_reservation(pipeline, monkeypatch):
    f = pipeline
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    f.google.models.generate_content.side_effect = [gemini_error(503, "UNAVAILABLE"), gemini_error(529, "UNAVAILABLE")]
    with pytest.raises(selected_extractor.SelectedProviderRejected) as rejected:
        run(f)
    assert rejected.value.code == 529
    models = [call.kwargs["model"] for call in f.google.models.generate_content.call_args_list]
    assert models == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    # Two reservations, both released: nothing was billed and both may be claimed again.
    assert [row["status"] for row in f.ledger.rows.values()] == ["pending", "pending"]
    assert {row["model"] for row in f.ledger.rows.values()} == {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite"}
    summary = audit_summary(f.folder)
    assert summary["usage_unknown_attempts"] == 0 and summary["known_cost_usd"] == 0
    assert not f.requests  # Luna is only for a known truncation.


def test_a_retired_model_is_a_free_rejection_without_fallback(pipeline, monkeypatch):
    f = pipeline
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    f.google.models.generate_content.side_effect = gemini_error(404, "NOT_FOUND")
    with pytest.raises(selected_extractor.SelectedProviderRejected):
        run(f)
    assert f.google.models.generate_content.call_count == 1
    assert [row["status"] for row in f.ledger.rows.values()] == ["pending"]
    assert audit_summary(f.folder)["usage_unknown_attempts"] == 0
    # The released reservation is claimed again by a later attempt with the same request.
    f.google.models.generate_content.side_effect = gemini_error(404, "NOT_FOUND")
    with pytest.raises(selected_extractor.SelectedProviderRejected):
        run(f)
    assert len(f.ledger.rows) == 1 and f.google.models.generate_content.call_count == 2


def test_the_saturation_fallback_model_is_allowed_when_the_reading_is_stored(monkeypatch):
    """28/10: 1051736 was read by the fallback and refused on storage for not being the routed model."""
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    assert selected_read.allowed_llm1_models("gemini-3.1-flash-lite") == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    monkeypatch.setenv("PROD6_GEMINI_SATURATION_FALLBACK_MODEL", "")
    assert selected_read.allowed_llm1_models("gemini-3.1-flash-lite") == ["gemini-3.1-flash-lite"]
