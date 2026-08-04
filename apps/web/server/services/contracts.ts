import { query } from "@/server/db/client";
import { pagination } from "@/server/services/crud";
import { keywordFitLateralSql } from "@/server/services/keywordScoring";

export async function getContractForTenant(tenantId: string, idContrato: number) {
  const result = await query(
    `
    SELECT
      c.*,
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
      m.id AS match_id,
      m.score,
      m.verdict,
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
    LEFT JOIN cat_seace_objects obj ON obj.codigo = c.objeto_codigo
    LEFT JOIN cat_seace_states st ON st.codigo = c.estado_codigo
    LEFT JOIN contract_filter_index fi ON fi.id_contrato = c.id_contrato
    LEFT JOIN LATERAL (
      SELECT m.*
      FROM matches m
      JOIN company_profiles cp ON cp.id = m.profile_id
      WHERE cp.tenant_id = $2 AND m.id_contrato = c.id_contrato
      ORDER BY m.score DESC, m.updated_at DESC
      LIMIT 1
    ) m ON true
    LEFT JOIN application_drafts ad ON ad.match_id = m.id
    LEFT JOIN saved_contracts sc ON sc.tenant_id = $2 AND sc.id_contrato = c.id_contrato
    LEFT JOIN LATERAL (
      SELECT tx.summary_json, tx.raw_extraction_json
      FROM contract_documents d
      JOIN tdr_extractions tx ON tx.contract_document_id = d.id AND tx.is_current = true
      WHERE d.id_contrato = c.id_contrato
      ORDER BY tx.created_at DESC
      LIMIT 1
    ) te ON true
    ${keywordFitLateralSql(2)}
    ${ECON_LATERAL_SQL}
    WHERE c.id_contrato = $1
    LIMIT 1
    `,
    [idContrato, tenantId],
  );
  const contract = result.rows[0] ?? null;
  if (!contract) return null;

  const facets = await query(
    `
    SELECT id, facet, label, required, details_json, evidence_json
    FROM requirement_facets
    WHERE id_contrato = $1 AND is_current = true
    ORDER BY facet, id
    `,
    [idContrato],
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
  const values: unknown[] = [tenantId];
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
            AND bl.is_active = true
            AND cardinality(bl.cubso_segmentos) > 0
        )
        OR EXISTS (
          SELECT 1
          FROM company_profiles cp
          JOIN business_lines bl ON bl.profile_id = cp.id
          WHERE cp.tenant_id = $1
            AND cp.is_active = true
            AND bl.is_active = true
            AND c.cubso_segmento = ANY(bl.cubso_segmentos)
        )
      )
    `);
  }
  if (pipelineState) {
    values.push(pipelineState);
    where.push(`c.pipeline_state = $${values.length}`);
  }
  if (hasExtraction === "true") where.push("te.summary_json IS NOT NULL");
  if (hasExtraction === "false") where.push("te.summary_json IS NULL");
  if (verdict) {
    values.push(verdict);
    where.push(`m.verdict = $${values.length}`);
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

  const whereSql = where.length ? `WHERE ${where.join(" AND ")}` : "";
  values.push(limit, offset);

  const result = await query(
    `
    SELECT
      c.id_contrato,
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
      m.id AS match_id,
      m.score,
      m.verdict,
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
    LEFT JOIN cat_seace_objects obj ON obj.codigo = c.objeto_codigo
    LEFT JOIN cat_seace_states st ON st.codigo = c.estado_codigo
    LEFT JOIN contract_filter_index fi ON fi.id_contrato = c.id_contrato
    LEFT JOIN saved_contracts sc ON sc.tenant_id = $1 AND sc.id_contrato = c.id_contrato
    LEFT JOIN LATERAL (
      SELECT tx.summary_json
      FROM contract_documents d
      JOIN tdr_extractions tx ON tx.contract_document_id = d.id AND tx.is_current = true
      WHERE d.id_contrato = c.id_contrato
      ORDER BY tx.created_at DESC
      LIMIT 1
    ) te ON true
    LEFT JOIN LATERAL (
      SELECT m.*
      FROM matches m
      JOIN company_profiles cp ON cp.id = m.profile_id
      WHERE cp.tenant_id = $1 AND m.id_contrato = c.id_contrato
      ORDER BY m.score DESC, m.updated_at DESC
      LIMIT 1
    ) m ON true
    ${keywordFitLateralSql(1)}
    ${ECON_LATERAL_SQL}
    ${whereSql}
    ORDER BY
      ${saved === "true" ? "sc.created_at DESC," : ""}
      COALESCE(fit.keyword_points, 0) DESC,
      fit_points DESC,
      m.score DESC NULLS LAST,
      c.fec_fin_cotizacion ASC NULLS LAST,
      c.fec_publica DESC NULLS LAST
    LIMIT $${values.length - 1}
    OFFSET $${values.length}
    `,
    values,
  );

  const total = Number(result.rows[0]?.total_count ?? 0);
  return {
    data: result.rows.map(({ total_count: _totalCount, ...row }) => row),
    meta: { page, page_size: pageSize, count: result.rows.length, total },
  };
}

export async function getTenantOpportunityContext(tenantId: string) {
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
    ORDER BY bl.created_at NULLS LAST, bl.nombre NULLS LAST
    `,
    [tenantId],
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
