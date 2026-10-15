"""Offline transition of one diagnosed Gemma quota rejection to Flash-Lite."""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("dinaut_transition_runner", ROOT / "scripts/run_dinaut_three_dot_matches.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []
        self.changed = True

    @contextmanager
    def transaction(self):
        self.calls.append(("BEGIN", ()))
        yield
        self.calls.append(("COMMIT", ()))

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if sql.lstrip().startswith("UPDATE"):
            self.result = {"id": self.row["id"]} if self.changed else None
            if self.changed:
                self.row["status"] = "failed"
        else:
            self.result = deepcopy(self.row)
        return self

    def fetchone(self):
        return self.result


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    runner.select_recommendation_model("gemini-3.1-flash-lite")
    case = {"source_key": "seace_prod2", "source_id": 1046782}
    jev_request = {"model": runner.JEV_MODEL, "state": {"only": "frozen input"}, "questions": {}}
    binding = {"source": "a" * 64, "profile": "b" * 64, "lines": "c" * 64,
        "jev_request": runner.fingerprint(jev_request), "recommendation_model": runner.GEMMA_MODEL,
        "recommendation_prompt": runner.fingerprint(runner.RECOMMENDATION_PROMPT),
        "policy": runner.POLICY_VERSION, "as_of": "2026-10-15T20:00:00+00:00"}
    old_binding = {**binding, "recommendation_model": "gemma-4-31b-it"}
    item = {"case": case, "jev_payload": jev_request, "binding": binding,
            "pipeline_request_hash": runner.fingerprint(binding)}
    claim = {"id": 2142, "claim_token": "original-owner", "status": "claimed"}
    row = {"id": 2142, "id_convocatoria": case["source_id"], "profile_id": runner.PROFILE_ID,
        "kind": "gemini_match", "model": runner.JEV_MODEL + "+gemma-4-31b-it",
        "policy_version": runner.POLICY_VERSION, "request_hash": runner.fingerprint(old_binding),
        "claim_token": claim["claim_token"], "status": "uncertain", "lease_live": False}
    conn = Connection(row)
    gemma_request = {"contents": [{"role": "user", "parts": [{"text": "original compact input"}]}]}
    rejection = {"http_status": 429, "request_sha256": runner.fingerprint(gemma_request), "body": {
        "error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{
            "quotaValue": "16000", "quotaId": "GenerateRequestsInputTokensPerModelPerMinute-FreeTier",
            "quotaDimensions": {"model": "gemma-4-31b-it"}}]}]}}}
    jev = {"http_status": 200, "request_sha256": runner.fingerprint(jev_request), "body": {
        "model": runner.JEV_MODEL, "answers": {}, "usage": {"input_tokens": 1000, "output_tokens": 0}}}
    for name, value in (("jev-request", jev_request), ("jev-response", jev),
                        ("gemma-request", gemma_request), ("gemma-response", rejection)):
        (tmp_path / f"{name}.private.json").write_text(json.dumps(value))
    new_claim = {"id": 3143, "claim_token": "new-owner", "status": "claimed"}
    reservations = []
    def reserve(actual_conn, actual_case, **kwargs):
        assert actual_conn is conn and actual_case == case
        assert conn.row["status"] == "failed"
        assert any(sql == "COMMIT" for sql, _ in conn.calls)
        assert any(tmp_path.rglob("provider-transition.private.json"))
        assert kwargs["model"] == runner.JEV_MODEL + "+gemini-3.1-flash-lite"
        assert kwargs["request_hash"] == item["pipeline_request_hash"]
        reservations.append(kwargs)
        return new_claim
    monkeypatch.setattr(runner, "create_evaluation_before_dispatch", reserve)
    monkeypatch.setattr(runner, "reserved_dispatch", lambda *a, **k: pytest.fail("No provider dispatch in transition"))
    monkeypatch.setattr(runner, "dispatch_once", lambda *a, **k: pytest.fail("No inference in transition"))
    yield {"conn": conn, "case": case, "item": item, "claim": claim, "folder": tmp_path,
           "reservations": reservations, "new_claim": new_claim}
    runner.select_recommendation_model("gemma-4-31b-it")


def execute(bundle):
    return runner.transition_rejected_gemma_claim(bundle["conn"], bundle["case"],
        bundle["item"], bundle["folder"], bundle["claim"])


def edit_receipt(bundle, name, mutate):
    path = bundle["folder"] / f"{name}.private.json"
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value))


def test_transition_reuses_exact_jev_and_commits_old_before_new_claim(bundle):
    assert execute(bundle) == bundle["new_claim"]
    assert len(bundle["reservations"]) == 1
    proof = json.loads((bundle["folder"] / "provider-transition.private.json").read_text())
    assert Decimal(proof["jev_cost_usd"]) == Decimal("0.000042")
    assert proof["old_gemma_generation_returned"] is False
    assert proof["new_jev_inference_authorized"] is False
    assert proof["reused_jev_request_sha256"] == runner.fingerprint(bundle["item"]["jev_payload"])
    assert json.loads((bundle["folder"] / "pg-claim-gemma-original.private.json").read_text()) == bundle["claim"]
    assert json.loads((bundle["folder"] / "pg-claim.private.json").read_text()) == bundle["new_claim"]


@pytest.mark.parametrize("mutation", [
    {"id_convocatoria": 1046842}, {"profile_id": "another-profile"},
    {"claim_token": "other-owner"}, {"status": "running"},
    {"model": "different-provider"}, {"kind": "gemini_extraction"},
    {"policy_version": "different-policy"}, {"request_hash": "d" * 64},
])
def test_unrelated_or_changed_reservation_cannot_be_resolved(bundle, mutation):
    bundle["conn"].row.update(mutation)
    with pytest.raises(runner.ProviderBlocked):
        execute(bundle)
    assert not bundle["reservations"]
    assert not any(sql.lstrip().startswith("UPDATE") for sql, _ in bundle["conn"].calls)


@pytest.mark.parametrize("mutation", [{"source_id": 1046842}, {"source_key": "seace_prod6"}])
def test_other_case_never_uses_the_2142_exception(bundle, mutation):
    bundle["case"].update(mutation)
    with pytest.raises(runner.ProviderBlocked):
        execute(bundle)
    assert not bundle["reservations"]


@pytest.mark.parametrize("receipt,mutate", [
    ("gemma-response", lambda value: value.update(http_status=500)),
    ("jev-response", lambda value: value.update(http_status=500)),
    ("jev-response", lambda value: value.update(request_sha256="f" * 64)),
    ("jev-request", lambda value: value["state"].update(only="other input")),
    ("gemma-response", lambda value: value["body"]["error"]["details"][0]["violations"][0].update(quotaValue="100")),
    ("gemma-response", lambda value: value["body"]["error"]["details"][0]["violations"][0].update(quotaId="RequestsPerDay")),
])
def test_only_diagnosed_429_with_exact_jev_receipt_is_resolved(bundle, receipt, mutate):
    edit_receipt(bundle, receipt, mutate)
    with pytest.raises(runner.ProviderBlocked):
        execute(bundle)
    assert not bundle["reservations"]
    assert not any(sql.lstrip().startswith("UPDATE") for sql, _ in bundle["conn"].calls)


def test_owner_cas_failure_never_creates_replacement_claim(bundle):
    bundle["conn"].changed = False
    with pytest.raises(runner.PublicationConflict):
        execute(bundle)
    assert not bundle["reservations"]


def test_default_gemma_does_not_transition_or_touch_pg(bundle):
    runner.select_recommendation_model("gemma-4-31b-it")
    assert execute(bundle) == bundle["claim"]
    assert not bundle["conn"].calls and not bundle["reservations"]


def current_row(bundle, **changes):
    bundle["conn"].row.update(model=runner.MODEL, request_hash=bundle["item"]["pipeline_request_hash"],
        status="running", lease_live=True, **changes)


@pytest.mark.parametrize("change", [
    {"id_convocatoria": 999}, {"profile_id": "other"}, {"kind": "gemini_extraction"},
    {"model": "old-model"}, {"policy_version": "old-policy"}, {"request_hash": "other"},
    {"claim_token": "other-owner"}, {"lease_live": False},
])
def test_current_claim_requires_source_profile_model_policy_input_token_and_live_lease(bundle, change):
    current_row(bundle)
    bundle["conn"].row.update(change)
    with pytest.raises(runner.ProviderBlocked):
        runner.verify_owned_claim(bundle["conn"], bundle["claim"], bundle["case"],
            bundle["item"], bundle["folder"])


def test_uncertain_claim_can_replay_cached_jev_but_cannot_start_new_recommendation(bundle):
    current_row(bundle)
    bundle["conn"].row.update(status="uncertain", lease_live=False)
    runner.verify_owned_claim(bundle["conn"], bundle["claim"], bundle["case"],
        bundle["item"], bundle["folder"], stage="jev")
    with pytest.raises(runner.ProviderBlocked):
        runner.verify_owned_claim(bundle["conn"], bundle["claim"], bundle["case"],
            bundle["item"], bundle["folder"])


def test_run_case_claims_new_model_before_recommendations_without_new_jev(bundle, monkeypatch):
    case_folder = bundle["folder"] / f"seace_prod2-{bundle['case']['source_id']}"
    case_folder.mkdir()
    for path in list(bundle["folder"].glob("*.private.json")):
        path.replace(case_folder / path.name)
    bundle["folder"] = case_folder
    (case_folder / "pg-claim.private.json").write_text(json.dumps(bundle["claim"]))
    reserve = runner.create_evaluation_before_dispatch
    def reserve_and_expose(conn, case, **kwargs):
        new = reserve(conn, case, **kwargs)
        conn.row.update(id=new["id"], claim_token=new["claim_token"], model=runner.MODEL,
            request_hash=bundle["item"]["pipeline_request_hash"], status="running", lease_live=True)
        return new
    monkeypatch.setattr(runner, "create_evaluation_before_dispatch", reserve_and_expose)
    monkeypatch.setattr(runner, "predispatch", lambda *a: {"previous_match": None, "previous_provenance": None})
    monkeypatch.setattr(runner, "score_match", lambda *a, **k: {"score": 74})
    seen = []
    class ReachedRecommendations(Exception):
        pass
    def dispatch(conn, case, folder, stage, payload, **kwargs):
        seen.append(stage)
        assert bundle["reservations"]
        assert conn.row["id"] == bundle["new_claim"]["id"]
        if stage == "jev":
            assert runner.fingerprint(payload) == runner.fingerprint(bundle["item"]["jev_payload"])
            return json.loads((folder / "jev-response.private.json").read_text())
        assert "gemini-3.1-flash-lite:generateContent" in kwargs["url"]
        raise ReachedRecommendations()
    monkeypatch.setattr(runner, "reserved_dispatch", dispatch)
    monkeypatch.setattr(runner, "gemma_request", lambda *a: {"compact": "recommendation"})
    frozen = {"receipt": {"cutoff_utc": "2026-10-15T20:00:00+00:00"}}
    with pytest.raises(ReachedRecommendations):
        runner.run_case(bundle["conn"], SimpleNamespace(gemini_api_key="not-a-real-key"),
            frozen, {}, bundle["item"], case_folder.parent, "not-a-real-key")
    assert seen == ["gemma"]  # JEV is reused directly from the existing response, no provider POST.
    assert len(bundle["reservations"]) == 1


def validation_rejection(bundle, monkeypatch):
    bundle['case']['source_id'] = 1046842
    bundle['claim']['id'] = 2144
    bundle['conn'].row.update(id=2144,id_convocatoria=1046842,model=runner.MODEL,
        request_hash=bundle['item']['pipeline_request_hash'],status='uncertain')
    receipt={'http_status':400,'request_sha256':runner.fingerprint(bundle['item']['jev_payload']),
             'body':{'detail':{'error_type':'max_tokens_exceeded'}}}
    (bundle['folder']/'jev-response.private.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(runner,'prepare_jev_transport',lambda folder,payload:
        {'payload':{**payload,'state':runner.canonical(payload['state'])}},raising=False)
    return receipt


def test_only_diagnosed_validation_rejection_can_renew_owned_composition(bundle, monkeypatch):
    receipt=validation_rejection(bundle,monkeypatch)
    before=(bundle['folder']/'jev-response.private.json').read_bytes()
    assert runner.recover_rejected_jev_claim(bundle['conn'],bundle['case'],bundle['item'],
        bundle['folder'],bundle['claim'])==bundle['claim']
    updates=[sql for sql,args in bundle['conn'].calls if sql.lstrip().startswith('UPDATE')]
    assert len(updates)==1 and "status='running'" in updates[0]
    for field in ('claim_token','id_convocatoria','profile_id','kind','model','policy_version','request_hash'):
        assert field in updates[0]
    assert (bundle['folder']/'jev-response.private.json').read_bytes()==before
    proof=json.loads((bundle['folder']/'jev-transport-recovery.private.json').read_text())
    assert proof['cost_of_rejected_request_usd'] is None
    assert proof['same_semantic_input'] is True and proof['no_long_context'] is True
    assert not bundle['reservations']


@pytest.mark.parametrize('mutation',[
    {'http_status':429}, {'body':{'detail':{'error_type':'quota_exceeded'}}},
    {'body':{'detail':{'error_type':'max_tokens_exceeded'},'usage':{'input_tokens':100}}},
    {'request_sha256':'different-input'},
])
def test_other_sent_errors_never_authorize_transport_recovery(bundle,monkeypatch,mutation):
    receipt=validation_rejection(bundle,monkeypatch);receipt.update(mutation)
    (bundle['folder']/'jev-response.private.json').write_text(json.dumps(receipt))
    with pytest.raises(runner.ProviderBlocked):
        runner.recover_rejected_jev_claim(bundle['conn'],bundle['case'],bundle['item'],
            bundle['folder'],bundle['claim'])
    assert not any(sql.lstrip().startswith('UPDATE') for sql,args in bundle['conn'].calls)
