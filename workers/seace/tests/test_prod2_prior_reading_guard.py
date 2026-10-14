"""Cross-batch payment preservation uses only attributable PostgreSQL evidence."""
import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2.prior_reading_guard import (
    POLICY, preserve_prior_reading, validate_preservation_policy,
)

CONV = 1047375
PROFILE = "00000000-0000-4000-8000-000000000099"
DOCUMENT = "00000000-0000-4000-8000-000000000001"
OPPORTUNITY = "00000000-0000-4000-8000-000000000002"


class Connection:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))
        self.queries = []

    def execute(self, sql, params):
        assert sql.lstrip().startswith("SELECT")
        assert "FOR UPDATE" not in sql.upper()
        self.queries.append((sql, params))
        return SimpleNamespace(fetchall=lambda: copy.deepcopy(self.rows))


def prior(status="done", *, evaluation=7):
    raw = {"summary": {"descripcion_corta": "Lectura histórica conservada"}}
    bundle = {"cache_key": "prod2:llm1:" + "a" * 64,
        "original_sha256": "b" * 64, "analysis_sha256": "c" * 64,
        "document_uuid": DOCUMENT, "document_sha256": "b" * 64,
        "doc_class": "tdr", "model": "historical-model",
        "prompt_version": "historical-prompt", "schema_version": "historical-schema",
        "prompt_hash": "d" * 64, "raw_extraction_json": raw,
        "requires_human_review": True, "input_tokens": 100, "output_tokens": 40,
        "cost_usd": 0.01}
    return {"evaluation_id": evaluation, "id_convocatoria": CONV, "profile_id": PROFILE,
        "kind": "gemini_extraction", "status": status, "request_hash": "a" * 64,
        "reserved_model": "historical-model", "policy_version": "prod2_document_extraction_v1",
        "completed_at": datetime(2026, 10, 7, tzinfo=timezone.utc), "result_json": bundle,
        "extraction_id": evaluation + 10, "extraction_convocation_id": CONV,
        "extraction_evaluation_id": evaluation, "opportunity_id": OPPORTUNITY,
        "document_uuid": DOCUMENT, "sha256_original": "b" * 64,
        "is_current": True, "quality": "auto", "extraction_model": "historical-model",
        "extraction_prompt_version": "historical-prompt",
        "extraction_schema_version": "historical-schema", "requires_human_review": True,
        "raw_extraction_json": copy.deepcopy(raw), "facet_count": 3,
        "is_listed": True, "source_url":
            "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + DOCUMENT,
        "download_sha256": "b" * 64, "download_bytes": 1234,
        "canonical_opportunity_id": OPPORTUNITY, "canonical_opportunity_exists": OPPORTUNITY}


def call(conn, **kwargs):
    return preserve_prior_reading(conn, convocation_id=CONV, profile_id=PROFILE,
        policy=POLICY, **kwargs)


def test_absent_policy_preserves_old_behavior_without_query():
    conn = Connection([prior()])
    assert preserve_prior_reading(conn, convocation_id=CONV, profile_id=PROFILE) is None
    assert conn.queries == []


@pytest.mark.parametrize("policy", [True, False, {}, "", "v1", POLICY + "-changed"])
def test_invalid_policy_rejected_before_any_query(policy):
    conn = Connection()
    with pytest.raises(ValueError, match="preservation policy"):
        preserve_prior_reading(conn, convocation_id=CONV, profile_id=PROFILE, policy=policy)
    assert conn.queries == []


def test_novel_candidate_and_profile_can_progress():
    conn = Connection()
    assert call(conn) is None
    assert len(conn.queries) == 1
    sql, params = conn.queries[0]
    assert params == (CONV, PROFILE)
    assert "WHERE ev.id_convocatoria=%s AND ev.profile_id=%s AND ev.kind='gemini_extraction'" in sql
    assert "ex.evaluation_id=ev.id AND ex.id_convocatoria=ev.id_convocatoria" in sql
    assert "dl.sha256_original=ex.sha256_original" in sql


def test_done_preserves_canonical_reading_without_policy_equivalence_or_payload():
    conn = Connection([prior()])
    original = copy.deepcopy(conn.rows)
    result = call(conn)
    assert result["status"] == "llm1_complete"
    assert result["llm1_prior_preserved"] is True
    assert result["llm1_cost_usd"] == 0
    assert result["llm1_evaluation_id"] == 7 and result["extraction_id"] == 17
    assert result["llm1_model"] == "historical-model"
    assert result["requires_human_review"] is True
    evidence = result["prior_reading_preserved"]
    assert evidence["new_reading_policy_equivalence_claimed"] is False
    assert evidence["provider_calls"] == evidence["source_calls"] == evidence["business_writes"] == 0
    assert evidence["canonical"]["schema_version"] == "historical-schema"
    assert evidence["canonical"]["prompt_version"] == "historical-prompt"
    assert "raw_extraction_json" not in json.dumps(result)
    assert "result_json" not in json.dumps(result)
    assert conn.rows == original


def test_review_flag_false_is_preserved_exactly():
    row = prior()
    row["requires_human_review"] = row["result_json"]["requires_human_review"] = False
    assert call(Connection([row]))["requires_human_review"] is False


@pytest.mark.parametrize("quality", ["reviewed", "corrected"])
def test_reviewed_canonical_quality_is_preserved(quality):
    row = prior()
    row["quality"] = quality
    assert call(Connection([row]))["status"] == "llm1_complete"


@pytest.mark.parametrize("status", ["pending", "running", "uncertain", "failed"])
def test_prior_non_done_read_is_preserved_without_repayment(status):
    row = prior(status)
    row.update(extraction_id=None, is_current=None, result_json=None)
    result = call(Connection([row]))
    assert result["status"] == "needs_review"
    assert result["llm1_prior_preserved"] is True and result["llm1_cost_usd"] == 0
    assert result["prior_reading_preserved"]["prior_reservations"][0]["status"] == status
    assert "llm1_evaluation_id" not in result


def test_valid_current_done_outranks_old_uncertain_and_failed():
    current = prior(evaluation=9)
    rows = [prior("uncertain", evaluation=7), prior("failed", evaluation=8), current]
    result = call(Connection(rows))
    assert result["status"] == "llm1_complete" and result["llm1_evaluation_id"] == 9
    assert len(result["prior_reading_preserved"]["prior_reservations"]) == 3


@pytest.mark.parametrize("field,value", [
    ("extraction_id", None), ("extraction_evaluation_id", 111),
    ("extraction_convocation_id", CONV + 1), ("profile_id", OPPORTUNITY),
    ("is_current", False), ("is_listed", False), ("quality", "failed"),
    ("quality", "unknown"), ("raw_extraction_json", {}),
    ("requires_human_review", None), ("facet_count", -1),
    ("opportunity_id", DOCUMENT), ("canonical_opportunity_exists", None),
    ("document_uuid", OPPORTUNITY), ("sha256_original", "e" * 64),
    ("download_sha256", "e" * 64), ("download_bytes", None),
    ("reserved_model", "different-model"), ("extraction_model", "different-model"),
    ("extraction_prompt_version", "different-prompt"),
    ("extraction_schema_version", "different-schema"),
    ("completed_at", datetime(2026, 10, 7)), ("source_url", "https://example.org/document"),
    ("source_url", "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + DOCUMENT + "&ticket=secret"),
])
def test_incomplete_or_corrupt_done_never_authorizes_repayment(field, value):
    row = prior()
    row[field] = value
    result = call(Connection([row]))
    assert result["status"] == "needs_review"
    assert result["prior_reading_preserved"]["reason"] == "prior_reading_requires_review"
    assert result["requires_human_review"] is True


@pytest.mark.parametrize("field,value", [
    ("raw_extraction_json", {}), ("cache_key", "prod2:llm1:" + "z" * 64),
    ("document_uuid", OPPORTUNITY), ("requires_human_review", False),
    ("requires_human_review", 1),
    ("analysis_sha256", None), ("prompt_hash", None), ("input_tokens", True),
    ("output_tokens", -1), ("cost_usd", float("nan")), ("doc_class", "unsupported"),
])
def test_malformed_paid_receipt_keeps_review_work_visible(field, value):
    row = prior()
    row["result_json"][field] = value
    result = call(Connection([row]))
    assert result["status"] == "needs_review"
    assert result["llm1_cost_usd"] == 0


def test_structurally_invalid_raw_is_not_accepted_when_json_matches():
    row = prior()
    raw = {"requirements": {"key_personnel": "invalid"}}
    row["raw_extraction_json"] = row["result_json"]["raw_extraction_json"] = raw
    assert call(Connection([row]))["status"] == "needs_review"


def test_historical_done_without_current_extraction_is_pending():
    row = prior()
    row.update(extraction_id=None, is_current=None, raw_extraction_json=None)
    assert call(Connection([row]))["status"] == "needs_review"


def test_ambiguous_current_rows_fail_closed():
    result = call(Connection([prior(evaluation=7), prior(evaluation=8)]))
    assert result["status"] == "needs_review"
    assert result["prior_reading_preserved"]["reason"] == "ambiguous_current_canonical_reading"


def test_exact_policy_version_validation():
    assert validate_preservation_policy(POLICY) == POLICY
    assert validate_preservation_policy(None) is None
