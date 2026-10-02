import json
from unittest.mock import MagicMock

from buenapro_worker.normalization.participation_repository import persist_participation
from buenapro_worker.normalization.facets import derive_summary


def test_persist_participation_requires_evidence_and_preserves_provenance():
    repo = MagicMock()
    raw = {"participation": {
        "consorcio": {"status": "permitted"},
        "subcontratacion": {"status": "prohibited", "clause": "No se permite subcontratar.", "page": 5},
    }}
    persist_participation(repo, opportunity_id="test-opportunity", raw=raw,
                          extraction_id=42, source="seace_prod4", document_sha256="verified-sha")
    sql, args = repo.conn.execute.call_args.args
    assert "UPDATE opportunities" in sql
    assert args[:2] == ("not_identified", "prohibited")
    payload = json.loads(args[2])
    assert payload["subcontratacion"]["page"] == 5
    assert payload["extraction_id"] == 42
    assert payload["document_sha256"] == "verified-sha"
    assert payload["source"] == "seace_prod4"
    assert args[3] == "test-opportunity"


def test_missing_legacy_identity_does_not_update_another_opportunity():
    repo = MagicMock()
    persist_participation(repo, opportunity_id=None, raw={}, extraction_id=1,
                          source="seace_prod6", document_sha256=None)
    repo.conn.execute.assert_not_called()


def test_summary_includes_participation_without_turning_it_into_bidder_requirements():
    summary = derive_summary({"participation": {
        "consorcio": {"status": "conditional", "clause": "Consorcio de hasta dos integrantes.",
                      "page": 7, "conditions": ["Hasta dos integrantes"]}
    }})
    assert summary["participation"]["consorcio"]["status"] == "conditional"
    assert summary["requirement_facets"] == []
