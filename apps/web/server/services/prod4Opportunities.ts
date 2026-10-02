import { query } from "@/server/db/client";
import { normalizeProd4Schedule } from "@/lib/procurementSchedule";
import { buildProd4Analysis, type Prod4Extraction, type Prod4RequirementFacet, type Prod4DocumentMatch } from "@/lib/prod4Analysis";

type SourceWindowState = "current" | "exited" | "all";

export type Prod4ListParams = {
  object: "good" | "service" | null;
  q: string;
  state: SourceWindowState;
  page: number;
  pageSize: number;
};

export function parseProd4ListParams(params: URLSearchParams): Prod4ListParams {
  const object = params.get("object");
  if (object && object !== "good" && object !== "service") {
    throw new Error("object must be good or service");
  }
  const state = params.get("state") || "current";
  if (state !== "current" && state !== "exited" && state !== "all") {
    throw new Error("state must be current, exited or all");
  }
  const q = (params.get("q") || "").trim();
  if (q.length > 120) throw new Error("q must be 120 characters or fewer");
  const page = Number(params.get("page") || "1");
  const pageSize = Number(params.get("page_size") || "20");
  if (!Number.isSafeInteger(page) || page < 1) throw new Error("page must be a positive integer");
  if (!Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 100) {
    throw new Error("page_size must be between 1 and 100");
  }
  return { object: object as Prod4ListParams["object"], q, state, page, pageSize };
}

const BASE_SELECT = `
  p.id_procedimiento::text AS id_procedimiento,
  p.opportunity_id,
  'seace_prod4'::text AS source_key,
  p.nomenclatura,
  p.title,
  p.description,
  p.object_type,
  p.procedure_type,
  p.buyer_name,
  p.region,
  p.published_at,
  p.registration_closes_at,
  p.proposals_start_at,
  p.proposals_closes_at,
  p.reference_amount,
  p.currency,
  p.source_url,
  p.technology_match_reason,
  p.last_seen_at,
  p.missing_since,
  CASE WHEN p.missing_since IS NULL THEN 'current' ELSE 'exited' END AS source_window_status,
  o.record_kind,
  o.procurement_method,
  o.lifecycle_stage,
  o.participation_access,
  o.actionability,
  (SELECT count(*)::int FROM prod4_items i WHERE i.id_procedimiento = p.id_procedimiento) AS items_count,
  (SELECT count(*)::int FROM prod4_documents d WHERE d.id_procedimiento = p.id_procedimiento) AS documents_count,
  COALESCE((
    SELECT array_agg(DISTINCT left(i.cubso_code, 2))
    FROM prod4_items i
    WHERE i.id_procedimiento = p.id_procedimiento
      AND i.cubso_code ~ '^[0-9]{2}'
  ), ARRAY[]::text[]) AS technology_segments
`;

function buildListWhere(params: Prod4ListParams) {
  const values: unknown[] = [];
  const where = ["p.technology_relevant = true"];
  if (params.object) {
    values.push(params.object);
    where.push(`p.object_type = $${values.length}`);
  }
  if (params.state !== "all") {
    where.push(params.state === "current" ? "p.missing_since IS NULL" : "p.missing_since IS NOT NULL");
  }
  if (params.q) {
    values.push(`%${params.q}%`);
    where.push(`(p.nomenclatura ILIKE $${values.length} OR p.title ILIKE $${values.length} OR p.description ILIKE $${values.length} OR p.buyer_name ILIKE $${values.length})`);
  }
  return { values, whereSql: where.join(" AND ") };
}

export async function listProd4Opportunities(params: Prod4ListParams) {
  const { values, whereSql } = buildListWhere(params);
  const count = await query<{ total: number }>(
    `SELECT count(*)::int AS total FROM prod4_processes p WHERE ${whereSql}`,
    values,
  );
  const offset = (params.page - 1) * params.pageSize;
  const rows = await query(
    `SELECT ${BASE_SELECT}
     FROM prod4_processes p
     JOIN opportunities o ON o.id = p.opportunity_id
     WHERE ${whereSql}
     ORDER BY p.proposals_closes_at ASC NULLS LAST, p.published_at DESC NULLS LAST, p.id_procedimiento DESC
     LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
    [...values, params.pageSize, offset],
  );
  return {
    data: rows.rows,
    total: count.rows[0]?.total ?? 0,
    page: params.page,
    page_size: params.pageSize,
  };
}

// The same source window is visible to every tenant, but fit is calculated
// against each tenant's active profile. An unmatched segment has no fit level.
export async function listProd4OpportunitiesForTenant(tenantId: string, params: Prod4ListParams) {
  const { values, whereSql } = buildListWhere(params);
  const count = await query<{ total: number }>(
    `SELECT count(*)::int AS total FROM prod4_processes p WHERE ${whereSql}`,
    values,
  );
  const offset = (params.page - 1) * params.pageSize;
  const tenantParam = values.length + 1;
  const rows = await query(
    `SELECT ${BASE_SELECT},
       fit.fit_points,
       fit.fit_score,
       fit.fit_level,
       fit.business_line_id AS fit_business_line_id,
       fit.business_line_name,
       COALESCE(fit.keyword_hits, '[]'::jsonb) AS keyword_hits,
       matched.score AS match_score,
       matched.verdict AS match_verdict
     FROM prod4_processes p
     JOIN opportunities o ON o.id = p.opportunity_id
     LEFT JOIN LATERAL (
       SELECT score.*
       FROM company_profiles cp
       CROSS JOIN LATERAL profile_prod4_fit(cp.id, p.id_procedimiento) score
       WHERE cp.tenant_id = $${tenantParam} AND cp.is_active = true
       ORDER BY score.keyword_points DESC, score.business_line_id
       LIMIT 1
     ) fit ON true
     LEFT JOIN LATERAL (
       SELECT m.score, m.verdict
       FROM opportunity_matches m
       JOIN company_profiles cp ON cp.id = m.profile_id
       WHERE cp.tenant_id = $${tenantParam}
         AND cp.is_active = true
         AND m.opportunity_id = p.opportunity_id
       ORDER BY m.updated_at DESC, m.score DESC
       LIMIT 1
     ) matched ON true
     WHERE ${whereSql}
     ORDER BY fit.keyword_points DESC NULLS LAST,
              fit.fit_points DESC NULLS LAST,
              p.proposals_closes_at ASC NULLS LAST,
              p.published_at DESC NULLS LAST,
              p.id_procedimiento DESC
     LIMIT $${tenantParam + 1} OFFSET $${tenantParam + 2}`,
    [...values, tenantId, params.pageSize, offset],
  );
  return {
    data: rows.rows,
    total: count.rows[0]?.total ?? 0,
    page: params.page,
    page_size: params.pageSize,
  };
}

export async function getProd4Opportunity(id: string) {
  const processResult = await query(
    `SELECT ${BASE_SELECT}, p.id_convocatoria_pub::text AS id_convocatoria_pub,
            p.numero_procedimiento, p.buyer_id, p.detail_fetched_at, p.schedule_fetched_at,
            p.raw_detail->'listaCronograma' AS official_schedule_raw
     FROM prod4_processes p
     JOIN opportunities o ON o.id = p.opportunity_id
     WHERE p.id_procedimiento = $1 AND p.technology_relevant = true`,
    [id],
  );
  if (!processResult.rows[0]) return null;
  const [items, documents, schedule] = await Promise.all([
    query(
      `SELECT nro_item, cubso_code, description, quantity, unit
       FROM prod4_items WHERE id_procedimiento = $1 ORDER BY nro_item`,
      [id],
    ),
    query(
      `SELECT codigo_alfresco, name, document_type, extension, published_at, source_url
       FROM prod4_documents WHERE id_procedimiento = $1 ORDER BY published_at DESC NULLS LAST, name`,
      [id],
    ),
    query(
      `SELECT position, stage_key, stage_name, starts_at, ends_at
       FROM prod4_schedule WHERE id_procedimiento = $1 ORDER BY position`,
      [id],
    ),
  ]);
  const { official_schedule_raw, ...process } = processResult.rows[0];
  return {
    process,
    items: items.rows,
    documents: documents.rows,
    schedule: schedule.rows,
    official_schedule: normalizeProd4Schedule(official_schedule_raw),
  };
}

export async function getProd4OpportunityForTenant(tenantId: string, id: string) {
  const detail = await getProd4Opportunity(id);
  if (!detail) return null;
  const affinity = await query(
    `SELECT fit.fit_points, fit.fit_score, fit.fit_level,
            fit.business_line_id AS fit_business_line_id,
            fit.business_line_name,
            COALESCE(fit.keyword_hits, '[]'::jsonb) AS keyword_hits,
            matched.score AS match_score,
            matched.verdict AS match_verdict
     FROM prod4_processes p
     LEFT JOIN LATERAL (
       SELECT score.*
       FROM company_profiles cp
       CROSS JOIN LATERAL profile_prod4_fit(cp.id, p.id_procedimiento) score
       WHERE cp.tenant_id = $1 AND cp.is_active = true
       ORDER BY score.keyword_points DESC, score.business_line_id
       LIMIT 1
     ) fit ON true
     LEFT JOIN LATERAL (
       SELECT m.score, m.verdict
       FROM opportunity_matches m
       JOIN company_profiles cp ON cp.id = m.profile_id
       WHERE cp.tenant_id = $1
         AND cp.is_active = true
         AND m.opportunity_id = p.opportunity_id
       ORDER BY m.updated_at DESC, m.score DESC
       LIMIT 1
     ) matched ON true
     WHERE p.id_procedimiento = $2::bigint`,
    [tenantId, id],
  );
  const tenantFit = affinity.rows[0] ?? {
    fit_points: null,
    fit_score: null,
    fit_level: null,
    fit_business_line_id: null,
    business_line_name: null,
    keyword_hits: [],
    match_score: null,
    match_verdict: null,
  };
  const [analysisState, extractionResult, facetsResult, matchResult] = await Promise.all([
    query<{ document_analysis_status: string | null; document_analysis_reason: string | null; checked_at: string | null }>(
      `SELECT document_analysis_status, document_analysis_reason,
              document_analysis_checked_at::text AS checked_at
       FROM prod4_processes WHERE id_procedimiento = $1::bigint`, [id],
    ),
    query<Prod4Extraction>(
      `SELECT id::text, codigo_alfresco::text, summary_json, raw_extraction_json,
              model, prompt_version, schema_version, quality, requires_human_review,
              facet_count, created_at::text
       FROM prod4_document_extractions
       WHERE id_procedimiento = $1::bigint AND is_current = true
       ORDER BY created_at DESC, id DESC LIMIT 1`, [id],
    ),
    query<Prod4RequirementFacet>(
      `SELECT f.id::text, f.facet, f.label, f.required, f.details_json, f.evidence_json
       FROM prod4_requirement_facets f
       JOIN prod4_document_extractions e ON e.id = f.extraction_id AND e.is_current = true
       WHERE f.id_procedimiento = $1::bigint AND f.is_current = true
       ORDER BY f.id`, [id],
    ),
    query<Prod4DocumentMatch>(
      `SELECT m.id::text, m.profile_id::text, m.score, m.verdict,
              m.breakdown_json, m.missing_actions_json,
              m.matched_at::text, m.updated_at::text
       FROM opportunity_matches m
       JOIN company_profiles cp ON cp.id = m.profile_id
       JOIN prod4_processes p ON p.opportunity_id = m.opportunity_id
       WHERE cp.tenant_id = $1 AND cp.is_active = true
         AND p.id_procedimiento = $2::bigint
       ORDER BY m.updated_at DESC, m.score DESC, m.id DESC LIMIT 1`, [tenantId, id],
    ),
  ]);
  const state = analysisState.rows[0];
  const analysis = buildProd4Analysis({
    sourceStatus: state?.document_analysis_status ?? null,
    reason: state?.document_analysis_reason ?? null,
    checkedAt: state?.checked_at ?? null,
    extraction: extractionResult.rows[0] ?? null,
    facets: facetsResult.rows,
    match: matchResult.rows[0] ?? null,
  });
  return { ...detail, process: { ...detail.process, ...tenantFit }, analysis };
}
