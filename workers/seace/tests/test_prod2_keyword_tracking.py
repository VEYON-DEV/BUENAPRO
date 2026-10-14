from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from buenapro_worker.prod2.business_batches import select_keyword_preset
from buenapro_worker.prod2.keyword_tracking import progress_summary, summarize_result, validated_query, scope_for, identifier

ROOT = Path(__file__).resolve().parents[3]
QUERY = {"keyword": "software", "object_type": "service", "year": 2026,
    "published_from": "2026-04-06", "published_to": "2026-10-06", "seace_version": "3"}


def task(state="pending", *, query=None, attempts=0, result=None):
    return {"query_manifest": query or QUERY, "status": state, "attempts": attempts, "result_json": result}


def result(**overrides):
    return {"query": QUERY, "status": "complete", "coverage_complete": True, "source_atomic_snapshot": False,
        "errors": [], "coverage_kind": "observed_pass", "pages_processed": 2, "http_searches": 2,
        "rows_offered": 3, "leaf_windows": [{"observed_at": "2026-10-06T12:00:00+00:00"}], **overrides}


def test_preset_is_exactly_thirty_from_four_business_lines_no_silent_alias_expansion():
    files = ["prod2-keyword-presets.v1.json", "prod2-business-batches.v1.json", "prod2-convenio-keywords.v3.json"]
    selected = select_keyword_preset(*(json.loads((ROOT / "docs/sources" / name).read_text()) for name in files), "digitales30")
    assert len(selected["keywords"]) == len(set(selected["keywords"])) == 30
    assert [row["search_keyword_count"] for row in selected["business_plan"]["batches"]] == [12, 8, 6, 4]
    assert selected["keyword_preset"]["keyword_count"] == 30
    assert "agentes ia" in selected["keywords"] and "agentes de ia" in selected["keywords"]
    assert "software" in selected["keywords"]
    assert not {"ia", "soporte", "sistema", "datos", "automatización"} & set(selected["keywords"])


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "stale_source", "stale_business", "unknown_id"])
def test_preset_rejects_invalid_or_stale_subsets(mutation):
    files = ["prod2-keyword-presets.v1.json", "prod2-business-batches.v1.json", "prod2-convenio-keywords.v3.json"]
    presets, config, inventory = [json.loads((ROOT / "docs/sources" / name).read_text()) for name in files]
    preset_id = "digitales30"
    if mutation == "duplicate":
        presets["presets"][preset_id]["keywords"].append("software")
    elif mutation == "unknown":
        presets["presets"][preset_id]["keywords"].append("not_configured")
    elif mutation == "stale_source":
        inventory["keyword_snapshot_date"] = "2026-10-07"
    elif mutation == "stale_business":
        config["batches"][0]["label"] = "Changed label"
    else:
        preset_id = "unknown"
    with pytest.raises(ValueError):
        select_keyword_preset(presets, config, inventory, preset_id)


def test_used_does_not_mean_complete_and_unknown_legacy_history_is_not_unused():
    rows = [task("complete", attempts=1), task("pending", query={**QUERY, "object_type": "good"})]
    progress = progress_summary(rows, ["software", "chatbot"])
    assert progress[0]["used"] and progress[0]["status"] == "partial"
    assert progress[0]["queries_complete"] == 1 and progress[0]["queries_planned"] == 2
    assert progress[1]["status"] == "pending" and not progress[1]["tracking_registered"]
    assert progress_summary([task("complete", attempts=1)], ["software"])[0]["status"] == "complete"
    assert progress_summary([task("running", attempts=1)], ["software"])[0]["status"] == "running"
    assert progress_summary([task("blocked", attempts=1)], ["software"])[0]["status"] == "blocked"


def test_every_year_and_object_is_visible_in_progress():
    rows = [task(query={**QUERY, "year": year, "object_type": kind})
        for year in [2026, 2025] for kind in ["good", "service"]]
    progress = progress_summary(rows, ["software"])[0]
    assert progress["queries_planned"] == 4 and progress["queries_complete"] == 0
    assert {(row["year"], row["object_type"]) for row in progress["queries"]} == {
        (2026, "good"), (2026, "service"), (2025, "good"), (2025, "service")}


def test_replayed_complete_result_keeps_original_source_observation_not_now():
    replay = result(http_searches=0, cached_pages_reemitted=2, ended_at=datetime.now(timezone.utc).isoformat())
    summary = summarize_result(QUERY, replay)
    assert summary["replayed_observation"]
    assert summary["source_observed_at"] == "2026-10-06T12:00:00+00:00"
    assert "ended_at" not in summary
    partial = summarize_result(QUERY, result(status="stopped", coverage_complete=False, errors=[{"code": "stop_requested"}]))
    assert partial["status"] == "partial" and not partial["coverage_complete"]


@pytest.mark.parametrize("overrides", [
    {"query": {**QUERY, "keyword": "chatbot"}}, {"coverage_complete": 1},
    {"status": "partial"}, {"source_atomic_snapshot": True}, {"errors": [{"code": "source_403"}]},
    {"pages_processed": True}, {"http_pages": -1},
])
def test_unbound_partial_or_invalid_results_cannot_claim_completion(overrides):
    with pytest.raises(ValueError):
        summarize_result(QUERY, result(**overrides))


def test_epoch_scope_freezes_dates_not_candidate_profile_or_keyword_subset():
    manifest = {"published_from": "2026-04-06", "published_to": "2026-10-06", "keywords": ["software"], "resolved_years": [2026]}
    assert scope_for(manifest) == scope_for({**manifest, "keywords": ["software", "chatbot"], "resolved_years": [2026, 2025]})
    assert scope_for(manifest) != scope_for({**manifest, "published_to": "2026-10-07"})
    assert validated_query(QUERY) == QUERY
    with pytest.raises(ValueError):
        validated_query({**QUERY, "keyword": "SOFTWARE"})
    with pytest.raises(ValueError):
        identifier("../escape")
