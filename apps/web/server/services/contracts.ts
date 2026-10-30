import { query } from "@/server/db/client";
import { pagination } from "@/server/services/crud";
import { keywordFitLateralSql } from "@/server/services/keywordScoring";
import { CommercialFilterError, deriveCommercialCategories, normalizeCommercialClassification, parseCommercialFilters } from "@/lib/commercialTaxonomy";
import { appendCommercialPredicates, COMMERCIAL_SELECT, PROD6_COMMERCIAL_JOIN } from "./commercialClassification";

// Read the current document extraction; canonical participation may belong to another source or revision.
const PARTICIPATION_EXTRACTION = `jsonb_build_object(
  'id', tx.id::text,
  'quality', tx.quality,
  'requires_human_review', tx.requires_human_review,
  'participation', COALESCE(NULLIF(tx.summary_json->'participation', 'null'::jsonb), tx.raw_extraction_json->'participation'),
  'page_scope', 'document'
) AS participation_extraction`;

// Unvalidated scores remain separate from approved scores; the requested
// provisional projection exposes only the existing stored number with review.
// Legacy evaluations without the flag keep their existing behavior.
const PUBLISHED_MATCH_SCORE = "CASE WHEN m.breakdown_json->'meta'->>'score_validated' = 'false' THEN NULL ELSE m.score END";
const PUBLISHED_MATCH_VERDICT = "CASE WHEN m.breakdown_json->'meta'->>'score_validated' = 'false' THEN 'gris' ELSE m.verdict END";
const MATCH_REVIEW_REQUIRED = "COALESCE(m.breakdown_json->'meta'->>'score_validated' = 'false', false)";
const PROVISIONAL_MATCH_SCORE = `CASE WHEN ${MATCH_REVIEW_REQUIRED} AND m.score >= 0 AND m.score <= 100 THEN m.score ELSE NULL END`;
const AVAILABLE_MATCH_SCORE = "CASE WHEN m.id IS NOT NULL AND m.score BETWEEN 0 AND 100 THEN m.score ELSE NULL END";

export class ContractRequestError extends Error {
  constructor(message: string, public status = 400) { super(message); }
}

async function resolveListProfile(tenantId: string, profileId: string | null) {
  if (profileId && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(profileId)) {
    throw new ContractRequestError("profile_id must be a UUID");
  }
  const result = await query<{ id: string; razon_social: string }>(
    `SELECT id::text, razon_social FROM company_profiles
     WHERE tenant_id = $1 AND is_active = true AND ($2::uuid IS NULL OR id = $2::uuid)
     ORDER BY created_at, id LIMIT 1`, [tenantId, profileId]);
  if (profileId && !result.rows[0]) throw new ContractRequestError("Perfil no disponible en tu empresa.", 404);
  return result.rows[0] ?? null;
}

function provisionalScore(row: Record<string, unknown>) {
  return row.match_id != null && row.match_review_required === true && typeof row.provisional_score === "number"
    && Number.isFinite(row.provisional_score) && row.provisional_score >= 0 && row.provisional_score <= 100
    ? row.provisional_score : null;
}

export async function getContractForTenant(tenantId: string, idContrato: number, profileId: string | null = null) {
  const profile = await resolveListProfile(tenantId, profileId);
  const result = await query(
    `
    SELECT
      c.*,
      'seace_prod6'::text AS source_key,
      o.record_kind,
      o.object_type,
      o.procurement_method,
      o.lifecycle_stage,
      o.participation_access,
      o.actionability,
      o.consortium_status,
      o.subcontracting_status,
      o.participation_terms_json,
      obj.nombre AS objeto_nombre,
      st.nombre AS estado_nombre,
      econ.exigido AS econ_exigido,
      COALESCE(fit.fit_points, 0)::int AS fit_points,
      fit.business_line_id AS fit_business_line_id,
      fit.business_line_name AS fit_business_line_name,
      COALESCE(fit.keyword_hits, '[]'::jsonb) AS fit_keyword_hits,
      fi.tipo_pago,
      fi.penalidad_tope_pct,
      fi.entregables_count,
      fi.roles_requeridos,
      fi.facets AS filter_facets,
      te.summary_json,
      te.raw_extraction_json,
      te.extraction_id,
      te.participation_extraction,
      ${COMMERCIAL_SELECT},
      m.id AS match_id,
      ${MATCH_REVIEW_REQUIRED} AS match_review_required,
      ${PROVISIONAL_MATCH_SCORE} AS provisional_score,
      $3::uuid::text AS profile_id,
      ${PUBLISHED_MATCH_SCORE} AS score,
      ${PUBLISHED_MATCH_VERDICT} AS verdict,
      m.user_state,
      m.responsable_id,
      m.monto_ofertado,
      m.notas,
      m.breakdown_json,
      m.missing_actions_json,
      ad.id AS application_id,
      (sc.id_contrato IS NOT NULL) AS is_saved,
      sc.created_at AS saved_at
    FROM seace_contracts c
    LEFT JOIN opportunities o ON o.id = c.opportunity_id
    LEFT JOIN cat_seace_objects obj ON obj.codigo = c.objeto_codigo
    LEFT JOIN cat_seace_states st ON st.codigo = c.estado_codigo
    LEFT JOIN contract_filter_index fi ON fi.id_contrato = c.id_contrato
    LEFT JOIN LATERAL (
      SELECT m.*
      FROM matches m
      JOIN company_profiles cp ON cp.id = m.profile_id
      WHERE cp.tenant_id = $2 AND m.id_contrato = c.id_contrato
        AND cp.is_active = true AND cp.id = $3::uuid
      ORDER BY ${PUBLISHED_MATCH_SCORE} DESC NULLS LAST, m.updated_at DESC
      LIMIT 1
    ) m ON true
    LEFT JOIN application_drafts ad ON ad.match_id = m.id
    LEFT JOIN saved_contracts sc ON sc.tenant_id = $2 AND sc.id_contrato = c.id_contrato
    LEFT JOIN LATERAL (
      SELECT tx.id AS extraction_id, tx.summary_json, tx.raw_extraction_json, ${PARTICIPATION_EXTRACTION}
      FROM contract_documents d
      JOIN tdr_extractions tx ON tx.contract_document_id = d.id AND tx.is_current = true
      WHERE d.id_contrato = c.id_contrato
      ORDER BY tx.created_at DESC, tx.id DESC
      LIMIT 1
    ) te ON true
    ${keywordFitLateralSql(2, 3)}
    ${ECON_LATERAL_SQL}
    ${PROD6_COMMERCIAL_JOIN}
    WHERE c.id_contrato = $1
    LIMIT 1
    `,
    [idContrato, tenantId, profile?.id ?? null],
  );
  const contract: Record<string, any> | null = result.rows[0] ? { ...result.rows[0], provisional_score: provisionalScore(result.rows[0]),
    commercial_classification: normalizeCommercialClassification(result.rows[0].commercial_classification),
    commercial_categories: deriveCommercialCategories(result.rows[0].commercial_classification) } : null;
  if (!contract) return null;

  const facets = await query(
    `
    SELECT id, facet, label, required, details_json, evidence_json
    FROM requirement_facets
    WHERE id_contrato = $1 AND extraction_id = $2 AND is_current = true
    ORDER BY facet, id
    `,
    [idContrato, contract.extraction_id],
  );
  const documents = await query(
    `
    SELECT id, id_contrato_archivo, filename, mime, doc_class, size_original_bytes, r2_preview_key, seace_download_url
    FROM contract_documents
    WHERE id_contrato = $1
    ORDER BY id
    `,
    [idContrato],
  );
  return { contract, facets: facets.rows, documents: documents.rows };
}

const ECON_LATERAL_SQL = `
  LEFT JOIN LATERAL (
    SELECT max((rf.details_json->>'monto')::numeric) AS exigido
    FROM requirement_facets rf
    WHERE rf.id_contrato = c.id_contrato
      AND rf.facet = 'economic_experience'
      AND rf.is_current = true
      AND rf.details_json->>'monto' ~ '^[0-9]+\\.?[0-9]*$'
  ) econ ON true
`;

export async function listContractsForTenant(tenantId: string, params: URLSearchParams) {
  const { limit, offset, page, pageSize } = pagination(params);
  let commercial;
  try { commercial = parseCommercialFilters(params); }
  catch (error) { if (error instanceof CommercialFilterError) throw new ContractRequestError(error.message); throw error; }
  const withScore = params.get("with_score");
  if (withScore && withScore !== "true" && withScore !== "false") throw new ContractRequestError("with_score must be true or false");
  const profile = await resolveListProfile(tenantId, params.get("profile_id"));
  const values: unknown[] = [tenantId, profile?.id ?? null];
  const where: string[] = [];

  const q = params.get("q");
  const objeto = params.get("objeto");
  const estado = params.get("estado");
  const segmento = params.get("segmento");
  const region = params.get("region");
  const bucket = params.get("bucket");
  const pipelineState = params.get("pipeline_state");
  const hasExtraction = params.get("has_extraction");
  const verdict = params.get("verdict");
  const cotizar = params.get("cotizar");
  const closingBefore = params.get("closing_before");
  const openOnly = params.get("open_only");
  const hasAmount = params.get("has_amount");
  const saved = params.get("saved");
  const analysis = params.get("analysis");

  if (q) {
    values.push(`%${q}%`);
    where.push(`(c.codigo ILIKE $${values.length} OR c.descripcion ILIKE $${values.length} OR c.entidad_nombre ILIKE $${values.length})`);
  }
  if (objeto) {
    values.push(Number(objeto));
    where.push(`c.objeto_codigo = $${values.length}`);
  }
  if (estado) {
    values.push(Number(estado));
    where.push(`c.estado_codigo = $${values.length}`);
  }
  if (segmento) {
    values.push(segmento);
    where.push(`c.cubso_segmento = $${values.length}`);
  }
  if (region) {
    values.push(region);
    where.push(`c.departamento ILIKE $${values.length}`);
  }
  if (bucket) {
    values.push(bucket);
    where.push(`
      EXISTS (
        SELECT 1
        FROM mvp_enabled_cubso_segments ecs
        WHERE ecs.codigo = c.cubso_segmento
          AND ecs.anio = c.anio
          AND ecs.bucket = $${values.length}
          AND ecs.enabled = true
      )
    `);
  }
  if (!bucket && !segmento) {
    where.push(`
      (
        NOT EXISTS (
          SELECT 1
          FROM company_profiles cp
          JOIN business_lines bl ON bl.profile_id = cp.id
          WHERE cp.tenant_id = $1
            AND cp.is_active = true
            AND cp.id = $2::uuid
            AND bl.is_active = true
            AND cardinality(bl.cubso_segmentos) > 0
        )
        OR EXISTS (
          SELECT 1
          FROM company_profiles cp
          JOIN business_lines bl ON bl.profile_id = cp.id
          WHERE cp.tenant_id = $1
            AND cp.is_active = true
            AND cp.id = $2::uuid
            AND bl.is_active = true
            AND c.cubso_segmento = ANY(bl.cubso_segmentos)
        )
        -- Found by the nationwide keyword search (segment '0', unknown) and accepted for this company.
        OR (c.cubso_segmento = '0' AND EXISTS (
          SELECT 1 FROM prod6_profile_decisions d
          WHERE d.id_contrato = c.id_contrato AND d.profile_id = $2::uuid AND d.decision = 'accepted'
        ))
      )
    `);
  }
  if (pipelineState) {
    values.push(pipelineState);
    where.push(`c.pipeline_state = $${values.length}`);
  }
  if (hasExtraction === "true") where.push("te.summary_json IS NOT NULL");
  if (hasExtraction === "false") where.push("te.summary_json IS NULL");
  // Analysis availability is independent of publishing its numeric score.
  if (analysis === "done") where.push("m.id IS NOT NULL");
  if (withScore === "true") where.push(`${AVAILABLE_MATCH_SCORE} IS NOT NULL`);
  if (verdict) {
    values.push(verdict);
    where.push(`${PUBLISHED_MATCH_VERDICT} = $${values.length}`);
  }
  if (cotizar === "true") where.push("c.cotizar = true");
  if (cotizar === "false") where.push("c.cotizar = false");
  if (openOnly === "true") where.push("(c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion >= now())");
  if (openOnly === "false") where.push("c.fec_fin_cotizacion < now()");
  if (hasAmount === "true") where.push("c.valor_estimado IS NOT NULL");
  if (hasAmount === "false") where.push("c.valor_estimado IS NULL");
  if (saved === "true") where.push("sc.id_contrato IS NOT NULL");
  if (saved === "false") where.push("sc.id_contrato IS NULL");
  if (closingBefore) {
    values.push(closingBefore);
    where.push(`c.fec_fin_cotizacion <= $${values.length}::timestamptz`);
  }
  appendCommercialPredicates(commercial, values, where);

  const whereSql = where.length ? `WHERE ${where.join(" AND ")}` : "";
  values.push(limit, offset);

  const result = await query(
    `
    SELECT
      c.id_contrato,
      c.opportunity_id,
      'seace_prod6'::text AS source_key,
      o.record_kind,
      o.object_type,
      o.procurement_method,
      o.lifecycle_stage,
      o.participation_access,
      o.actionability,
      c.codigo,
      c.anio,
      c.entidad_nombre,
      c.objeto_codigo,
      obj.nombre AS objeto_nombre,
      c.estado_codigo,
      st.nombre AS estado_nombre,
      c.descripcion,
      c.cubso_segmento,
      c.cubso_item,
      c.departamento,
      c.provincia,
      c.distrito,
      c.valor_estimado,
      c.valor_no_informado,
      c.cotizar,
      c.fec_publica,
      c.fec_ini_cotizacion,
      c.fec_fin_cotizacion,
      c.pipeline_state,
      fi.tipo_pago,
      fi.penalidad_tope_pct,
      fi.entregables_count,
      fi.roles_requeridos,
      fi.facets,
      te.summary_json,
      te.participation_extraction,
      ${COMMERCIAL_SELECT},
      m.id AS match_id,
      m.profile_id::text AS profile_id,
      ${MATCH_REVIEW_REQUIRED} AS match_review_required,
      ${PROVISIONAL_MATCH_SCORE} AS provisional_score,
      ${PUBLISHED_MATCH_SCORE} AS score,
      ${PUBLISHED_MATCH_VERDICT} AS verdict,
      m.user_state,
      econ.exigido AS econ_exigido,
      COALESCE(fit.fit_points, 0)::int AS fit_points,
      fit.business_line_id AS fit_business_line_id,
      fit.business_line_name AS fit_business_line_name,
      COALESCE(fit.keyword_hits, '[]'::jsonb) AS fit_keyword_hits,
      (sc.id_contrato IS NOT NULL) AS is_saved,
      sc.created_at AS saved_at,
      count(*) OVER()::int AS total_count
    FROM seace_contracts c
    LEFT JOIN opportunities o ON o.id = c.opportunity_id
    LEFT JOIN cat_seace_objects obj ON obj.codigo = c.objeto_codigo
    LEFT JOIN cat_seace_states st ON st.codigo = c.estado_codigo
    LEFT JOIN contract_filter_index fi ON fi.id_contrato = c.id_contrato
    LEFT JOIN saved_contracts sc ON sc.tenant_id = $1 AND sc.id_contrato = c.id_contrato
    LEFT JOIN LATERAL (
      SELECT tx.summary_json, ${PARTICIPATION_EXTRACTION}
      FROM contract_documents d
      JOIN tdr_extractions tx ON tx.contract_document_id = d.id AND tx.is_current = true
      WHERE d.id_contrato = c.id_contrato
      ORDER BY tx.created_at DESC, tx.id DESC
      LIMIT 1
    ) te ON true
    LEFT JOIN LATERAL (
      SELECT m.*
      FROM matches m
      JOIN company_profiles cp ON cp.id = m.profile_id
      WHERE cp.tenant_id = $1 AND m.id_contrato = c.id_contrato
        AND cp.is_active = true AND cp.id = $2::uuid
      ORDER BY ${PUBLISHED_MATCH_SCORE} DESC NULLS LAST, m.updated_at DESC
      LIMIT 1
    ) m ON true
    ${keywordFitLateralSql(1, 2)}
    ${ECON_LATERAL_SQL}
    ${PROD6_COMMERCIAL_JOIN}
    ${whereSql}
    ORDER BY
      ${AVAILABLE_MATCH_SCORE} DESC NULLS LAST,
      ${saved === "true" ? "sc.created_at DESC," : ""}
      COALESCE(fit.keyword_points, 0) DESC,
      fit_points DESC,
      ${PUBLISHED_MATCH_SCORE} DESC NULLS LAST,
      c.fec_fin_cotizacion ASC NULLS LAST,
      c.fec_publica DESC NULLS LAST,
      c.id_contrato DESC
    LIMIT $${values.length - 1}
    OFFSET $${values.length}
    `,
    values,
  );

  const total = Number(result.rows[0]?.total_count ?? 0);
  return {
    data: result.rows.map(({ total_count: _totalCount, ...row }) => ({ ...row, provisional_score: provisionalScore(row),
      commercial_classification: normalizeCommercialClassification(row.commercial_classification),
      commercial_categories: deriveCommercialCategories(row.commercial_classification) })),
    meta: { page, page_size: pageSize, count: result.rows.length, total },
    profile: profile ? { id: profile.id, name: profile.razon_social } : null,
  };
}

export async function getTenantOpportunityContext(tenantId: string, profileId: string | null = null) {
  const result = await query(
    `
    SELECT
      cp.razon_social,
      bl.nombre,
      bl.cubso_segmentos,
      bl.keywords
    FROM company_profiles cp
    LEFT JOIN business_lines bl ON bl.profile_id = cp.id AND bl.is_active = true
    WHERE cp.tenant_id = $1
      AND cp.is_active = true
      AND ($2::uuid IS NULL OR cp.id = $2::uuid)
    ORDER BY bl.created_at NULLS LAST, bl.nombre NULLS LAST
    `,
    [tenantId, profileId],
  );

  const first = result.rows[0];
  return {
    razon_social: first?.razon_social ?? "Tu empresa",
    lines: result.rows
      .filter((row) => row.nombre)
      .map((row) => ({
        nombre: row.nombre as string,
        cubso_segmentos: (row.cubso_segmentos ?? []) as string[],
        keywords: (row.keywords ?? []) as string[],
      })),
  };
}

export async function getOriginalDocumentUrl(idContrato: number, docId: number) {
  const result = await query(
    `
    SELECT seace_download_url
    FROM contract_documents
    WHERE id_contrato = $1 AND id = $2
    `,
    [idContrato, docId],
  );
  return result.rows[0]?.seace_download_url as string | undefined;
}

export async function getDocumentSource(idContrato: number, docId: number) {
  const result = await query<{ seace_download_url: string; mime: string | null; filename: string }>(
    `SELECT seace_download_url,mime,filename FROM contract_documents WHERE id_contrato=$1 AND id=$2`,
    [idContrato, docId],
  );
  return result.rows[0] ?? null;
}
