from copy import deepcopy
from pathlib import Path

import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2.batch_runner import private_write
from buenapro_worker.prod2 import storage_policy as subject

CODE = "00000000-0000-4000-8000-000000000001"
SOURCE = "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows
    def fetchall(self): return self.rows


class Connection:
    def __init__(self, row):
        self.row = row
        self.documents = [{"codigo_alfresco": CODE, "source_url": SOURCE}]
        self.download = {"bytes": 9, "format": "pdf"}
        self.active = 0
        self.statements = []
    def execute(self, statement, params):
        self.statements.append(statement)
        if "FROM prod2_document_extractions" in statement: return Cursor(self.row)
        if "FROM prod2_documents" in statement: return Cursor(self.documents)
        if "FROM prod2_document_downloads" in statement: return Cursor(self.download)
        if "count(*)" in statement: return Cursor({"count": self.active})
        raise AssertionError(statement)


@pytest.fixture
def completed(tmp_path):
    case = tmp_path / "123"
    case.mkdir()
    bundle = {"cache_key": "prod2:llm1:" + "a" * 64, "model": "synthetic",
              "raw_extraction_json": {"summary": {}, "source_document_manifest": [
                  {"document_uuid": CODE, "original_sha256": "d" * 64}]}, "document_set_hash": "b" * 64}
    outcome = {"document_storage_policy": subject.OFFICIAL_LINKS_POLICY,
        "status": "llm1_complete", "accepted": True, "convocation_id": 123,
        "extraction_id": 10, "llm1_evaluation_id": 20, "llm1_request_hash": "a" * 64,
        "llm1_receipt_hash": canonical_hash(bundle), "detail_hash": "c" * 64,
        "document_set_hash": "b" * 64, "facet_count": 4,
        "source_document_manifest": bundle["raw_extraction_json"]["source_document_manifest"],
        "documents": [{"document_uuid": CODE, "source_url": SOURCE}]}
    row = {"id_convocatoria": 123, "evaluation_id": 20, "evaluation_status": "done",
        "kind": "gemini_extraction", "request_hash": "a" * 64, "is_current": True,
        "quality": "auto", "detail_hash": "c" * 64, "document_inventory_complete": True,
        "result_json": bundle, "model": "synthetic", "raw_extraction_json": bundle["raw_extraction_json"],
        "facet_count": 4}
    private_write(case / "outcome.private.json", outcome)
    private_write(case / (CODE + ".download.private.json"), {"document_uuid": CODE,
        "sha256": "d" * 64, "bytes": 9, "format": "pdf"})
    return case, outcome, Connection(row)


def test_readback_checks_paid_result_and_references_without_r2_or_any_mutation(completed):
    case, outcome, conn = completed
    proof = subject.verify_completed_storage(conn, case, outcome)
    assert proof["pg_verified"] is True and proof["active_payments"] == 0
    assert proof["usage_unknown_attempts"] == 0 and proof["remote_cleanup_pending"] == 0
    assert all(statement.lstrip().startswith("SELECT") for statement in conn.statements)
    assert all("storage_key" not in statement for statement in conn.statements)


@pytest.mark.parametrize("key,value", [("evaluation_status", "uncertain"), ("request_hash", "e" * 64),
    ("evaluation_id", 99), ("id_convocatoria", 321), ("is_current", False), ("quality", "failed"),
    ("detail_hash", "e" * 64), ("document_inventory_complete", False), ("facet_count", 3),
    ("model", "changed"), ("raw_extraction_json", {"altered": True})])
def test_changed_persistence_retains_inputs(completed, key, value):
    case, outcome, conn = completed
    conn.row[key] = value
    with pytest.raises(ValueError): subject.verify_completed_storage(conn, case, outcome)


def test_receipt_payload_change_or_missing_reference_fails(completed):
    case, outcome, conn = completed
    conn.row["result_json"] = deepcopy(conn.row["result_json"])
    conn.row["result_json"]["extra"] = True
    with pytest.raises(ValueError): subject.verify_completed_storage(conn, case, outcome)
    del conn.row["result_json"]["extra"]
    conn.documents = []
    with pytest.raises(ValueError, match="references"): subject.verify_completed_storage(conn, case, outcome)


def test_download_revision_must_be_stored_and_completed_outcome_durable(completed):
    case, outcome, conn = completed
    conn.download["bytes"] = 8
    with pytest.raises(ValueError, match="download fact"): subject.verify_completed_storage(conn, case, outcome)
    conn.download["bytes"] = 9
    (case / "outcome.private.json").unlink()
    private_write(case / "outcome.private.json", {**outcome, "status": "needs_review"})
    with pytest.raises(ValueError, match="Durable outcome"): subject.verify_completed_storage(conn, case, outcome)


def test_last_upload_cleanup_receipt_supersedes_pending_state_and_binds_attempt(completed):
    case, outcome, conn = completed
    attempt = "gemini-selected-synthetic"
    private_write(case / (attempt + ".outcome.private.json"), {"attempt_id": attempt,
        "model_call_started": False, "remote_cleanup_pending": 1, "remote_cleanup_unknown": False})
    assert subject.verify_completed_storage(conn, case, outcome)["remote_cleanup_pending"] == 1
    private_write(case / (attempt + ".cleanup.private.json"), {"attempt_id": attempt,
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False})
    assert subject.verify_completed_storage(conn, case, outcome)["remote_cleanup_pending"] == 0
    (case / (attempt + ".cleanup.private.json")).unlink()
    private_write(case / (attempt + ".cleanup.private.json"), {"attempt_id": "other",
        "remote_cleanup_pending": 0, "remote_cleanup_unknown": False})
    with pytest.raises(ValueError, match="another attempt"): subject.verify_completed_storage(conn, case, outcome)


def test_active_payment_is_reported_for_cleanup_gate(completed):
    case, outcome, conn = completed
    conn.active = 1
    assert subject.verify_completed_storage(conn, case, outcome)["active_payments"] == 1


@pytest.mark.parametrize("value", [None, False, "unknown"])
def test_unknown_storage_policy_is_never_treated_as_legacy(value):
    assert subject.official_links_only({}) is False
    with pytest.raises(ValueError): subject.official_links_only({"document_storage_policy": value})
