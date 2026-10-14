import copy
import json
from pathlib import Path

import pytest

from buenapro_worker.prod2.batch_plan import digest
from buenapro_worker.prod2.business_batches import select_business_batches, report_business_batches

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def inputs():
    return (json.loads((ROOT / "docs/sources/prod2-business-batches.v1.json").read_text()),
            json.loads((ROOT / "docs/sources/prod2-convenio-keywords.v3.json").read_text()))


def test_partition_has_exact_union_and_preserves_both_company_provenances(inputs):
    config, inventory = inputs
    result = select_business_batches(config, inventory, scope="todas")
    expected = {keyword for line in inventory["member_keyword_inventory"] for keyword in line["keywords"]}
    assert set(result["keywords"]) == expected and len(result["keywords"]) == 166
    business = result["business_plan"]
    assert len(business["batches"]) == 12
    assert sum(batch["search_keyword_count"] for batch in business["batches"]) == 166
    assert sum(batch["primary_keyword_count"] for batch in business["batches"]) == 166
    assert business["selected_literal_shared_keyword_count"] == 16
    shared = {keyword for keyword, row in business["keyword_index"].items() if row["literal_shared"]}
    assert shared == {"analítica de datos", "aplicativo", "automatización", "chatbot", "ciencia de datos",
        "cloud", "datos", "desarrollo de software", "informática", "inteligencia artificial", "licencia",
        "microsoft", "plataforma", "software", "suscripción", "web"}
    for keyword in shared:
        assert business["keyword_index"][keyword]["declared_members"] == ["veyon", "dinaut"]
    assert business["keyword_index"]["aws"]["declared_members"] == ["veyon"]
    assert business["keyword_index"]["plc"]["declared_members"] == ["dinaut"]
    assert business["same_joint_commercial_profile"] and not business["routing_is_business_classification"]


def test_digital_and_industrial_scopes_cover_union_without_implicit_keywords(inputs):
    config, inventory = inputs
    digital = select_business_batches(config, inventory, scope="digitales")["keywords"]
    industrial = select_business_batches(config, inventory, scope="industriales")["keywords"]
    assert len(digital) == 73 and len(industrial) == 93
    assert not set(digital) & set(industrial)
    assert set(digital) | set(industrial) == set(select_business_batches(config, inventory, scope="todas")["keywords"])
    assert "ia" in digital and "sistema" not in digital and "sistemas" not in digital
    assert "agentes ia" in digital and "agentes de ia" in digital
    assert "sistema de información" in digital and "sistemas de información" in digital


def test_isolated_batch_keeps_related_signals_but_combined_searches_them_once(inputs):
    config, inventory = inputs
    licenses = select_business_batches(config, inventory, batch_ids=["licencias_cloud"])
    assert len(licenses["keywords"]) == 18 and "software" in licenses["keywords"]
    software = licenses["business_plan"]["keyword_index"]["software"]
    assert software["primary_batch_id"] == "software_desarrollo" and software["search_batch_id"] == "licencias_cloud"
    combined = select_business_batches(config, inventory, batch_ids=["licencias_cloud", "software_desarrollo"])
    assert len(combined["keywords"]) == 42 and combined["keywords"].count("software") == 1
    assert combined["business_plan"]["keyword_index"]["software"]["search_batch_id"] == "software_desarrollo"
    assert combined == select_business_batches(config, inventory,
        batch_ids=["software_desarrollo", "licencias_cloud", "software_desarrollo"])


def test_training_related_plc_and_network_terms_keep_one_owner_for_combined_run(inputs):
    config, inventory = inputs
    training = select_business_batches(config, inventory, batch_ids=["capacitacion_industrial"])
    assert len(training["keywords"]) == 16
    assert training["business_plan"]["keyword_index"]["plc"]["search_batch_id"] == "capacitacion_industrial"
    full = select_business_batches(config, inventory, scope="todas")["business_plan"]
    assert full["keyword_index"]["plc"]["search_batch_id"] == "control_industrial"
    assert full["keyword_index"]["profibus"]["search_batch_id"] == "redes_industriales"
    for batch in full["batches"]:
        assert batch["primary_keyword_count"] == batch["search_keyword_count"]


def test_broad_signals_are_retained_after_specific_signals_in_each_batch(inputs):
    config, inventory = inputs
    result = select_business_batches(config, inventory, scope="todas")
    index = result["business_plan"]["keyword_index"]
    for batch in result["business_plan"]["batches"]:
        flags = [index[kw]["broad"] for kw in result["keywords"] if index[kw]["search_batch_id"] == batch["id"]]
        assert flags == sorted(flags)
    assert index["ia"]["broad"] and index["api"]["broad"] and index["soporte"]["broad"]


@pytest.mark.parametrize("change", ["missing_keyword", "duplicate_keyword", "invented_keyword", "missing_line",
                                    "duplicate_line", "stale_inventory", "duplicate_id", "invalid_scope", "unknown_broad"])
def test_invalid_or_stale_configuration_cannot_silently_reduce_coverage(inputs, change):
    config, inventory = inputs
    if change == "missing_keyword":
        config["batches"][0]["primary_keywords"].remove("software")
    elif change == "duplicate_keyword":
        config["batches"][1]["primary_keywords"].append("software")
    elif change == "invented_keyword":
        config["batches"][0]["primary_keywords"].append("not_configured")
    elif change == "missing_line":
        config["batches"][0]["source_lines"].pop()
    elif change == "duplicate_line":
        config["batches"][1]["source_lines"].append(config["batches"][0]["source_lines"][0])
    elif change == "stale_inventory":
        inventory["keyword_snapshot_date"] = "2026-10-07"
    elif change == "duplicate_id":
        config["batches"][1]["id"] = config["batches"][0]["id"]
    elif change == "invalid_scope":
        config["batches"][0]["scope"] = "unknown"
    elif change == "unknown_broad":
        config["broad_keywords"].append("not_configured")
    with pytest.raises(ValueError):
        select_business_batches(config, inventory)


@pytest.mark.parametrize("kwargs", [{"batch_ids": ["unknown"]}, {"batch_ids": []},
    {"scope": "unknown"}, {"batch_ids": ["ia_datos"], "scope": "digitales"}])
def test_unknown_and_conflicting_selectors_rejected(inputs, kwargs):
    with pytest.raises(ValueError):
        select_business_batches(*inputs, **kwargs)


def test_reports_partial_source_coverage_and_attributes_duplicate_candidate_only_once(inputs):
    selected = select_business_batches(*inputs, scope="digitales")
    selected.update(resolved_years=[2026, 2025], objects=["good", "service"])
    coverage = [{"query": {"keyword": "software"}, "coverage_complete": True},
        {"query": {"keyword": "licencia"}, "coverage_complete": False}]
    outcomes = [{"business_batch_id": "software_desarrollo", "accepted": True, "status": "llm1_complete"},
                {"business_batch_id": "licencias_cloud", "accepted": False, "status": "rejected_below_60"}]
    reports = report_business_batches(selected, coverage, outcomes)
    rows = {row["id"]: row for row in reports}
    assert rows["software_desarrollo"]["llm1_complete"] == 1
    assert not rows["licencias_cloud"]["source_coverage_complete"]
    assert rows["ia_datos"]["source_coverage_complete"] is False
    assert rows["software_desarrollo"]["source_coverage_complete"] is False
    assert rows["software_desarrollo"]["queries_expected"] == 100
    assert sum(row["processed_first_discovery"] for row in reports) == len(outcomes)
    assert report_business_batches({}, coverage, outcomes) == []


def test_report_requires_all_planned_queries_not_just_successful_observed_subset(inputs):
    manifest = select_business_batches(*inputs, batch_ids=["licencias_cloud"])
    manifest.update(resolved_years=[2026], objects=["good", "service"])
    coverage = [{"query": {"keyword": kw, "year": year, "object_type": object_type}, "coverage_complete": True}
        for kw in manifest["keywords"] for year in manifest["resolved_years"] for object_type in manifest["objects"]]
    assert report_business_batches(manifest, coverage, [])[0]["source_coverage_complete"]
    assert not report_business_batches(manifest, coverage[:-1], [])[0]["source_coverage_complete"]
    assert not report_business_batches(manifest, [], [])[0]["source_coverage_complete"]
    assert not report_business_batches(manifest, coverage[:-1] + [coverage[0]], [])[0]["source_coverage_complete"]


def test_order_change_versions_plan_but_keeps_keyword_and_company_provenance(inputs):
    config, inventory = inputs
    original = select_business_batches(config, inventory, scope="todas")
    changed = copy.deepcopy(config)
    changed["batches"][0], changed["batches"][1] = changed["batches"][1], changed["batches"][0]
    revised = select_business_batches(changed, inventory, scope="todas")
    assert revised["business_plan"]["config_hash"] != original["business_plan"]["config_hash"]
    assert set(revised["keywords"]) == set(original["keywords"])
    assert revised["business_plan"]["source_inventory_hash"] == digest(inventory)
    for keyword in original["keywords"]:
        old = original["business_plan"]["keyword_index"][keyword]
        new = revised["business_plan"]["keyword_index"][keyword]
        assert set(new["related_batch_ids"]) == set(old["related_batch_ids"])
        assert {key: value for key, value in new.items() if key != "related_batch_ids"} == {
            key: value for key, value in old.items() if key != "related_batch_ids"}
