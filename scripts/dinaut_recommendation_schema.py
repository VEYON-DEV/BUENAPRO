"""Strict Google responseJsonSchema for a frozen shared recommendation batch.

Pure construction only: no DB, filesystem, network or provider imports. The
schema covers every case and requirement at its exact original array position.
"""
from __future__ import annotations

import re


def _document_refs(profile: object) -> list[str]:
    if not isinstance(profile, dict):
        raise ValueError("Recommendation profile must be an object")
    columns, documents = profile.get("document_columns"), profile.get("documents")
    if (not isinstance(columns, list) or not all(isinstance(column, str) for column in columns)
            or len(columns) != len(set(columns)) or "ref" not in columns
            or not isinstance(documents, list)):
        raise ValueError("Invalid columnar documentary profile")
    index = columns.index("ref")
    refs = []
    for row in documents:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("Document row does not match document_columns")
        alias = row[index]
        if not isinstance(alias, str) or not re.fullmatch(r"D[1-9][0-9]*", alias):
            raise ValueError("Invalid documentary alias")
        if alias in refs:
            raise ValueError("Duplicate documentary alias")
        refs.append(alias)
    return refs


def _strict_object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _exact_array(schemas: list[dict]) -> dict:
    # Exact length also prohibits any trailing tuple item. Omit boolean `items`
    # and `unevaluatedItems`, which are outside Google's documented subset.
    result = {"type": "array", "minItems": len(schemas), "maxItems": len(schemas)}
    if schemas:
        result["prefixItems"] = schemas
    return result


def schema_from_contexts(contexts: dict[str, dict]) -> dict:
    """Return the JSON schema value for generationConfig.responseJsonSchema.

    Input is the mapping passed to build_recommendation_batch: each value has
    profile.document_columns/documents plus evaluation.requirements. Shared
    profile identity is checked before a schema can silently bind other sources.
    """
    if not isinstance(contexts, dict) or not 1 <= len(contexts) <= 5:
        raise ValueError("Recommendation schema requires one to five cases")
    case_schemas = []
    first_profile = None
    for case_key, context in contexts.items():
        if (not isinstance(case_key, str) or not re.fullmatch(r"seace_prod[26]-[1-9][0-9]*", case_key)
                or not isinstance(context, dict)):
            raise ValueError("Invalid stable source case_key")
        profile = context.get("profile")
        refs = _document_refs(profile)
        if first_profile is None:
            first_profile = profile
        elif profile != first_profile:
            raise ValueError("Shared recommendation batch profiles differ")
        evaluation = context.get("evaluation")
        requirements = evaluation.get("requirements") if isinstance(evaluation, dict) else None
        if not isinstance(requirements, list) or not requirements:
            raise ValueError("Each recommendation case requires explicit requirements")
        positions = []
        seen = set()
        for requirement in requirements:
            source_index = requirement.get("source_index") if isinstance(requirement, dict) else None
            if type(source_index) is not int or source_index < 1 or source_index in seen:
                raise ValueError("Invalid or duplicate requirement source_index")
            seen.add(source_index)
            reference_items = {"type": "string", "enum": list(refs)} if refs else {"type": "string"}
            positions.append(_strict_object({
                "source_index": {"type": "integer", "enum": [source_index]},
                "evidencia": {"type": "string"},
                "brecha": {"type": "string"},
                "accion": {"type": "string"},
                "evidence_refs": {"type": "array", "items": reference_items,
                                  "minItems": 0, "maxItems": len(refs)},
            }))
        case_schemas.append(_strict_object({
            "case_key": {"type": "string", "enum": [case_key]},
            "resumen": {"type": "string"},
            "requisitos": _exact_array(positions),
            "acciones_recomendadas": {"type": "array", "items": {"type": "string"}},
        }))
    return _strict_object({"evaluations": _exact_array(case_schemas)})


def legacy_map_schema_from_contexts(contexts: dict[str,dict]) -> dict:
    """GenerateContent responseSchema using only established OBJECT/ARRAY fields.

    Required named properties enforce coverage without tuple-schema support.
    Numeric source indices are represented by stable r-prefixed keys on the wire.
    """
    schema_from_contexts(contexts)  # Validate the same frozen scope and references.
    def obj(properties):
        return {'type':'OBJECT','properties':properties,'required':list(properties),
                'propertyOrdering':list(properties)}
    cases={}
    for key,context in contexts.items():
        rows={}
        for requirement in context['evaluation']['requirements']:
            rows['r'+str(requirement['source_index'])]=obj({
                'evidencia':{'type':'STRING'},'brecha':{'type':'STRING'},'accion':{'type':'STRING'},
                'evidence_refs':{'type':'ARRAY','items':{'type':'STRING'}}})
        cases[key]=obj({'resumen':{'type':'STRING'},'requisitos':obj(rows),
                       'acciones_recomendadas':{'type':'ARRAY','items':{'type':'STRING'}}})
    return obj({'evaluations':obj(cases)})


MAP_WIRE_PROMPT='''
Para ESTE envío prevalece el esquema de objetos requerido por la API:
{"evaluations": {"<case_key>": {"resumen":"...", "requisitos": {
"r<source_index>": {"evidencia":"...", "brecha":"...", "accion":"...", "evidence_refs":[]}},
"acciones_recomendadas":[]}}}. Incluye TODAS las claves de caso y requisito del
esquema, incluso los perfiles profesionales y requisitos sin evidencia. No añadas
source_index dentro del objeto: la clave rN conserva su identidad. Las acciones
son siempre una lista. No omitas requisitos ni cambies ningún puntaje o estado.
'''


def normalize_map_output(value:dict,case_keys:list[str]|None=None)->dict:
    """Lossless shape conversion; values remain exact model text, never invented."""
    if not isinstance(value,dict) or set(value)!={'evaluations'}:
        raise ValueError('Invalid recommendation envelope')
    evaluations=value['evaluations']
    if isinstance(evaluations,list):return value  # Existing successful pilot.
    if not isinstance(evaluations,dict):raise ValueError('Invalid wire case map')
    order=case_keys if case_keys is not None else list(evaluations)
    if len(order)!=len(set(order)) or set(order)!=set(evaluations):
        raise ValueError('Wire case coverage changed')
    rows=[]
    for key in order:
        case=evaluations[key]
        if not isinstance(case,dict) or set(case)!={'resumen','requisitos','acciones_recomendadas'}:
            raise ValueError('Invalid wire case properties')
        requirements=case['requisitos']
        if not isinstance(requirements,dict) or not all(re.fullmatch(r'r[1-9][0-9]*',k) for k in requirements):
            raise ValueError('Invalid wire requirement map')
        ordered=[]
        for index in sorted(requirements,key=lambda k:int(k[1:])):
            row=requirements[index]
            if not isinstance(row,dict) or set(row)!={'evidencia','brecha','accion','evidence_refs'}:
                raise ValueError('Invalid wire requirement fields')
            ordered.append({'source_index':int(index[1:]),**row})
        rows.append({'case_key':key,'resumen':case['resumen'],'requisitos':ordered,
                     'acciones_recomendadas':case['acciones_recomendadas']})
    return {'evaluations':rows}
