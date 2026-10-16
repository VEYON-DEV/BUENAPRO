"""Exact source citations can be reclassified locally; no inference or PG."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import dinaut_source_citation_refs as subject
import run_dinaut_three_dot_matches as run
import run_dinaut_three_dot_gemma_batches as batches


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    monkeypatch.setattr(batches.requests, "post", lambda *a, **k: pytest.fail("No HTTP"))
    monkeypatch.setattr(run.psycopg, "connect", lambda *a, **k: pytest.fail("No PG"))
    monkeypatch.setattr(run, "production_settings", lambda *a, **k: pytest.fail("No SSH"))
    monkeypatch.setattr(run, "reserved_dispatch", lambda *a, **k: pytest.fail("No inference"))


def payload():
    return {"state": {"requirements": [
        {"source_index": 1, "detalle": {"evidencia": "Pág. 14, numeral 21.A"}},
        {"source_index": 3, "detalle": {"evidencia": "Pág. 10, numeral 9"}}],
        "profile": {"documents": [{"ref": "D1"}], "professionals": [{"ref": "P1"}]}}}


def recommendation():
    return {"resumen": "Experiencia por acreditar.", "requisitos": [
        {"source_index": 1, "evidencia": "Pág. 14, numeral 21.A", "brecha": "Monto pendiente.",
         "accion": "Contrastar el contrato.", "evidence_refs": ["D1", "Pág. 14, numeral 21.A", "P1", "D1"]},
        {"source_index": 3, "evidencia": "Pág. 10, numeral 9", "brecha": "Autorización pendiente.",
         "accion": "Verificar la autorización.", "evidence_refs": ["Pág. 10, numeral 9"]}],
        "acciones_recomendadas": ["Contrastar las condiciones concretas."]}


def test_exact_same_requirement_citation_removed_without_mutating_any_other_field():
    original = recommendation(); frozen = payload()
    before = deepcopy((original, frozen))
    result, notes = subject.normalize_requirement_source_refs(original, frozen)
    expected = deepcopy(original)
    expected["requisitos"][0]["evidence_refs"] = ["D1", "P1", "D1"]
    expected["requisitos"][1]["evidence_refs"] = []
    assert result == expected
    assert (original, frozen) == before
    assert [note["source_index"] for note in notes] == [1, 3]
    assert all(note["evidence_text_unchanged"] and note["source_citation_is_not_company_evidence"] for note in notes)
    assert notes[0]["retained_company_refs"] == ["D1", "P1", "D1"]
    assert run.validate_recommendations(result, frozen) == result


@pytest.mark.parametrize("unknown", ["Pág. 10, numeral 9", "Pág. 14, numeral 21.A ",
    "pág. 14, numeral 21.A", "D14", "P2", "arbitrary", ""])
def test_other_requirement_fuzzy_or_arbitrary_unknown_refs_still_block(unknown):
    value = recommendation(); value["requisitos"][0]["evidence_refs"].append(unknown)
    before = deepcopy(value)
    with pytest.raises(ValueError, match="unknown documentary"):
        subject.normalize_requirement_source_refs(value, payload())
    assert value == before


@pytest.mark.parametrize("indices", [[1], [1, 1], [3, 1], [True, 3], ["1", 3], [1, 3, 4]])
def test_missing_duplicate_changed_or_coerced_row_identity_cannot_be_repaired(indices):
    value = recommendation(); rows = value["requisitos"]
    value["requisitos"] = [{**deepcopy(rows[min(index, 1)]), "source_index": source_index}
                           for index, source_index in enumerate(indices)]
    with pytest.raises(ValueError, match="coverage/order"):
        subject.normalize_requirement_source_refs(value, payload())


@pytest.mark.parametrize("citation", [None, "", 14, {}])
def test_unknown_reference_requires_nonempty_string_frozen_citation(citation):
    frozen = payload(); frozen["state"]["requirements"][0]["detalle"]["evidencia"] = citation
    with pytest.raises(ValueError, match="unknown documentary"):
        subject.normalize_requirement_source_refs(recommendation(), frozen)


def test_no_changes_means_no_proof_and_known_dp_aliases_always_stay():
    value = recommendation()
    for row in value["requisitos"]:
        row["evidence_refs"] = ["P1", "D1"]
    result, notes = subject.normalize_requirement_source_refs(value, payload())
    assert result == value and notes == []
    assert subject.source_ref_decoder_proof(request_sha256="r", response_sha256="s",
        jev_requests={}, recommendations={}, notes=notes) is None


def saved_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(batches, "FOLDER", tmp_path)
    keys = ["seace_prod2-19", "seace_prod6-20"]
    items = {key: {"case": {"source_key": key.rsplit("-", 1)[0], "source_id": int(key.rsplit("-", 1)[1])},
                   "jev_payload": payload()} for key in keys}
    request = {"contents": [{"parts": [{"text": "Frozen shared request"}]}]}
    raw = {"evaluations": [{"case_key": key, **recommendation()} for key in keys]}
    # This case is clean already: its old binding and decode must not change.
    for row in raw["evaluations"][0]["requisitos"]:
        row["evidence_refs"] = ["D1"]
    receipt = {"http_status": 200, "request_sha256": run.fingerprint(request), "body": {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(raw)}]}}],
        "usageMetadata": {"promptTokenCount": 101, "candidatesTokenCount": 33, "thoughtsTokenCount": 5}}}
    plan = {"case_keys": keys, "request_sha256": run.fingerprint(request),
            "jev_requests": {key: run.fingerprint(item["jev_payload"]) for key, item in items.items()}}
    folder = tmp_path / "gemma-batches/batch-example"
    for name, value in (("plan.private.json", plan), ("gemma-request.private.json", request), ("gemma-response.private.json", receipt)):
        run.private_json(folder / name, value)
    return folder, items, plan, request, receipt


def test_scope2_both_cached_decoders_agree_proof_is_bound_and_raw_usage_untouched(tmp_path, monkeypatch):
    folder, items, plan, _request, receipt = saved_batch(tmp_path, monkeypatch)
    raw_bytes = {path.name: path.read_bytes() for path in folder.iterdir()}
    payloads = {key: item["jev_payload"] for key, item in items.items()}
    with run.execution_scope(run.TWO_DOT_50_SCOPE):
        result, usage, notes = batches.recommendations_with_decoder_notes(receipt, payloads)
        assert usage == receipt["body"]["usageMetadata"]
        assert len(notes) == 2
        assert batches.bind_saved_batch(folder, items) == usage
        proof_path = folder / subject.PROOF_FILENAME
        proof = json.loads(proof_path.read_text())
        assert proof["decoder_version"] == subject.DECODER_VERSION
        assert proof["request_sha256"] == receipt["request_sha256"]
        assert proof["response_sha256"] == run.fingerprint(receipt)
        assert proof["normalized_recommendations_sha256"] == run.fingerprint(result)
        assert proof["jev_requests"] == plan["jev_requests"]
        original_bindings = {key: (tmp_path / key / "gemma-batch-binding.private.json").read_bytes() for key in items}
        totals = {name: 0 for name in usage}
        for key, item in items.items():
            decoded, allocated = run.saved_batch_recommendation(tmp_path / key, item)
            assert decoded == result[key]
            for name in totals:
                totals[name] += allocated[name]
        assert totals == usage
        assert batches.bind_saved_batch(folder, items) == usage
        assert original_bindings == {key: (tmp_path / key / "gemma-batch-binding.private.json").read_bytes() for key in items}
        assert proof_path.stat().st_mode & 0o777 == 0o600
        proof["normalizations"][0]["source_requirement_citation"] = "Changed source citation"
        run.private_json(proof_path, proof)
        with pytest.raises(ValueError, match="proof"):
            run.saved_batch_recommendation(tmp_path / plan["case_keys"][1], items[plan["case_keys"][1]])
        with pytest.raises(run.PublicationConflict, match="Immutable"):
            batches.bind_saved_batch(folder, items)
    assert raw_bytes == {name: (folder / name).read_bytes() for name in raw_bytes}


def test_corrected_saved_case_requires_proof_and_default33_still_rejects(tmp_path, monkeypatch):
    folder, items, plan, _request, receipt = saved_batch(tmp_path, monkeypatch)
    payloads = {key: item["jev_payload"] for key, item in items.items()}
    assert run.ACTIVE_SCOPE == run.THREE_DOT_SCOPE
    with pytest.raises(ValueError, match="unknown documentary"):
        batches.recommendations_from_receipt(receipt, payloads)
    assert not (folder / subject.PROOF_FILENAME).exists()
    with run.execution_scope(run.TWO_DOT_50_SCOPE):
        batches.bind_saved_batch(folder, items)
        (folder / subject.PROOF_FILENAME).unlink()
        key = plan["case_keys"][1]
        with pytest.raises(run.PublicationConflict, match="decoder proof"):
            run.saved_batch_recommendation(tmp_path / key, items[key])
        assert "scripts/dinaut_source_citation_refs.py" in run.scoped_runtime_paths()
    with pytest.raises(ValueError, match="unknown documentary"):
        run.saved_batch_recommendation(tmp_path / key, items[key])


@pytest.mark.parametrize("field,changed", [("request_sha256", "wrong"), ("response_sha256", "wrong"),
    ("jev_requests", {}), ("normalized_case_sha256", {}), ("decoder_version", "other"),
    ("score_or_status_changed", True), ("raw_response_preserved", False), ("new_inference_calls", 1)])
def test_proof_cannot_bind_a_different_source_receipt_or_derived_output(field, changed):
    result, notes = subject.normalize_requirement_source_refs(recommendation(), payload())
    proof = subject.source_ref_decoder_proof(request_sha256="request", response_sha256="response",
        jev_requests={"case": "jev"}, recommendations={"case": result},
        notes=[{"case_key": "case", **note} for note in notes])
    proof[field] = changed
    with pytest.raises(ValueError, match="proof"):
        subject.verify_source_ref_case_proof(proof, case_key="case", request_sha256="request",
            response_sha256="response", jev_request_sha256="jev", recommendation=result, notes=notes)
