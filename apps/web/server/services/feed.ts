import { query } from "@/server/db/client";

export async function getFeed(tenantId: string, params: URLSearchParams) {
  const verdict = params.get("verdict");
  const q = params.get("q");
  const objeto = params.get("objeto");
  const estado = params.get("estado");
  const segmento = params.get("segmento");
  const bucket = params.get("bucket");
  const region = params.get("region");
  const tipoPago = params.get("tipo_pago");
  const role = params.get("role");
  const facet = params.get("facet");
  const closingBefore = params.get("closing_before");
  const openOnly = params.get("open_only");
  const hasAmount = params.get("has_amount");
  const saved = params.get("saved");
  const page = Math.max(Number(params.get("page") ?? "1"), 1);
  const limit = Math.min(Math.max(Number(params.get("page_size") ?? "20"), 1), 100);
  const offset = (page - 1) * limit;
  const values: unknown[] = [tenantId];
  const where = ["cp.tenant_id = $1"];

  if (verdict) {
    values.push(verdict);
    where.push(`m.verdict = $${values.length}`);
  }
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
      EXISTS (
        SELECT 1
        FROM business_lines bl
        WHERE bl.profile_id = cp.id
          AND bl.is_active = true
          AND c.cubso_segmento = ANY(bl.cubso_segmentos)
      )
    `);
  }
  if (region) {
    values.push(region);
    where.push(`c.departamento ILIKE $${values.length}`);
  }
  if (tipoPago) {
    values.push(tipoPago);
    where.push(`fi.tipo_pago = $${values.length}`);
  }
  if (role) {
    values.push(role);
    where.push(`fi.roles_requeridos @> ARRAY[$${values.length}]::text[]`);
  }
  if (facet) {
    values.push(facet);
    where.push(`fi.facets @> ARRAY[$${values.length}]::text[]`);
  }
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

  values.push(limit, offset);
  const result = await query(
    `
    SELECT
      m.id AS match_id,
      m.score,
      m.verdict,
      m.user_state,
      c.id_contrato,
      c.codigo,
      c.entidad_nombre,
      c.descripcion,
      c.estado_codigo,
      st.nombre AS estado_nombre,
      c.objeto_codigo,
      obj.nombre AS objeto_nombre,
      c.cubso_segmento,
      c.departamento,
      c.provincia,
      c.valor_estimado,
      c.valor_no_informado,
      c.cotizar,
      c.fec_fin_cotizacion,
      fi.tipo_pago,
      fi.roles_requeridos,
      fi.facets,
      te.summary_json,
      (sc.id_contrato IS NOT NULL) AS is_saved,
      sc.created_at AS saved_at,
      count(*) OVER()::int AS total_count
    FROM matches m
    JOIN company_profiles cp ON cp.id = m.profile_id
    JOIN seace_contracts c ON c.id_contrato = m.id_contrato
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
    WHERE ${where.join(" AND ")}
    ORDER BY ${saved === "true" ? "sc.created_at DESC," : ""} m.score DESC, c.fec_fin_cotizacion ASC NULLS LAST
    LIMIT $${values.length - 1}
    OFFSET $${values.length}
    `,
    values,
  );
  const total = Number(result.rows[0]?.total_count ?? 0);
  return {
    data: result.rows.map(({ total_count: _totalCount, ...row }) => row),
    meta: { page, page_size: limit, count: result.rows.length, total },
  };
}
