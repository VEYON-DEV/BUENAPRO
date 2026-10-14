import copy

import pytest

from buenapro_worker.matching.readiness import match_readiness, require_match_ready
from buenapro_worker.matching.substantive_policy import is_administrative_requirement


def snapshot():
    source = {"extraction_id": 17, "is_current": True, "quality": "auto", "requires_human_review": False,
              "raw_extraction_json": {"requirements": []}, "summary_json": {"object": "Servidor"}}
    facets = [{"extraction_id": 17, "is_current": True, "facet": "specific_experience", "label": "Venta de computadoras",
               "required": True, "details_json": {"monto": 40000}}]
    return source, facets


@pytest.mark.parametrize("changes,reason", [
    ({"extraction_id": None}, "pending_reading"), ({"is_current": False}, "stale_reading"),
    ({"quality": "failed"}, "failed_or_unknown_reading"), ({"quality": None}, "failed_or_unknown_reading"),
    ({"requires_human_review": True}, "reading_requires_review"), ({"requires_human_review": None}, "reading_requires_review"),
    ({"raw_extraction_json": {}}, "empty_reading"), ({"summary_json": {}}, "empty_summary"),
])
def test_unread_or_unusable_data_never_enters_the_paid_batch(changes, reason):
    source, facets = snapshot(); source.update(changes)
    assert match_readiness(source, facets) == (False, reason)
    with pytest.raises(ValueError, match=reason): require_match_ready(source, facets)


def test_current_reading_is_reusable_without_claiming_participation_permission():
    source, facets = snapshot(); before = copy.deepcopy((source, facets))
    assert match_readiness(source, facets) == (True, "ready_from_existing_reading")
    require_match_ready(source, facets)
    assert (source, facets) == before


def test_requirements_belong_to_the_same_current_source_and_have_substantive_capacity():
    source, facets = snapshot()
    assert match_readiness(None, facets) == (False, "pending_reading")
    assert match_readiness(source, []) == (False, "missing_or_unbound_requirements")
    facets[0]["extraction_id"] = 18
    assert match_readiness(source, facets) == (False, "missing_or_unbound_requirements")
    facets[0].update(extraction_id=17, facet="rnp", label="RNP vigente")
    assert match_readiness(source, facets) == (False, "no_required_substantive_requirements")


def test_explicit_provisional_mode_reuses_reading_without_clearing_its_review_flag():
    source, facets = snapshot(); source["requires_human_review"] = True
    assert match_readiness(source, facets) == (False, "reading_requires_review")
    assert match_readiness(source, facets, provisional_source_review=True) == (True, "provisional_existing_reading_source_review_preserved")
    assert source["requires_human_review"] is True
    source["raw_extraction_json"] = {}
    assert match_readiness(source, facets, provisional_source_review=True) == (False, "empty_reading")
    source["quality"] = "failed"
    assert match_readiness(source, facets, provisional_source_review=True) == (False, "failed_or_unknown_reading")


@pytest.mark.parametrize("label,expected", [
    ("Persona Natural y/o jurídica", True), ("Carta de presentación indicando costo", True),
    ("No incurrir en nepotismo", True), ("Pacto de integridad", True),
    ("Carta de autorización del fabricante", False),
])
def test_submission_and_ordinary_identity_are_not_capacity_but_special_authorization_is(label, expected):
    assert is_administrative_requirement({"facet": "other", "label": label}) is expected
