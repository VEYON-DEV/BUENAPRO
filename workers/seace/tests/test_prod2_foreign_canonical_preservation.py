"""Explicit receipt replay saves a pending reading without replacing PROD4."""
from copy import deepcopy
from datetime import timedelta

import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import llm1_completion as subject
from test_prod2_llm1_completion import CODE, CONV, OPPORTUNITY, SHA, NOW, finish, fixture
from test_prod2_selected_completion import selected_fixture


def preservation_fixture(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    conn.canonical = {"consortium_status": "conditional", "subcontracting_status": "prohibited",
        "updated_at": NOW, "participation_terms_json": {"source": "seace_prod4", "extraction_id": 41,
            "document_sha256": SHA, "consorcio": {"status": "conditional", "page": 10},
            "subcontratacion": {"status": "prohibited", "page": 76}}}
    snapshot = subject.canonical_participation_snapshot(conn.canonical)
    proof = {"policy_version": subject.PRESERVE_FOREIGN_POLICY, "convocation_id": CONV,
        "opportunity_id": OPPORTUNITY, "evaluation_id": conn.reservation["id"],
        "request_hash": conn.reservation["request_hash"], "document_uuid": CODE,
        "document_sha256": SHA, "paid_result_hash": canonical_hash(conn.reservation["result_json"]),
        "canonical_snapshot": snapshot, "canonical_snapshot_hash": canonical_hash(snapshot)}
    return conn, extraction, proof


def test_default_foreign_guard_is_unchanged(monkeypatch):
    conn, extraction, proof = preservation_fixture(monkeypatch)
    with pytest.raises(subject.Llm1CompletionError, match="another source requires reconciliation"):
        finish(conn, extraction)
    assert conn.events == ["begin", "rollback"] and not conn.persist_calls and not conn.updates


def test_exact_opt_in_saves_reading_without_any_canonical_or_paid_mutation(monkeypatch):
    conn, extraction, proof = preservation_fixture(monkeypatch)
    before, paid, raw, expected_proof = map(deepcopy, (conn.canonical, conn.reservation, extraction, proof))
    saved = finish(conn, extraction, preserve_foreign_participation=proof)
    assert saved["projection_pending"] and saved["canonical_participation_preserved"]
    assert saved["requires_human_review"] and saved["extraction_id"] == 42
    assert saved["preservation_policy_version"] == subject.PRESERVE_FOREIGN_POLICY
    assert saved["preserved_canonical_snapshot_hash"] == proof["canonical_snapshot_hash"]
    assert len(conn.persist_calls) == 1 and not conn.updates
    assert conn.canonical == before and conn.reservation == paid and extraction == raw and proof == expected_proof
    assert conn.events == ["begin", "commit"]
    conn.current = {"evaluation_id": 7}
    assert finish(conn, extraction, preserve_foreign_participation=proof) == saved
    assert conn.canonical == before and not conn.updates


@pytest.mark.parametrize("key", ["policy_version", "convocation_id", "opportunity_id", "evaluation_id",
    "request_hash", "document_uuid", "document_sha256", "paid_result_hash", "canonical_snapshot_hash"])
def test_changed_binding_blocks_before_persistence(monkeypatch, key):
    conn, extraction, proof = preservation_fixture(monkeypatch)
    proof[key] = 999 if key in {"convocation_id", "evaluation_id"} else "invalid"
    with pytest.raises(subject.Llm1CompletionError):
        finish(conn, extraction, preserve_foreign_participation=proof)
    assert not conn.persist_calls and not conn.updates and conn.events[-1] == "rollback"


@pytest.mark.parametrize("mutation", ["extra", "missing", "bool_cid", "bool_owner", "notdict",
    "term", "consortium", "subcontracting", "timestamp", "source", "legacy_id", "legacy_sha",
    "review", "paid_raw", "revision", "unpaid"])
def test_unbound_or_changed_canonical_and_input_proofs_block(monkeypatch, mutation):
    conn, extraction, proof = preservation_fixture(monkeypatch)
    if mutation == "extra": proof["extra"] = True
    elif mutation == "missing": proof.pop("paid_result_hash")
    elif mutation == "bool_cid": proof["convocation_id"] = True
    elif mutation == "bool_owner": proof["evaluation_id"] = True
    elif mutation == "notdict": proof = []
    elif mutation == "term": conn.canonical["participation_terms_json"]["consorcio"]["page"] += 1
    elif mutation == "consortium": conn.canonical["consortium_status"] = "allowed"
    elif mutation == "subcontracting": conn.canonical["subcontracting_status"] = "allowed"
    elif mutation == "timestamp": conn.canonical["updated_at"] += timedelta(seconds=1)
    elif mutation == "source": conn.canonical["participation_terms_json"]["source"] = "seace_prod2"
    elif mutation == "legacy_id": conn.canonical["participation_terms_json"]["extraction_id"] = True
    elif mutation == "legacy_sha": conn.canonical["participation_terms_json"]["document_sha256"] = "f" * 64
    elif mutation == "review": extraction["requires_human_review"] = False
    elif mutation == "paid_raw": conn.reservation["result_json"]["raw_extraction_json"]["summary"] = {"changed": True}
    elif mutation == "revision": conn.document_revisions[CODE][0]["sha256_original"] = "f" * 64
    elif mutation == "unpaid": conn.reservation["status"] = "uncertain"
    with pytest.raises(subject.repository.Prod2PersistenceError):
        finish(conn, extraction, preserve_foreign_participation=proof)
    assert not conn.persist_calls and not conn.updates


def test_unselected_receipt_cannot_use_foreign_preservation(monkeypatch):
    conn, extraction, proof = preservation_fixture(monkeypatch)
    plain, normal = fixture(monkeypatch)
    plain.canonical = conn.canonical
    with pytest.raises(subject.Llm1CompletionError, match="preservation proof is invalid"):
        finish(plain, normal, preserve_foreign_participation=proof)
    assert not plain.persist_calls and not plain.updates


def test_canonical_write_during_extraction_forces_outer_rollback(monkeypatch):
    conn, extraction, proof = preservation_fixture(monkeypatch)
    persist = subject.repository.persist_extraction
    def changed(*args, **kwargs):
        saved = persist(*args, **kwargs)
        conn.canonical["participation_terms_json"]["consorcio"]["page"] = 99
        return saved
    monkeypatch.setattr(subject.repository, "persist_extraction", changed)
    with pytest.raises(subject.Llm1CompletionError, match="changed during"):
        finish(conn, extraction, preserve_foreign_participation=proof)
    assert conn.events[-1] == "rollback" and not conn.updates


@pytest.mark.parametrize("mutation", ["missing", "naive"])
def test_snapshot_must_include_timestamp_and_all_canonical_fields(monkeypatch, mutation):
    conn, extraction, proof = preservation_fixture(monkeypatch)
    if mutation == "missing": conn.canonical.pop("consortium_status")
    else: conn.canonical["updated_at"] = NOW.replace(tzinfo=None)
    with pytest.raises(subject.Llm1CompletionError, match="snapshot|timestamp"):
        finish(conn, extraction, preserve_foreign_participation=proof)
    assert not conn.persist_calls and not conn.updates
