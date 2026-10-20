from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from buenapro_worker.prod2 import handoff_selection as subject
from buenapro_worker.prod2.batch_plan import digest
from buenapro_worker.prod2.discovery import validate_capture
from buenapro_worker.prod2.discovery_registry import normalize_entry


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return subject.file_sha(path)


@pytest.fixture
def package(tmp_path):
    query = {"year": 2026, "keyword": "tablero", "object_type": "good", "seace_version": "3",
        "published_from": "2026-10-04", "published_to": "2026-10-20"}
    raw = {"query": query, "observed_at": "2026-10-20T08:00:00+00:00",
        "page": {"number": 1, "page_size": 15, "total_results": 1, "total_pages": 1},
        "rows": [{"convocation_id": 123, "procedure_id": 456, "nomenclature": "LP-1",
            "buyer_name": "ENTIDAD", "published_at": "05/10/2026 12:00", "description": "TABLERO ELÉCTRICO",
            "object_label": "Bien", "reference_amount": "---", "currency": "Soles", "seace_version": "3"}]}
    capture = validate_capture(raw)
    normalized = normalize_entry({"row": capture.rows[0], "query": query,
        "page_number": 1, "observed_at": raw["observed_at"]})
    row, sha = normalized["row"], normalized["content_hash"]
    title_sha = hashlib.sha256(row["description"].encode()).hexdigest()
    cache_path = tmp_path / "map/cache/page-000001.private.json"
    cache_sha = write(cache_path, {"version": "prod2_keyword_sweep_v1", "capture": raw, "page_hash": capture.page_hash})
    member = {"convocation_id": 123, "procedure_id": 456, "observed_hash_count": 1,
        "latest_listing_json": row, "latest_content_hash": sha, "first_classification": "new_id"}
    observation = {"convocation_id": 123, "listing_json": row, "content_hash": sha,
        "observed_query": query, "root_query": query, "query_hash": digest(query),
        "observed_query_hash": digest(query), "page_number": 1, "observed_at": raw["observed_at"]}
    manifest = {"run_id": "map", "published_from": query["published_from"],
        "published_to": query["published_to"], "queries": [query]}
    report = {"status": "complete", "mode": "metadata_only", "run_id": "map", "plan_sha256": "a" * 64,
        "source_coverage_complete": True, "model_calls": 0, "document_downloads": 0,
        "queries_planned": 1, "queries_complete": 1, "coverage": [{"coverage_complete": True, "errors": []}],
        "metadata": {"run_id": "map", "manifest": manifest, "manifest_hash": digest(manifest),
            "members": [member], "observations": [observation]}}
    report_path = tmp_path / "map/report.private.json"
    handoff_path = tmp_path / "map/handoff.private.json"
    handoff = {"run_id": "map", "source_coverage_complete": True, "plan_sha256": "a" * 64,
        "candidate_ids_for_review": [123], "needs_review_ids": []}
    report_sha, handoff_sha = write(report_path, report), write(handoff_path, handoff)
    inventory = {"run_id": "map", "plan_sha256": "a" * 64,
        "input_bindings": {"map/report.private.json": report_sha, "map/handoff.private.json": handoff_sha},
        "cache_refs": [{"path": str(cache_path), "sha256": cache_sha, "page_hash": capture.page_hash,
            "query": query, "page_number": 1, "observed_at": raw["observed_at"]}],
        "records": [{"convocation_id": 123, "procedure_id": 456, "listing_content_hash": sha,
            "title": row["description"], "title_sha256": title_sha,
            "evidence": {"cache_ref": 0, "row_index": 0,
                "root_query_hash": digest(query), "observed_query_hash": digest(query)}}]}
    inventory_path = tmp_path / "inventory.private.json"
    inventory_sha = write(inventory_path, inventory)
    selection = {"version": subject.VERSION, "scope_id": "din-industrial", "mapping_run_id": "map",
        "report_sha256": report_sha, "handoff_sha256": handoff_sha,
        "inventory_path": str(inventory_path), "inventory_sha256": inventory_sha,
        "candidates": [{"convocation_id": 123, "procedure_id": 456,
            "listing_content_hash": sha, "title_sha256": title_sha}],
        "selection_review": {"held_ids": [999], "automatic_paid_authorization": False}}
    selection_path = tmp_path / "selection.private.json"
    selection_sha = write(selection_path, selection)
    for name in ("scripts/run_prod2_selected.py", "scripts/run_prod2_accepted.py",
                 "workers/seace/src/buenapro_worker/synthetic.py", "prompts/base.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("offline-only")
    return {"root": tmp_path, "handoff": handoff_path, "report": report_path,
        "inventory": inventory_path, "selection": selection_path, "selection_sha": selection_sha,
        "cache": cache_path}


def load(package):
    return subject.load_selection(package["handoff"], package["selection"],
        package["selection_sha"], root=package["root"])


def update_selection(package, edit):
    selected = json.loads(package["selection"].read_text())
    edit(selected)
    package["selection_sha"] = write(package["selection"], selected)


def update_report(package, edit):
    report = json.loads(package["report"].read_text())
    edit(report)
    sha = write(package["report"], report)
    inventory = json.loads(package["inventory"].read_text())
    inventory["input_bindings"]["map/report.private.json"] = sha
    inventory_sha = write(package["inventory"], inventory)
    update_selection(package, lambda v: v.update(report_sha256=sha, inventory_sha256=inventory_sha))


def test_exact_metadata_to_native_staging_without_search_or_payment(package):
    result = load(package)
    item = result["items"][0]
    assert item["row"]["convocation_id"] == 123 and item["row"]["object_label"] == "Bien"
    assert item["page_number"] == 1 and item["query"]["keyword"] == "tablero"
    assert item["observed_at"] == "2026-10-20T08:00:00+00:00"
    assert len(item["handoff_provenance"]["associations"]) == 1
    assert result["binding"]["candidate_ids"] == [123]
    assert result["coverage"][0]["source_queries_repeated"] == 0
    assert len(result["binding"]["runtime_bindings"]) == 4
    assert result["binding"] == load(package)["binding"]


@pytest.mark.parametrize("field", ["selection", "handoff", "report", "inventory", "cache"])
def test_every_changed_bound_input_is_rejected(package, field):
    package[field].write_text(package[field].read_text() + " ")
    with pytest.raises(subject.HandoffSelectionError, match="sha256_mismatch"):
        load(package)


@pytest.mark.parametrize("edit,code", [
    (lambda v: v["candidates"].append(deepcopy(v["candidates"][0])), "duplicate_or_invalid_identity"),
    (lambda v: v["candidates"][0].update(procedure_id=999), "identity_or_hash"),
    (lambda v: v["candidates"][0].update(title_sha256="c" * 64), "identity_or_hash"),
    (lambda v: v["candidates"][0].update(convocation_id=999), "outside_handoff"),
    (lambda v: v.update(candidates=[]), "empty_selection"),
    (lambda v: v.update(version="unknown"), "invalid_selection_contract"),
])
def test_selection_conflicts_fail_closed(package, edit, code):
    update_selection(package, edit)
    with pytest.raises(subject.HandoffSelectionError, match=code):
        load(package)


@pytest.mark.parametrize("edit,code", [
    (lambda v: v.update(source_coverage_complete=False), "not_complete"),
    (lambda v: v["coverage"][0].update(errors=[{"code": "source_403"}]), "coverage_invalid"),
    (lambda v: v["metadata"]["members"][0].update(observed_hash_count=2), "changed_selected_listing"),
    (lambda v: v["metadata"]["observations"][0].update(page_number=2), "provenance_mismatch"),
    (lambda v: v["metadata"]["manifest"].update(published_from="2026-10-01"), "manifest_hash_mismatch"),
])
def test_rehashed_outer_inputs_do_not_hide_semantic_conflicts(package, edit, code):
    update_report(package, edit)
    with pytest.raises(subject.HandoffSelectionError, match=code):
        load(package)


def test_runtime_binding_changes_without_rewriting_old_metadata(package):
    first = load(package)["binding"]
    (package["root"] / "workers/seace/src/buenapro_worker/synthetic.py").write_text("changed")
    assert load(package)["binding"]["runtime_bindings"] != first["runtime_bindings"]
