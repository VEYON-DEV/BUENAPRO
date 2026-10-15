"""Shared-profile batch safety, offline only; all external calls are blocked."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("dinaut_three_dot_batches", ROOT / "scripts/run_dinaut_three_dot_gemma_batches.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
run = subject.run


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    monkeypatch.setattr(subject.requests, "post", lambda *a, **k: pytest.fail("No HTTP in offline batch tests"))
    monkeypatch.setattr(subject.psycopg, "connect", lambda *a, **k: pytest.fail("No PG in offline batch tests"))
    monkeypatch.setattr(run, "production_settings", lambda *a, **k: pytest.fail("No SSH in offline batch tests"))
    monkeypatch.setattr(run, "reserved_dispatch", lambda *a, **k: pytest.fail("No inference in offline batch tests"))
    monkeypatch.setattr(run, "GEMMA_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setattr(run, "MODEL", run.JEV_MODEL + "+gemini-3.1-flash-lite")


def payload(source_index=1):
    return {"state": {"requirements": [{"source_index": source_index}],
                      "profile": {"documents": [{"ref": "D1"}]}}}


def recommendation(case_key, source_index=1):
    return {"case_key": case_key, "resumen": "Falta conformidad específica.",
        "requisitos": [{"source_index": source_index, "evidencia": "D1: contrato software.",
                        "brecha": "Conformidad pendiente.", "accion": "Adjuntar conformidad.", "evidence_refs": ["D1"]}],
        "acciones_recomendadas": ["Adjuntar la conformidad correspondiente."]}


def saved_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "FOLDER", tmp_path)
    keys = ["seace_prod2-19", "seace_prod6-20"]
    items = {k: {"case": {"source_key": k.rsplit("-", 1)[0], "source_id": int(k.rsplit("-", 1)[1])},
                 "jev_payload": payload(index + 1)} for index, k in enumerate(keys)}
    folder = tmp_path / "gemma-batches/batch-example"
    request = {"contents": [{"role": "user", "parts": [{"text": "Frozen shared context"}]}]}
    digest = run.fingerprint(request)
    plan = {"case_keys": keys, "request_sha256": digest,
            "jev_requests": {k: run.fingerprint(v["jev_payload"]) for k, v in items.items()}}
    receipt = {"request_sha256": digest, "http_status": 200, "body": {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({
            "evaluations": [recommendation(k, index + 1) for index, k in enumerate(keys)]})}]}}],
        "usageMetadata": {"promptTokenCount": 101, "candidatesTokenCount": 33, "thoughtsTokenCount": 5}}}
    for name, value in (("plan.private.json", plan), ("gemma-request.private.json", request), ("gemma-response.private.json", receipt)):
        run.private_json(folder / name, value)
    return folder, items, plan, request, receipt


def test_prepare_shared_batches_is_offline_and_does_not_start_a_stage(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subject, "FOLDER", tmp_path)
    monkeypatch.setattr(run, "prepare", lambda folder: ({}, {}, [{"case": {"source_key": "seace_prod2", "source_id": 19}}], {}))
    monkeypatch.setattr(sys, "argv", ["batch", "--recommendation-model", "gemini-3.1-flash-lite"])
    subject.main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "prepared_shared_batches", "ready": 1, "inference_calls": 0}
    assert list(tmp_path.iterdir()) == []


def test_gemini_json_request_and_no_gemma_rate_sleep(monkeypatch):
    monkeypatch.setattr(subject, "build_recommendation_batch", lambda contexts: {"profile": {"documents": []}, "evaluations": list(contexts)})
    request = subject.batch_request({"seace_prod2-19": {
        "profile":{"document_columns":["ref"],"documents":[["D1"]]},
        "evaluation":{"requirements":[{"source_index":1}]}}})
    assert request["generationConfig"]["responseMimeType"] == "application/json"
    assert request["generationConfig"]["responseSchema"]["properties"]["evaluations"]["required"] == ['seace_prod2-19']
    monkeypatch.setattr(subject.time, "sleep", lambda *_: pytest.fail("Gemma pacing must not delay explicitly selected Gemini"))
    subject.paced_wait(Path("/nonexistent-not-read"), 29000)


def test_count_tokens_is_exact_cached_and_never_generation(tmp_path, monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {"totalTokens": 1234}
    def post(url, **kwargs):
        calls.append((url, kwargs))
        assert url.endswith("gemini-3.1-flash-lite:countTokens")
        assert kwargs["json"]["generateContentRequest"]["model"] == "models/gemini-3.1-flash-lite"
        assert kwargs["allow_redirects"] is False
        return Response()
    monkeypatch.setattr(subject.requests, "post", post)
    settings = SimpleNamespace(gemini_api_key="fake-offline-key")
    request = {"contents": [{"parts": [{"text": "Exact frozen text"}]}]}
    assert subject.count_input(settings, request, tmp_path) == 1234
    assert subject.count_input(settings, request, tmp_path) == 1234
    with pytest.raises(run.PublicationConflict):
        subject.count_input(settings, {"contents": "changed"}, tmp_path)
    assert len(calls) == 1
    assert "fake-offline-key" not in (tmp_path / "token-count.private.json").read_text()


def test_oversized_exact_count_sends_no_generation_or_source_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "batch_request", lambda contexts: {"contents": "sample"})
    monkeypatch.setattr(subject, "count_input", lambda *_: subject.INPUT_BUDGET + 1)
    monkeypatch.setattr(run, "predispatch", lambda *a, **k: pytest.fail("No source dispatch for oversized input"))
    success, count = subject.execute_batch(None, None, tmp_path / "batch", ["seace_prod2-19"],
        {"seace_prod2-19": {}}, {"seace_prod2-19": {"jev_payload": payload()}})
    assert success is False and count == subject.INPUT_BUDGET + 1


def test_successful_recovery_binds_both_sources_exactly_and_does_not_pay_again(tmp_path, monkeypatch):
    folder, items, plan, _request, receipt = saved_batch(tmp_path, monkeypatch)
    usage = subject.bind_saved_batch(folder, items)
    assert subject.bind_saved_batch(folder, items) == usage
    totals = {name: 0 for name in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount")}
    for k in plan["case_keys"]:
        case_folder = tmp_path / k
        binding = json.loads((case_folder / "gemma-batch-binding.private.json").read_text())
        assert binding["case_key"] == k
        assert binding["jev_request_sha256"] == run.fingerprint(items[k]["jev_payload"])
        assert binding["batch_response_sha256"] == run.fingerprint(receipt)
        result, allocated = run.saved_batch_recommendation(case_folder, items[k])
        assert result["requisitos"][0]["source_index"] == items[k]["jev_payload"]["state"]["requirements"][0]["source_index"]
        for name in totals:
            totals[name] += allocated[name]
    assert totals == usage


def test_old_shared_response_cannot_be_rebound_to_different_jev_payload(tmp_path, monkeypatch):
    folder, items, _plan, _request, _receipt = saved_batch(tmp_path, monkeypatch)
    first = next(iter(items))
    items[first]["jev_payload"]["state"]["opportunity"] = {"description": "Changed source snapshot"}
    with pytest.raises((ValueError, run.PublicationConflict, run.ProviderBlocked)):
        subject.bind_saved_batch(folder, items)
    assert not (tmp_path / first / "gemma-batch-binding.private.json").exists()


def test_receipt_sha_change_and_wrong_case_identity_block_saved_recommendation(tmp_path, monkeypatch):
    folder, items, _plan, _request, receipt = saved_batch(tmp_path, monkeypatch)
    subject.bind_saved_batch(folder, items)
    first = next(iter(items)); case_folder = tmp_path / first
    wrong = deepcopy(items[first]); wrong["case"]["source_id"] += 1
    with pytest.raises(run.PublicationConflict, match="another JEV input"):
        run.saved_batch_recommendation(case_folder, wrong)
    receipt["body"]["usageMetadata"]["promptTokenCount"] += 1
    run.private_json(folder / "gemma-response.private.json", receipt)
    with pytest.raises(run.PublicationConflict, match="hash mismatch"):
        run.saved_batch_recommendation(case_folder, items[first])


def test_recommendation_batch_coverage_cannot_mix_sources_or_inject_scores(tmp_path, monkeypatch):
    _folder, items, _plan, _request, receipt = saved_batch(tmp_path, monkeypatch)
    keys = list(items)
    body = {"evaluations": [recommendation(keys[0], 1), recommendation(keys[1], 2)]}
    body["evaluations"][0]["score"] = 100
    receipt["body"]["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(body)
    with pytest.raises(ValueError):
        subject.recommendations_from_receipt(receipt, {k: v["jev_payload"] for k, v in items.items()})
    body["evaluations"] = [recommendation(keys[1], 2), recommendation(keys[0], 1)]
    receipt["body"]["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(body)
    with pytest.raises(ValueError, match="coverage/order"):
        subject.recommendations_from_receipt(receipt, {k: v["jev_payload"] for k, v in items.items()})


def test_completed_incomplete_output_requires_an_explicit_bound_diagnosis(tmp_path,monkeypatch):
    folder,items,plan,request,receipt=saved_batch(tmp_path,monkeypatch)
    first=plan['case_keys'][0]
    items[first]['jev_payload']['state']['requirements'].append({'source_index':2})
    plan['jev_requests'][first]=run.fingerprint(items[first]['jev_payload'])
    raw=json.loads(receipt['body']['candidates'][0]['content']['parts'][0]['text'])
    raw['evaluations'][0]['acciones_recomendadas']='Conservar este texto exacto.'
    receipt['body']['candidates'][0]['content']['parts'][0]['text']=json.dumps(raw)
    run.private_json(folder/'plan.private.json',plan)
    run.private_json(folder/'gemma-response.private.json',receipt)
    original=(folder/'gemma-response.private.json').read_bytes()
    assert not subject.explicitly_superseded_incomplete(folder,items)
    diagnosis=subject.incomplete_response_diagnosis(folder,items)
    assert diagnosis['missing_source_indices'][first]==[2]
    assert diagnosis['action_string_cases']==[first]
    assert diagnosis['automatic_retry'] is False and diagnosis['jev_replay'] is False
    run.private_json(folder/'incomplete-output-diagnosis.private.json',diagnosis)
    assert subject.explicitly_superseded_incomplete(folder,items)
    assert (folder/'gemma-response.private.json').read_bytes()==original
    diagnosis['response_sha256']='changed'
    run.private_json(folder/'incomplete-output-diagnosis.private.json',diagnosis)
    with pytest.raises(run.ProviderBlocked):subject.explicitly_superseded_incomplete(folder,items)


def test_other_response_failures_cannot_be_waived_as_missing_output(tmp_path,monkeypatch):
    folder,items,plan,request,receipt=saved_batch(tmp_path,monkeypatch)
    receipt['http_status']=429
    run.private_json(folder/'gemma-response.private.json',receipt)
    with pytest.raises(run.ProviderBlocked):subject.incomplete_response_diagnosis(folder,items)
    assert not (folder/'incomplete-output-diagnosis.private.json').exists()


def test_schema_validation_rejection_requires_explicit_diagnosis_without_bill_invention(tmp_path,monkeypatch):
    folder,items,plan,request,receipt=saved_batch(tmp_path,monkeypatch)
    request['generationConfig']={'responseJsonSchema':{'type':'object'}}
    receipt={'http_status':400,'request_sha256':run.fingerprint(request),'body':{
        'error':{'code':400,'message':'Request contains an invalid argument.','status':'INVALID_ARGUMENT'}}}
    run.private_json(folder/'gemma-request.private.json',request)
    run.private_json(folder/'gemma-response.private.json',receipt)
    assert not subject.explicitly_superseded_schema_rejection(folder)
    diagnosis=subject.schema_rejection_diagnosis(folder)
    assert diagnosis['cost_usd'] is None and diagnosis['automatic_retry'] is False
    run.private_json(folder/'schema-rejection-diagnosis.private.json',diagnosis)
    assert subject.explicitly_superseded_schema_rejection(folder)
    receipt['http_status']=429
    run.private_json(folder/'gemma-response.private.json',receipt)
    with pytest.raises(run.ProviderBlocked):subject.explicitly_superseded_schema_rejection(folder)
