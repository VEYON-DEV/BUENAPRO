import httpx
import pytest

from buenapro_worker.prod2.selected_control import RunLimits, LimitReached
from buenapro_worker.prod2.selected_payments import AuditedJevClient, audit_summary, provider_permission
from buenapro_worker.prod2.batch_runner import private_write


def limits(maximum=5):
    return RunLimits(max_accepted=1, max_candidates=1, max_jev_calls=maximum, max_llm1_calls=1)


def test_invalid_answers_still_leave_usage_before_typed_validation(tmp_path):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={
        "answers": "invalid", "usage": {"input_tokens": 1000, "output_tokens": 50}}))
    with httpx.Client(transport=transport) as client:
        audited = AuditedJevClient(tmp_path, limits(), client=client)
        assert audited.post("https://example.test", json={}).json()["answers"] == "invalid"
    costs = audit_summary(tmp_path)
    assert costs["jev_calls"] == 1 and costs["known_cost_usd"] == pytest.approx(.000042)
    assert costs["usage_unknown_attempts"] == 0


def test_timeout_receipt_counts_unknown_charge_and_is_never_retried(tmp_path):
    def timeout(req):
        raise httpx.ReadTimeout("private transport detail")
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        audited = AuditedJevClient(tmp_path, limits(), client=client)
        with pytest.raises(httpx.ReadTimeout):
            audited.post("https://example.test", headers={"Authorization": "Bearer SECRET"})
    report = audit_summary(tmp_path)
    assert report["attempts"] == report["usage_unknown_attempts"] == 1
    assert report["known_cost_usd"] == 0
    assert "SECRET" not in next(tmp_path.glob("*.json")).read_text()
    assert "private transport detail" not in next(tmp_path.glob("*.json")).read_text()


def test_budget_blocks_before_provider_and_creates_no_attempt_receipt(tmp_path):
    called = []
    def response(req):
        called.append(req)
        return httpx.Response(403)
    control = limits(1)
    control.before_request("jev")
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        audited = AuditedJevClient(tmp_path, control, client=client)
        with pytest.raises(LimitReached):
            audited.post("https://example.test")
    assert called == [] and audit_summary(tmp_path)["attempts"] == 0


def test_gemini_started_response_outcome_count_once_and_pre_generation_failure_is_free(tmp_path):
    for stage in ("started", "response", "outcome"):
        private_write(tmp_path / ("gemini-selected-synthetic." + stage + ".private.json"), {
            "attempt_id": "gemini-selected-synthetic", "provider_usage_known": stage != "started",
            "model_call_started": stage != "started", "cost_usd": .01 if stage != "started" else None})
    private_write(tmp_path / "gemini-selected-free.outcome.private.json", {
        "attempt_id": "gemini-selected-free", "model_call_started": False,
        "provider_usage_known": False, "cost_usd": 0, "status": "transport_failed"})
    result = audit_summary(tmp_path)
    assert result["attempts"] == result["llm1_calls"] == 1
    assert result["known_cost_usd"] == .01 and result["usage_unknown_attempts"] == 0


def test_budget_is_checked_before_durable_claim_but_cached_calls_can_finish(tmp_path):
    class Conn:
        def __init__(self, status):
            self.status = status
        def execute(self, *a):
            return self
        def fetchone(self):
            return {"status": self.status} if self.status else None
    control = limits(1)
    control.before_request("jev")
    with pytest.raises(LimitReached):
        with provider_permission(Conn(None), tmp_path, control, provider="jev", kind="selector", request_hash="a" * 64):
            pytest.fail("No payment claim may be created")
    with provider_permission(Conn("done"), tmp_path, control, provider="jev", kind="selector", request_hash="a" * 64) as issue:
        with pytest.raises(LimitReached):
            issue()  # this permit only allows a cache hit; it cannot charge


def test_started_jev_crash_receipt_keeps_unknown_attempt_in_budget(tmp_path):
    private_write(tmp_path / "jev-attempt-interrupted.started.private.json", {
        "attempt_id": "jev-attempt-interrupted", "provider": "jev", "usage_known": False})
    summary = audit_summary(tmp_path)
    assert summary["jev_calls"] == summary["usage_unknown_attempts"] == 1


def test_llm1_cache_only_checks_full_evaluation_key_before_any_new_reservation(tmp_path):
    class Conn:
        def execute(self, statement, params):
            assert "WHERE evaluation_key=%s" in statement
            assert params == ("b" * 64,)
            return self
        def fetchone(self):
            return None  # A receipt for another convocation/model/profile is insufficient.
    control = limits()
    control.before_request("llm1")
    with pytest.raises(LimitReached):
        with provider_permission(Conn(), tmp_path, control, provider="llm1", kind="llm1",
                                 request_hash="a" * 64, evaluation_key="b" * 64):
            pytest.fail("Budget exhausted; cannot claim another evaluation tuple")


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_versioned_payment_reserves_correct_policy_and_reuses_exact_completed_receipt(tmp_path, monkeypatch, version):
    from buenapro_worker.prod2 import selected_payments as subject, document_selection as selection
    from test_prod2_document_selection import response
    policy = selection.DOCUMENT_SELECTION_POLICY_V3 if version == "v3" else selection.DOCUMENT_SELECTION_POLICY_V2
    candidates = [{"candidate_id": "principal", "filename": "TDR.pdf", "source_kind": "loose",
                   "preview_text": "Terminos de referencia del servicio de desarrollo de software"}]
    receipt = {}
    requests = []
    def claim(conn, **kwargs):
        assert kwargs["policy_version"] == policy
        assert kwargs["request_hash"] == selection.document_selection_request_hash(
            "SOFTWARE", candidates, policy_version=policy)
        return {"state": "cached", "result": receipt["result"]} if receipt else {"state": "claimed", "claim_token": "synthetic"}
    monkeypatch.setattr(subject.repo, "claim_selection", claim)
    monkeypatch.setattr(subject.repo, "complete_selection", lambda conn, **kw: receipt.update(result=kw["result"]))
    monkeypatch.setattr(subject.repo, "uncertain_selection", lambda *a, **kw: pytest.fail("Valid selector must finish"))
    def respond(request):
        requests.append(request)
        data = response(("tdr_eett", .95, .01))
        if version == "v3":
            data["answers"].pop("d0000_needed")
            data["answers"].pop("d0000_ambiguity")
            data["answers"]["d0000_action"] = {"type": "choice", "choice": "read", "confidence": .95,
                "probabilities": {"read": .95, "omit": .04, "inspect": .01}}
        return httpx.Response(200, json=data)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        selector = selection.JevDocumentSelector("synthetic", client=http,
                                                policy_version=policy)
        first, reused = subject.paid_document_selection(None, tmp_path, convocation_id=1,
            description="SOFTWARE", candidates=candidates, selector=selector)
        replay, reused_again = subject.paid_document_selection(None, tmp_path, convocation_id=1,
            description="SOFTWARE", candidates=candidates, selector=selector)
    assert not reused and reused_again and len(requests) == 1
    assert first["selected_ids"] == replay["selected_ids"] == ["principal"]
    assert first["request_hash"] == replay["request_hash"]
