import { commercialCategories, commercialTaxonomy, type CommercialFilters } from "@/lib/commercialTaxonomy";

// SQL values here come exclusively from the versioned catalog, never request text.
const literal = (value: string) => `'${value.replaceAll("'", "''")}'`;
const known = (items: { id: string }[]) => `ARRAY[${items.map(item => literal(item.id)).join(",")}]::text[]`;
const labels = (items: string[]) => `ARRAY[${items.map(literal).join(",")}]::text[]`;
const categoryRules = commercialCategories.categories.map(category => `(${literal(category.id)}, ${labels(category.match_any.areas)},
  ${labels(category.match_any.work_types)}, ${labels(category.match_any.specialties)})`).join(",\n");
const safeArray = (field: string) => `CASE WHEN jsonb_typeof(cc_input.value->'${field}') = 'array' THEN cc_input.value->'${field}' ELSE '[]'::jsonb END`;

function validArray(field: string, items: { id: string }[]) {
  return `(jsonb_typeof(cc_input.value->'${field}') = 'array' AND NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(${safeArray(field)}) AS tag(value)
    WHERE (jsonb_typeof(tag.value) = 'string' AND (tag.value #>> '{}') = ANY(${known(items)})) IS NOT TRUE
  ) AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(${safeArray(field)}) AS tag(value)
    GROUP BY tag.value HAVING count(*) > 1))`;
}

// Validate retained documentary evidence, not its semantic correctness. Unknown,
// incomplete or uncited tags remain unclassified; no model is called to repair them.
const validEvidence = `(jsonb_typeof(cc_input.value->'evidence') = 'array' AND NOT EXISTS (
  SELECT 1 FROM jsonb_array_elements(${safeArray("evidence")}) AS evidence(value)
  WHERE (jsonb_typeof(evidence.value) = 'object'
    AND jsonb_typeof(evidence.value->'value') = 'string'
    AND jsonb_typeof(evidence.value->'citation') = 'string'
    AND evidence.value->>'citation' !~ '^[[:space:]]*$'
    AND CASE WHEN jsonb_typeof(evidence.value->'page') = 'number' THEN
      (evidence.value->>'page')::numeric BETWEEN 1 AND 9007199254740991
      AND trunc((evidence.value->>'page')::numeric) = (evidence.value->>'page')::numeric ELSE false END
    AND CASE evidence.value->>'axis'
      WHEN 'area' THEN evidence.value->>'value' = cc_input.value->>'primary_area'
        OR ${safeArray("secondary_areas")} ? (evidence.value->>'value')
      WHEN 'work_type' THEN ${safeArray("work_types")} ? (evidence.value->>'value')
      WHEN 'specialty' THEN ${safeArray("specialties")} ? (evidence.value->>'value')
      ELSE false END
  ) IS NOT TRUE
) AND NOT EXISTS (
  SELECT 1 FROM (
    SELECT 'area'::text AS axis, cc_input.value->>'primary_area' AS value
    UNION ALL SELECT 'area', tag.value #>> '{}' FROM jsonb_array_elements(${safeArray("secondary_areas")}) tag(value)
    UNION ALL SELECT 'work_type', tag.value #>> '{}' FROM jsonb_array_elements(${safeArray("work_types")}) tag(value)
    UNION ALL SELECT 'specialty', tag.value #>> '{}' FROM jsonb_array_elements(${safeArray("specialties")}) tag(value)
  ) selected
  WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(${safeArray("evidence")}) evidence(value)
    WHERE evidence.value->>'axis' = selected.axis AND evidence.value->>'value' = selected.value)
))`;

/** Choose the latest eligible extraction first, even if it has no tags. */
const prod2Current = `SELECT ex.raw_extraction_json, ex.summary_json
  FROM prod2_document_extractions ex
  WHERE ex.id_convocatoria = p.id_convocatoria AND ex.is_current = true AND ex.quality <> 'failed'
  ORDER BY ex.created_at DESC, ex.id DESC LIMIT 1`;
const prod6Current = `SELECT tx.raw_extraction_json, tx.summary_json
  FROM contract_documents cd JOIN tdr_extractions tx ON tx.contract_document_id = cd.id
  WHERE cd.id_contrato = c.id_contrato AND tx.is_current = true AND tx.quality <> 'failed'
  ORDER BY tx.created_at DESC, tx.id DESC LIMIT 1`;

function classificationJoin(current: string) {
  return `LEFT JOIN LATERAL (${current}) cc_source ON true
  LEFT JOIN LATERAL (SELECT COALESCE(NULLIF(cc_source.raw_extraction_json->'commercial_classification', 'null'::jsonb),
    cc_source.summary_json->'commercial_classification') AS value) cc_input ON true
  LEFT JOIN LATERAL (SELECT CASE WHEN jsonb_typeof(cc_input.value) = 'object' THEN CASE
    WHEN cc_input.value->>'taxonomy_version' = ${literal(commercialTaxonomy.version)}
    AND cc_input.value->>'primary_area' = ANY(${known(commercialTaxonomy.areas)})
    AND cc_input.value->>'primary_area' <> 'sin_clasificar'
    AND cc_input.value->>'support_status' = ANY(${known(commercialTaxonomy.support_statuses)})
    AND ${validArray("secondary_areas", commercialTaxonomy.areas.filter(item => item.id !== "sin_clasificar"))}
    AND NOT (${safeArray("secondary_areas")} ? (cc_input.value->>'primary_area'))
    AND ${validArray("work_types", commercialTaxonomy.work_types)}
    AND ${validArray("specialties", commercialTaxonomy.specialties)}
    AND ${validEvidence}
    THEN CASE WHEN EXISTS (
      SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(cc_source.raw_extraction_json->'retrieval_evidence_audit'->'errors') = 'array'
        THEN cc_source.raw_extraction_json->'retrieval_evidence_audit'->'errors' ELSE '[]'::jsonb END) audit(value)
      WHERE audit.value->>'kind' = 'unverified_commercial_citation'
    ) THEN cc_input.value || jsonb_build_object('support_status', 'needs_review') ELSE cc_input.value END
    ELSE NULL END ELSE NULL END AS classification) commercial ON true`;
}

export const PROD2_COMMERCIAL_JOIN = classificationJoin(prod2Current);
export const PROD6_COMMERCIAL_JOIN = classificationJoin(prod6Current);
export const COMMERCIAL_SELECT = "commercial.classification AS commercial_classification";

export function hasCommercialFilters(filters: CommercialFilters | undefined) {
  return Boolean(filters && (filters.categories?.length || filters.areas.length || filters.workTypes.length || filters.specialties.length || filters.statuses.length));
}

export function appendCommercialPredicates(filters: CommercialFilters | undefined, values: unknown[], where: string[]) {
  if (!filters) return;
  if (filters.categories?.length) {
    values.push(filters.categories);
    where.push(`(CASE WHEN commercial.classification IS NULL THEN false ELSE EXISTS (
      SELECT 1 FROM (VALUES ${categoryRules}) AS category(id, areas, work_types, specialties)
      WHERE category.id = ANY($${values.length}::text[]) AND (
        commercial.classification->>'primary_area' = ANY(category.areas)
        OR commercial.classification->'secondary_areas' ?| category.areas
        OR commercial.classification->'work_types' ?| category.work_types
        OR commercial.classification->'specialties' ?| category.specialties
      )
    ) END)`);
  }
  if (filters.areas.length) {
    values.push(filters.areas);
    where.push(`(COALESCE(commercial.classification->>'primary_area', 'sin_clasificar') = ANY($${values.length}::text[])
      OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(commercial.classification->'secondary_areas', '[]'::jsonb)) AS area(value)
        WHERE area.value <> 'sin_clasificar' AND area.value = ANY($${values.length}::text[])))`);
  }
  if (filters.workTypes.length) {
    values.push(filters.workTypes);
    where.push(`COALESCE(commercial.classification->'work_types', '[]'::jsonb) ?| $${values.length}::text[]`);
  }
  if (filters.specialties.length) {
    values.push(filters.specialties);
    where.push(`COALESCE(commercial.classification->'specialties', '[]'::jsonb) ?| $${values.length}::text[]`);
  }
  if (filters.statuses.length) {
    values.push(filters.statuses);
    where.push(`COALESCE(commercial.classification->>'support_status', 'unclassified') = ANY($${values.length}::text[])`);
  }
}
