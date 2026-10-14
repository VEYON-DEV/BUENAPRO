from datetime import datetime, timezone
from types import SimpleNamespace

from buenapro_worker.prod2.keyword_history import historical_summary, read_history


def test_historical_used_is_distinct_from_current_scope_complete_or_never_used():
    query = {"keyword": "SOFTWARE", "year": 2026, "object_type": "good",
        "published_from": "2026-10-01", "published_to": "2026-10-05"}
    result = historical_summary([{"query": query, "status": "complete", "passes": 1}],
        [{"query": query | {"object_type": "service"}, "observations": 16}], ["software", "chatbot"])
    assert result[0]["historical_evidence"] is True
    assert result[0]["source_passes"][0]["query"]["published_from"] == "2026-10-01"
    assert result[0]["accepted_samples_prove_full_query_coverage"] is False
    assert result[1]["historical_evidence"] is False
    assert result[1]["missing_evidence_means"] == "unknown_not_proof_never_used"


def test_read_history_uses_only_selects_and_no_candidate_payloads():
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    class Connection:
        def execute(self, sql, params=None):
            assert sql.lstrip().startswith("SELECT")
            if "to_regclass" in sql:
                return SimpleNamespace(fetchone=lambda: {"discovery": True, "accepted": True})
            assert params == (["software"],)
            assert "row_manifest" not in sql and "result_json" not in sql
            if "JOIN" in sql:
                return SimpleNamespace(fetchall=lambda: [{"query": {"keyword": "software"},
                    "status": "complete", "passes": 1, "last_observed_at": now}])
            return SimpleNamespace(fetchall=lambda: [{"query": {"keyword": "software"},
                "observations": 14, "last_observed_at": now}])
    result = read_history(Connection(), ["software"])[0]
    assert result["source_passes"][0]["last_observed_at"] == now.isoformat()
    assert result["accepted_observations"][0]["observations"] == 14
