"""Response schema checks only; no provider/DB calls or imports."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from dinaut_recommendation_schema import schema_from_contexts


def contexts():
    # `ref` deliberately is not column zero: extraction must use the column map.
    profile = {"document_columns": ["summary", "status", "ref"],
               "documents": [["Licencias", "reviewed", "D1"], ["Soporte", "reviewed", "D43"]]}
    return {"seace_prod2-19": {"profile": deepcopy(profile), "evaluation": {
                "requirements": [{"source_index": 2}, {"source_index": 7}, {"source_index": 11}]}},
            "seace_prod6-20": {"profile": deepcopy(profile), "evaluation": {
                "requirements": [{"source_index": 3}]}}}


def output():
    return {"evaluations": [{"case_key": key, "resumen": "Evidencia pendiente.",
        "requisitos": [{"source_index": row["source_index"], "evidencia": "D1: licencias.",
                        "brecha": "Conformidad pendiente.", "accion": "Adjuntar conformidad.",
                        "evidence_refs": ["D1"]} for row in context["evaluation"]["requirements"]],
        "acciones_recomendadas": ["Adjuntar conformidad."]} for key, context in contexts().items()]}


def test_schema_exact_case_and_requirement_lengths_order_and_aliases():
    original = contexts(); before = deepcopy(original)
    schema = schema_from_contexts(original)
    assert original == before
    assert schema["additionalProperties"] is False
    cases = schema["properties"]["evaluations"]
    assert cases["minItems"] == cases["maxItems"] == 2
    assert cases["prefixItems"][0]["properties"]["case_key"]["enum"] == ["seace_prod2-19"]
    assert cases["prefixItems"][1]["properties"]["case_key"]["enum"] == ["seace_prod6-20"]
    requirements = cases["prefixItems"][0]["properties"]["requisitos"]
    assert requirements["minItems"] == requirements["maxItems"] == 3
    assert [row["properties"]["source_index"]["enum"] for row in requirements["prefixItems"]] == [[2], [7], [11]]
    first = requirements["prefixItems"][0]
    assert first["additionalProperties"] is False
    assert first["properties"]["evidence_refs"]["items"]["enum"] == ["D1", "D43"]
    assert set(first["required"]) == {"source_index", "evidencia", "brecha", "accion", "evidence_refs"}
    assert cases["prefixItems"][0]["properties"]["acciones_recomendadas"] == {"type": "array", "items": {"type": "string"}}


@pytest.mark.parametrize("mutation", [
    lambda c: c.clear(),
    lambda c: c["seace_prod2-19"]["profile"]["document_columns"].append("ref"),
    lambda c: c["seace_prod2-19"]["profile"]["documents"][0].pop(),
    lambda c: c["seace_prod2-19"]["profile"]["documents"][0].__setitem__(2, "uuid-not-alias"),
    lambda c: c["seace_prod2-19"]["evaluation"]["requirements"][1].update(source_index=2),
    lambda c: c["seace_prod2-19"]["evaluation"]["requirements"][0].update(source_index=True),
    lambda c: c["seace_prod6-20"]["profile"]["documents"][0].__setitem__(0, "Other snapshot"),
])
def test_malformed_or_mixed_context_fails_before_generation(mutation):
    value = contexts(); mutation(value)
    with pytest.raises(ValueError):
        schema_from_contexts(value)


def test_schema_with_no_document_aliases_allows_only_empty_reference_arrays():
    value = contexts()
    for context in value.values():
        context["profile"]["documents"] = []
    row = schema_from_contexts(value)["properties"]["evaluations"]["prefixItems"][0]["properties"]["requisitos"]["prefixItems"][0]
    assert row["properties"]["evidence_refs"]["maxItems"] == 0
    assert "enum" not in row["properties"]["evidence_refs"]["items"]  #No invalid enum: [].


@pytest.mark.parametrize("mutation", [
    lambda v: v["evaluations"][0].update(acciones_recomendadas="Omitted array"),
    lambda v: v["evaluations"][0]["requisitos"].pop(),
    lambda v: v["evaluations"].reverse(),
    lambda v: v["evaluations"][0]["requisitos"].reverse(),
    lambda v: v["evaluations"][0]["requisitos"][0].update(evidence_refs=["D999"]),
    lambda v: v["evaluations"][0]["requisitos"][0].update(score=100),
    lambda v: v["evaluations"][0].update(score=100),
])
def test_draft202012_rejects_lost_coverage_wrong_order_scalar_actions_and_fabrication(mutation):
    jsonschema = pytest.importorskip("jsonschema", reason="Optional independent Draft202012 validator absent; no install/network in schema task")
    schema = schema_from_contexts(contexts())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(output())
    invalid = output(); mutation(invalid)
    assert list(validator.iter_errors(invalid))


def test_established_schema_uses_required_named_rows_and_exact_lossless_conversion():
    from dinaut_recommendation_schema import legacy_map_schema_from_contexts,normalize_map_output
    original=contexts();schema=legacy_map_schema_from_contexts(original)
    cases=schema['properties']['evaluations']
    assert cases['type']=='OBJECT' and cases['required']==list(original)
    first=cases['properties']['seace_prod2-19']['properties']['requisitos']
    assert first['required']==['r2','r7','r11']
    assert cases['properties']['seace_prod2-19']['properties']['acciones_recomendadas']['type']=='ARRAY'
    expected=output();mapped={}
    for case in expected['evaluations']:
        mapped[case['case_key']]={k:v for k,v in case.items() if k not in {'case_key','requisitos'}}
        mapped[case['case_key']]['requisitos']={'r'+str(r['source_index']):{k:v for k,v in r.items() if k!='source_index'} for r in reversed(case['requisitos'])}
    assert normalize_map_output({'evaluations':mapped},list(original))==expected
    assert normalize_map_output(expected)==expected
    broken=deepcopy(mapped);broken['seace_prod2-19']['requisitos']['r2']['score']=100
    with pytest.raises(ValueError):normalize_map_output({'evaluations':broken},list(original))
    with pytest.raises(ValueError):normalize_map_output({'evaluations':mapped},['other'])
