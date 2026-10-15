"""Transport proof and journal gating only; no HTTP, PG or model calls."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import dinaut_jev_transport as subject
from match_provider_journal import ProviderBlocked, fingerprint, private_json


def payload():
    return {"model": "jev-1.13.0", "state": {"nested": {"ñ": [None, False, 0, "quoted \\\"\ntext"]}},
            "questions": {"r1": {"type": "choice", "instructions": "Use the same frozen state.",
                                  "criteria": {"yes": "Supported", "no": "Not supported"}}}}


def root_receipt(folder, *, status=400, body=None):
    value = payload()
    private_json(folder / "jev-request.private.json", value, exclusive=True)
    private_json(folder / "jev-started.private.json", {"request_sha256": fingerprint(value)}, exclusive=True)
    receipt = {"request_sha256": fingerprint(value), "http_status": status,
               "body": body if body is not None else {"detail": {"error_type": "max_tokens_exceeded"}}}
    private_json(folder / "jev-response.private.json", receipt, exclusive=True)
    return receipt


def journal_callback(calls):
    def callback(folder, stage, wire, **kwargs):
        calls.append({"folder": folder, "stage": stage, "wire": copy.deepcopy(wire), "kwargs": kwargs})
        private_json(folder / "jev-request.private.json", wire, exclusive=True)
        private_json(folder / "jev-started.private.json", {"request_sha256": fingerprint(wire)}, exclusive=True)
        receipt = {"http_status": 200, "request_sha256": fingerprint(wire),
                   "body": {"model": wire["model"], "answers": {}, "usage": {"input_tokens": 1}}}
        private_json(folder / "jev-response.private.json", receipt, exclusive=True)
        return receipt
    return callback


def test_roundtrip_preserves_state_model_questions_and_original_input():
    original = payload(); before = copy.deepcopy(original)
    wire = subject.compact_jev_payload(original)
    assert isinstance(wire["state"], str)
    assert json.loads(wire["state"]) == original["state"]
    assert wire["model"] == original["model"] and wire["questions"] == original["questions"]
    assert original == before
    assert "\n" not in wire["state"]


def test_root_200_is_reused_exactly_without_child_files_or_dispatch(tmp_path):
    receipt = root_receipt(tmp_path, status=200, body={"usage": {"input_tokens": 100}})
    def no_dispatch(*args, **kwargs):
        pytest.fail("Already paid root success must not be sent again")
    assert subject.dispatch_jev_transport(tmp_path, payload(), reserved_dispatch=no_dispatch) == receipt
    assert subject.get_cached_jev_receipt(tmp_path) == receipt
    assert not (tmp_path / subject.CHILD_FOLDER).exists()


def test_exact_context_400_archived_and_one_lossless_child_call_reused(tmp_path):
    rejected = root_receipt(tmp_path)
    original_bytes = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    calls = []
    result = subject.dispatch_jev_transport(tmp_path, payload(), reserved_dispatch=journal_callback(calls), token="reserved")
    assert len(calls) == 1 and calls[0]["folder"] == tmp_path / subject.CHILD_FOLDER
    assert calls[0]["stage"] == "jev" and calls[0]["kwargs"] == {"token": "reserved"}
    manifest = json.loads((tmp_path / subject.CHILD_FOLDER / subject.MANIFEST_FILENAME).read_text())
    assert manifest["source_payload_sha256"] == fingerprint(payload())
    assert manifest["wire_payload_sha256"] == fingerprint(calls[0]["wire"])
    assert manifest["root_rejection_receipt_sha256"] == fingerprint(rejected)
    assert manifest["long_context"] is False and manifest["split"] is False
    assert manifest["raw_rejection_cost_inference"] == "not_assumed_zero"
    assert subject.get_cached_jev_receipt(tmp_path, payload()) == result
    assert subject.dispatch_jev_transport(tmp_path, payload(), reserved_dispatch=journal_callback(calls)) == result
    assert len(calls) == 1
    assert {name: (tmp_path / name).read_bytes() for name in original_bytes} == original_bytes


def test_unsent_request_uses_child_only_and_manifest_resolves_without_root_attempt(tmp_path):
    calls = []
    result = subject.dispatch_jev_transport(tmp_path, payload(), reserved_dispatch=journal_callback(calls))
    assert result["http_status"] == 200
    assert not (tmp_path / "jev-request.private.json").exists()
    assert subject.get_cached_jev_receipt(tmp_path) == result
    manifest = json.loads((tmp_path / subject.CHILD_FOLDER / subject.MANIFEST_FILENAME).read_text())
    assert manifest["root_disposition"] == "unsent" and manifest["root_rejection_receipt_sha256"] is None


def test_sent_uncertain_root_blocks_new_representation_before_any_mutation(tmp_path):
    private_json(tmp_path / "jev-request.private.json", payload())
    private_json(tmp_path / "jev-started.private.json", {"request_sha256": fingerprint(payload())})
    with pytest.raises(ProviderBlocked, match="Unresolved sent root"):
        subject.prepare_jev_transport(tmp_path, payload())
    assert not (tmp_path / subject.CHILD_FOLDER).exists()


@pytest.mark.parametrize("status,body", [
    (429, {"detail": {"error_type": "max_tokens_exceeded"}}),
    (404, {"detail": {"error_type": "max_tokens_exceeded"}}),
    (400, {"detail": {"error_type": "other"}}),
    (400, {"detail": {"error_type": "max_tokens_exceeded"}, "answers": {}}),
    (400, {"detail": {"error_type": "max_tokens_exceeded"}, "usage": {}}),
    (400, {"detail": {"error_type": "max_tokens_exceeded", "usage": {}}}),
    (400, "max_tokens_exceeded"),
])
def test_other_rejections_or_extended_bodies_never_authorize_transport(tmp_path, status, body):
    root_receipt(tmp_path, status=status, body=body)
    with pytest.raises(ProviderBlocked, match="not the authorized exact"):
        subject.prepare_jev_transport(tmp_path, payload())
    assert not (tmp_path / subject.CHILD_FOLDER).exists()


def test_root_hash_mismatch_blocks_before_dispatch(tmp_path):
    root_receipt(tmp_path)
    changed = payload(); changed["state"]["nested"]["ñ"].append("changed")
    with pytest.raises(ProviderBlocked, match="binding changed"):
        subject.prepare_jev_transport(tmp_path, changed)


def test_child_sent_uncertainty_or_rejection_is_never_retried(tmp_path):
    prepared = subject.prepare_jev_transport(tmp_path, payload())
    child = prepared["folder"]
    private_json(child / "jev-request.private.json", prepared["payload"])
    private_json(child / "jev-started.private.json", {"request_sha256": fingerprint(prepared["payload"])})
    with pytest.raises(ProviderBlocked, match="Unresolved compact"):
        subject.prepare_jev_transport(tmp_path, payload())
    private_json(child / "jev-response.private.json", {"request_sha256": fingerprint(prepared["payload"]),
                                                       "http_status": 400, "body": {"detail": "rejected"}})
    with pytest.raises(ProviderBlocked, match="Saved compact JEV rejection"):
        subject.get_cached_jev_receipt(tmp_path)


@pytest.mark.parametrize("filename", [subject.MANIFEST_FILENAME, subject.WIRE_FILENAME, subject.SOURCE_FILENAME])
def test_corrupt_semantic_bindings_block_reuse(tmp_path, filename):
    calls = []
    subject.dispatch_jev_transport(tmp_path, payload(), reserved_dispatch=journal_callback(calls))
    path = tmp_path / subject.CHILD_FOLDER / filename
    value = json.loads(path.read_text()); value["tampered"] = True; private_json(path, value)
    with pytest.raises(ProviderBlocked):
        subject.get_cached_jev_receipt(tmp_path, payload())
    assert len(calls) == 1


def test_changed_archived_diagnostic_blocks_child_receipt_reuse(tmp_path):
    root_receipt(tmp_path); calls = []
    subject.dispatch_jev_transport(tmp_path, payload(), reserved_dispatch=journal_callback(calls))
    path = tmp_path / "jev-response.private.json"
    value = json.loads(path.read_text()); value["seconds"] = 99; private_json(path, value)
    with pytest.raises(ProviderBlocked, match="root diagnostic changed"):
        subject.get_cached_jev_receipt(tmp_path)


def test_child_source_is_bound_to_archived_root_even_without_expected_argument(tmp_path):
    root_receipt(tmp_path); calls = []
    subject.dispatch_jev_transport(tmp_path, payload(), reserved_dispatch=journal_callback(calls))
    child = tmp_path / subject.CHILD_FOLDER
    changed = payload(); changed["state"] = {"silently_changed": True}
    private_json(child / subject.SOURCE_FILENAME, changed)
    with pytest.raises(ProviderBlocked, match="different frozen source"):
        subject.get_cached_jev_receipt(tmp_path)


def test_metrics_are_local_proxy_with_no_dispatch(tmp_path):
    metrics = subject.compact_transport_metrics(payload(), token_counter=lambda text: len(text))
    assert metrics["semantic_roundtrip_equal"] and metrics["model_calls"] == 0
    assert metrics["long_context"] is False
    assert metrics["source_payload_sha256"] == fingerprint(payload())
    assert list(tmp_path.iterdir()) == []
