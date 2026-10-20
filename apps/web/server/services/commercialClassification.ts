import { commercialCategories, commercialTaxonomy, PROD2_TITLE_CATEGORY_MODEL, PROD2_TITLE_CATEGORY_POLICY, type CommercialFilters } from "@/lib/commercialTaxonomy";

// SQL values here come exclusively from the versioned catalog, never request text.
const literal = (value: string) => `'${value.replaceAll("'", "''")}'`;
const known = (items: { id: string }[]) => `ARRAY[${items.map(item => literal(item.id)).join(",")}]::text[]`;
const labels = (items: string[]) => `ARRAY[${items.map(literal).join(",")}]::text[]`;
// Mirrors Python str.split() used for deduplication, preserving all non-whitespace characters.
const titleWhitespace = String.raw`U&'[\0009-\000d\001c-\0020\0085\00a0\1680\2000-\200a\2028\2029\202f\205f\3000]+'`;
const titlePromptSha256 = "641d7969735a8359de2bd53c09224fb1bfa9f916e3884f8f42968882dfd11e35";
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

// Separate from documentary taxonomy: a title has no citation, page or eligibility evidence.
// The latest valid documentary classification always wins, including one outside our six categories.
export const PROD2_TITLE_CATEGORY_JOIN = `LEFT JOIN LATERAL (
  SELECT tc.categories, tc.decision FROM prod2_title_classifications tc
  JOIN prod2_title_category_receipts receipt ON receipt.request_hash = tc.request_hash
  WHERE commercial.classification IS NULL AND tc.convocation_id = p.id_convocatoria
    AND tc.source_title = p.description
    AND tc.source_title_sha256 = encode(sha256(convert_to(p.description, 'UTF8')), 'hex')
    AND tc.policy_version = ${literal(PROD2_TITLE_CATEGORY_POLICY)}
    AND receipt.policy_version = tc.policy_version AND receipt.model = ${literal(PROD2_TITLE_CATEGORY_MODEL)}
    AND receipt.status = 'done'
    AND receipt.prompt_sha256 = ${literal(titlePromptSha256)}
    AND receipt.catalog_sha256 = encode(sha256(convert_to(array_to_json(${known(commercialCategories.categories)})::text, 'UTF8')), 'hex')
    AND receipt.request_json->>'model' = receipt.model
    AND receipt.request_json->'state'->>'titulo' = receipt.normalized_title
    AND receipt.normalized_title = btrim(regexp_replace(p.description, ${titleWhitespace}, ' ', 'g'))
    AND receipt.title_sha256 = encode(sha256(convert_to(receipt.normalized_title, 'UTF8')), 'hex')
    AND receipt.result_json->>'policy_version' = tc.policy_version
    AND receipt.result_json->>'basis' = 'title_jev' AND receipt.result_json->'preliminary' = 'true'::jsonb
    AND receipt.result_json->'categories' = to_jsonb(tc.categories)
    AND receipt.result_json->>'decision' = tc.decision
    AND tc.categories <@ ${known(commercialCategories.categories)} AND array_position(tc.categories, NULL) IS NULL
    AND cardinality(tc.categories) = (SELECT count(DISTINCT category) FROM unnest(tc.categories) category)
    AND ((tc.decision = 'classified' AND cardinality(tc.categories) > 0)
      OR (tc.decision IN ('other', 'unclear') AND cardinality(tc.categories) = 0))
) title_category ON true
LEFT JOIN LATERAL (SELECT
  CASE WHEN commercial.classification IS NOT NULL THEN ARRAY(
    SELECT category.id FROM (VALUES ${categoryRules}) AS category(id, areas, work_types, specialties)
    WHERE commercial.classification->>'primary_area' = ANY(category.areas)
      OR commercial.classification->'secondary_areas' ?| category.areas
      OR commercial.classification->'work_types' ?| category.work_types
      OR commercial.classification->'specialties' ?| category.specialties
  ) ELSE COALESCE(title_category.categories, ARRAY[]::text[]) END AS categories,
  CASE WHEN commercial.classification IS NOT NULL THEN 'document'
    WHEN title_category.decision IS NOT NULL THEN 'title' ELSE NULL END AS source,
  title_category.decision
) effective_commercial ON true`;

export const PROD2_COMMERCIAL_SELECT = `${COMMERCIAL_SELECT},
  effective_commercial.categories AS commercial_categories,
  effective_commercial.source AS commercial_classification_source,
  CASE WHEN effective_commercial.source = 'document' THEN CASE WHEN cardinality(effective_commercial.categories) > 0 THEN 'classified' ELSE 'other' END
    ELSE COALESCE(effective_commercial.decision, 'unclassified') END AS commercial_classification_state`;

export function hasCommercialFilters(filters: CommercialFilters | undefined) {
  return Boolean(filters && (filters.categories?.length || filters.areas.length || filters.workTypes.length || filters.specialties.length || filters.statuses.length));
}

export function appendCommercialPredicates(filters: CommercialFilters | undefined, values: unknown[], where: string[], categorySource: "document" | "prod2_effective" = "document") {
  if (!filters) return;
  if (filters.categories?.length) {
    values.push(filters.categories);
    where.push(categorySource === "prod2_effective" ? `effective_commercial.categories && $${values.length}::text[]` : `(CASE WHEN commercial.classification IS NULL THEN false ELSE EXISTS (
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
