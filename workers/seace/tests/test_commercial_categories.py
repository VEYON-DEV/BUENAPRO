"""Same deterministic six-category projection as web; no new paid schema."""
from copy import deepcopy
import json
from pathlib import Path
import tomllib

import pytest

from buenapro_worker.extraction.commercial_classification import TAXONOMY_VERSION
from buenapro_worker.normalization import commercial_categories as subject
from buenapro_worker.normalization.facets import derive_summary


def classified(*, primary="software_ti", secondary=(), work=(), specialties=()):
    selected = [("area", primary), *(("area", value) for value in secondary),
                *(("work_type", value) for value in work), *(("specialty", value) for value in specialties)]
    return {"taxonomy_version": TAXONOMY_VERSION, "primary_area": primary,
        "secondary_areas": list(secondary), "work_types": list(work), "specialties": list(specialties),
        "support_status": "preliminary", "evidence": [{"axis": axis, "value": value,
            "citation": "Objeto: software, inteligencia artificial, cloud, chatbot, RPA, PLC y tableros eléctricos.",
            "page": 1} for axis, value in selected]}


@pytest.mark.parametrize("block,expected", [
    (classified(work=["licencias_suscripciones"]), ["licencias_suscripciones"]),
    (classified(work=["desarrollo_software"]), ["desarrollo_software"]),
    (classified(primary="inteligencia_artificial"), ["ia_automatizacion"]),
    (classified(primary="automatizacion_procesos"), ["ia_automatizacion"]),
    (classified(specialties=["chatbot"]), ["ia_automatizacion"]),
    (classified(specialties=["rpa"]), ["ia_automatizacion"]),
    (classified(primary="industrial_electrica"), ["industrial"]),
    (classified(specialties=["tableros_electricos"]), ["industrial"]),
    (classified(primary="infraestructura_cloud"), ["cloud_infraestructura"]),
    (classified(work=["soporte_mantenimiento"]), ["soporte_mantenimiento"]),
    (classified(primary="otro"), []),
])
def test_categories_use_only_catalog_rules(block, expected):
    before = deepcopy(block)
    assert subject.derive_commercial_categories(block) == expected
    assert block == before


def test_mixed_purchase_can_have_all_six_categories_in_catalog_order():
    block = classified(primary="industrial_electrica",
        secondary=["inteligencia_artificial", "infraestructura_cloud"],
        work=["licencias_suscripciones", "desarrollo_software", "soporte_mantenimiento"], specialties=["chatbot"])
    assert subject.derive_commercial_categories(block) == ["licencias_suscripciones", "desarrollo_software",
        "ia_automatizacion", "industrial", "cloud_infraestructura", "soporte_mantenimiento"]


@pytest.mark.parametrize("block", [None, "IA", {}, {"taxonomy_version": "unknown"},
    {"taxonomy_version": TAXONOMY_VERSION, "primary_area": "sin_clasificar", "secondary_areas": [],
     "work_types": [], "specialties": [], "support_status": "needs_review", "evidence": []}])
def test_absent_invalid_and_unknown_remain_unclassified(block):
    assert subject.derive_commercial_categories(block) == []


def test_summary_projection_is_derived_without_changing_raw_or_legacy_absence():
    raw = {"commercial_classification": classified(specialties=["chatbot"], work=["licencias_suscripciones"]),
           "commercial_categories": ["industrial"], "summary": {"commercial_categories": ["unknown"]}}
    before = deepcopy(raw)
    assert derive_summary(raw)["commercial_categories"] == ["licencias_suscripciones", "ia_automatizacion"]
    assert raw == before
    assert "commercial_categories" not in derive_summary({"summary": {"descripcion_corta": "IA y cloud"}})


def test_packaged_category_catalog_is_in_setuptools_data():
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "commercial_categories.v1.json" in config["tool"]["setuptools"]["package-data"]["buenapro_worker.extraction"]


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(version="unknown"),
    lambda value: value.update(classification_taxonomy_version="unknown"),
    lambda value: value["categories"][0].update(id="unknown"),
    lambda value: value["categories"][0]["match_any"].update(areas=["unknown"]),
    lambda value: value["categories"].pop(),
])
def test_malformed_or_unknown_catalog_fails_closed_without_guessing(monkeypatch, tmp_path, mutate):
    catalog = json.loads(subject.files("buenapro_worker.extraction").joinpath("commercial_categories.v1.json").read_text())
    mutate(catalog)
    (tmp_path / "commercial_categories.v1.json").write_text(json.dumps(catalog))
    monkeypatch.setattr(subject, "files", lambda _: tmp_path)
    subject._categories.cache_clear()
    try:
        assert subject.derive_commercial_categories(classified(work=["licencias_suscripciones"])) == []
    finally:
        subject._categories.cache_clear()
