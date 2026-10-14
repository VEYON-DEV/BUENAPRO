"""Immutable page-routing policy, strict boundaries and legacy receipt compatibility."""
import copy

import pytest

from buenapro_worker.prod2 import model_routing as subject


SMALL = "gemini-3.1-flash-lite"
LARGE = "gemini-2.5-flash-lite"


def manifest():
    return {"llm1_model_routing": subject.default_model_routing()}


def test_default_policy_is_exact_and_returns_independent_values():
    expected = {
        "policy_version": "prod2_llm1_page_routing_v1", "page_threshold": 20,
        "small_model": SMALL, "large_model": LARGE,
    }
    assert subject.MODEL_ROUTING_POLICY == expected["policy_version"]
    first = subject.default_model_routing()
    assert first == expected
    first["page_threshold"] = 1
    assert subject.default_model_routing() == expected


@pytest.mark.parametrize("pages,model", [(1, SMALL), (20, SMALL), (21, LARGE), (1000, LARGE)])
@pytest.mark.parametrize("base_model", [SMALL, LARGE])
def test_total_page_boundaries_select_fixed_models_and_prove_the_choice(pages, model, base_model):
    run = manifest()
    before = copy.deepcopy(run)
    resolved, metadata = subject.resolve_llm1_model(run, pages, base_model)
    assert resolved == model
    assert metadata == {
        "policy_version": subject.MODEL_ROUTING_POLICY, "page_threshold": 20,
        "page_count": pages, "model": model,
    }
    assert run == before
    metadata["page_threshold"] = 1
    assert run == before


def test_routing_uses_total_pages_even_when_each_transport_part_has_twenty_pages():
    run = {**manifest(), "transport_parts": [{"page_count": 20}, {"page_count": 20}]}
    model, metadata = subject.resolve_llm1_model(run, 40, SMALL)
    assert model == LARGE
    assert metadata["page_count"] == 40


@pytest.mark.parametrize("threshold,pages,model", [(1, 1, SMALL), (1, 2, LARGE), (1000, 1000, SMALL)])
def test_versioned_threshold_is_bounded_and_honored(threshold, pages, model):
    run = manifest()
    run["llm1_model_routing"]["page_threshold"] = threshold
    assert subject.resolve_llm1_model(run, pages, SMALL)[0] == model


@pytest.mark.parametrize("field,value", [
    ("policy_version", "prod2_llm1_page_routing_v2"), ("policy_version", None),
    ("small_model", LARGE), ("small_model", "gemini-2.5-flash"),
    ("large_model", SMALL), ("large_model", "gemini-3.1-flash-lite-preview"),
])
def test_mutated_version_and_fixed_models_fail_without_fallback(field, value):
    run = manifest()
    run["llm1_model_routing"][field] = value
    with pytest.raises(subject.ModelRoutingError):
        subject.resolve_llm1_model(run, 21, SMALL)


@pytest.mark.parametrize("policy", [None, False, "v1", [], {}, {"page_threshold": 20}])
def test_present_malformed_policy_does_not_behave_as_legacy(policy):
    with pytest.raises(subject.ModelRoutingError, match="fields"):
        subject.resolve_llm1_model({"llm1_model_routing": policy}, 21, SMALL)


@pytest.mark.parametrize("missing", ["policy_version", "page_threshold", "small_model", "large_model"])
def test_missing_policy_field_is_rejected(missing):
    run = manifest()
    del run["llm1_model_routing"][missing]
    with pytest.raises(subject.ModelRoutingError, match="fields"):
        subject.resolve_llm1_model(run, 21, SMALL)


def test_unknown_policy_fields_are_rejected():
    run = manifest()
    run["llm1_model_routing"]["fallback_model"] = SMALL
    with pytest.raises(subject.ModelRoutingError, match="fields"):
        subject.resolve_llm1_model(run, 21, SMALL)


@pytest.mark.parametrize("invalid", [0, -1, 1001, True, False, 20.0, "20", None])
def test_invalid_threshold_is_rejected(invalid):
    run = manifest()
    run["llm1_model_routing"]["page_threshold"] = invalid
    with pytest.raises(subject.ModelRoutingError, match="Page threshold"):
        subject.resolve_llm1_model(run, 21, SMALL)


@pytest.mark.parametrize("invalid", [0, -1, 1001, True, False, 21.0, "21", None])
def test_invalid_complete_page_count_is_rejected(invalid):
    with pytest.raises(subject.ModelRoutingError, match="Complete PDF page count"):
        subject.resolve_llm1_model(manifest(), invalid, SMALL)


@pytest.mark.parametrize("legacy_model", [SMALL, LARGE, "gemini-2.0-flash", " literal-old-model "])
def test_legacy_manifest_preserves_literal_model_and_has_no_routing_metadata(legacy_model):
    assert subject.resolve_llm1_model({}, 493, legacy_model) == (legacy_model, None)


def test_legacy_manifest_does_not_add_new_page_validation_or_mutate_manifest():
    run = {"model": "old"}
    assert subject.resolve_llm1_model(run, None, "old") == ("old", None)
    assert run == {"model": "old"}


@pytest.mark.parametrize("invalid", [None, "", "  ", False, 123])
def test_invalid_base_identifier_is_rejected_in_every_mode(invalid):
    for run in ({}, manifest()):
        with pytest.raises(subject.ModelRoutingError, match="Base model"):
            subject.resolve_llm1_model(run, 21, invalid)


def test_policy_mode_rejects_unrecognized_base_model_before_any_fallback():
    with pytest.raises(subject.ModelRoutingError, match="Base model"):
        subject.resolve_llm1_model(manifest(), 21, "gemini-2.0-flash")


@pytest.mark.parametrize("invalid", [None, [], False, "manifest"])
def test_invalid_manifest_is_rejected(invalid):
    with pytest.raises(subject.ModelRoutingError, match="manifest"):
        subject.resolve_llm1_model(invalid, 21, SMALL)
