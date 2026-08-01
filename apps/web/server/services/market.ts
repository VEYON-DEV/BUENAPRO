import { query } from "@/server/db/client";

export type MarketFilters = {
  scope: "profile" | "all";
  view: "overview" | "contracts" | "suppliers";
  q: string;
  segment: string;
  result: string;
  department: string;
  entity: string;
  year: string;
  minPrice: string;
  maxPrice: string;
  page: number;
};

export function parseMarketFilters(params: URLSearchParams): MarketFilters {
  const scope = params.get("scope") === "all" ? "all" : "profile";
  const requestedView = params.get("view");
  const view = requestedView === "contracts" || requestedView === "suppliers" ? requestedView : "overview";
  return {
    scope,
    view,
    q: (params.get("q") ?? "").trim().slice(0, 120),
    segment: (params.get("segment") ?? "").replace(/[^0-9]/g, "").slice(0, 4),
    result: ["ADJUDICADO", "DESIERTO", "SIN_RESULTADO"].includes(params.get("result") ?? "")
      ? String(params.get("result"))
      : "",
    department: (params.get("department") ?? "").trim().slice(0, 80),
    entity: (params.get("entity") ?? "").replace(/[^0-9]/g, "").slice(0, 20),
    year: (params.get("year") ?? "").replace(/[^0-9]/g, "").slice(0, 4),
    minPrice: (params.get("min_price") ?? "").replace(/[^0-9.]/g, "").slice(0, 18),
    maxPrice: (params.get("max_price") ?? "").replace(/[^0-9.]/g, "").slice(0, 18),
    page: Math.max(1, Number.parseInt(params.get("page") ?? "1", 10) || 1),
  };
}

function marketQuery(filters: MarketFilters, tenantId: string) {
  const values: unknown[] = [tenantId];
  const conditions: string[] = [];
  const add = (value: unknown) => {
    values.push(value);
    return `$${values.length}`;
  };

  if (filters.scope === "profile") {
    conditions.push("EXISTS (SELECT 1 FROM profile_segments ps WHERE ps.code=h.cubso_segmento)");
  }
  if (filters.q) {
    const ref = add(filters.q);
    conditions.push(`(
      to_tsvector('spanish', coalesce(h.descripcion,'') || ' ' || coalesce(h.cubso_name,''))
        @@ websearch_to_tsquery('spanish', ${ref})
      OR h.codigo_completo ILIKE '%' || ${ref} || '%'
      OR h.entity_name ILIKE '%' || ${ref} || '%'
      OR h.supplier_name ILIKE '%' || ${ref} || '%'
    )`);
  }
  if (filters.segment) conditions.push(`h.cubso_segmento=${add(filters.segment)}`);
  if (filters.result) conditions.push(`h.estado_resultado=${add(filters.result)}`);
  if (filters.department) conditions.push(`h.department=${add(filters.department)}`);
  if (filters.entity) conditions.push(`h.seace_entity_id=${add(Number(filters.entity))}`);
  if (filters.year) conditions.push(`COALESCE(h.codigo_anio, EXTRACT(YEAR FROM h.fec_publica)::int)=${add(Number(filters.year))}`);
  if (filters.minPrice) conditions.push(`h.precio_total>=${add(Number(filters.minPrice))}`);
  if (filters.maxPrice) conditions.push(`h.precio_total<=${add(Number(filters.maxPrice))}`);

  const cte = `
    WITH profile_segments AS (
      SELECT DISTINCT unnest(bl.cubso_segmentos) AS code
      FROM company_profiles cp
      JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
      WHERE cp.tenant_id=$1 AND cp.is_active=true
    ), filtered AS (
      SELECT h.* FROM historical_contract_outcomes h
      ${conditions.length ? `WHERE ${conditions.join(" AND ")}` : ""}
    )
  `;
  return { cte, values };
}

export async function getMarketIntelligence(tenantId: string, filters: MarketFilters) {
  const { cte, values } = marketQuery(filters, tenantId);
  const offset = (filters.page - 1) * 20;
  const [profile, options, summary, trend, regions, entities, suppliers, contracts] = await Promise.all([
    query(
      `SELECT cp.razon_social, array_agg(DISTINCT segment.code ORDER BY segment.code) AS segments
       FROM company_profiles cp
       LEFT JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
       LEFT JOIN LATERAL unnest(bl.cubso_segmentos) segment(code) ON true
       WHERE cp.tenant_id=$1 AND cp.is_active=true GROUP BY cp.id LIMIT 1`,
      [tenantId],
    ),
    query(
      `WITH profile_segments AS (
         SELECT DISTINCT unnest(bl.cubso_segmentos) AS code
         FROM company_profiles cp JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
         WHERE cp.tenant_id=$1 AND cp.is_active=true
       )
       SELECT
         (SELECT json_agg(row_to_json(s) ORDER BY s.code) FROM (
           SELECT h.cubso_segmento AS code, COALESCE(c.nombre, 'Segmento ' || h.cubso_segmento) AS name, count(*)::int AS count
           FROM historical_contract_outcomes h LEFT JOIN cat_cubso_segmentos c ON c.codigo=h.cubso_segmento
           WHERE ($2::boolean OR EXISTS (SELECT 1 FROM profile_segments ps WHERE ps.code=h.cubso_segmento))
           GROUP BY h.cubso_segmento,c.nombre
         ) s) AS segments,
         (SELECT json_agg(row_to_json(d) ORDER BY d.name) FROM (
           SELECT department AS name, count(*)::int AS count FROM historical_contract_outcomes
           WHERE department IS NOT NULL GROUP BY department
         ) d) AS departments,
         (SELECT json_agg(row_to_json(y) ORDER BY y.year DESC) FROM (
           SELECT COALESCE(codigo_anio, EXTRACT(YEAR FROM fec_publica)::int) AS year, count(*)::int AS count
           FROM historical_contract_outcomes GROUP BY 1
         ) y) AS years,
         (SELECT json_agg(row_to_json(e) ORDER BY e.name) FROM (
           SELECT seace_entity_id AS id, entity_name AS name, count(*)::int AS count
           FROM historical_contract_outcomes WHERE seace_entity_id IS NOT NULL
           GROUP BY seace_entity_id,entity_name
         ) e) AS entities`,
      [tenantId, filters.scope === "all"],
    ),
    query(
      `${cte} SELECT count(*)::int AS total,
        count(*) FILTER (WHERE estado_resultado='ADJUDICADO')::int AS adjudicados,
        count(*) FILTER (WHERE estado_resultado='DESIERTO')::int AS desiertos,
        count(DISTINCT supplier_ruc)::int AS suppliers,
        count(DISTINCT seace_entity_id)::int AS entities,
        count(precio_total)::int AS with_price,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY precio_total) FILTER (WHERE precio_total IS NOT NULL) AS median_price,
        percentile_cont(0.25) WITHIN GROUP (ORDER BY precio_total) FILTER (WHERE precio_total IS NOT NULL) AS price_q1,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY precio_total) FILTER (WHERE precio_total IS NOT NULL) AS price_q3
       FROM filtered`,
      values,
    ),
    query(
      `${cte}, months AS (
         SELECT generate_series(date_trunc('month', now())-interval '11 months', date_trunc('month', now()), interval '1 month') AS month_start
       )
       SELECT months.month_start AS month,
         count(f.id_contrato) FILTER (WHERE f.estado_resultado='ADJUDICADO')::int AS adjudicados,
         count(f.id_contrato) FILTER (WHERE f.estado_resultado='DESIERTO')::int AS desiertos
       FROM months LEFT JOIN filtered f ON date_trunc('month', COALESCE(f.fec_fin_cotizacion,f.fec_publica,f.created_at))=months.month_start
       GROUP BY months.month_start ORDER BY months.month_start`,
      values,
    ),
    query(
      `${cte} SELECT department AS name, count(*)::int AS total,
         count(*) FILTER (WHERE estado_resultado='ADJUDICADO')::int AS adjudicados,
         sum(precio_total) AS amount
       FROM filtered WHERE department IS NOT NULL GROUP BY department ORDER BY total DESC LIMIT 8`,
      values,
    ),
    query(
      `${cte} SELECT seace_entity_id AS id, entity_name AS name, count(*)::int AS total,
         count(*) FILTER (WHERE estado_resultado='ADJUDICADO')::int AS adjudicados,
         sum(precio_total) AS amount
       FROM filtered WHERE seace_entity_id IS NOT NULL GROUP BY seace_entity_id,entity_name
       ORDER BY total DESC,amount DESC NULLS LAST LIMIT 8`,
      values,
    ),
    query(
      `${cte} SELECT supplier_ruc AS ruc, supplier_name AS name, count(*)::int AS awards,
         sum(precio_total) AS amount, percentile_cont(0.5) WITHIN GROUP (ORDER BY precio_total) AS median_price,
         count(DISTINCT seace_entity_id)::int AS entities, count(DISTINCT department)::int AS regions
       FROM filtered WHERE estado_resultado='ADJUDICADO' AND supplier_ruc IS NOT NULL
       GROUP BY supplier_ruc,supplier_name ORDER BY awards DESC,amount DESC NULLS LAST LIMIT 20`,
      values,
    ),
    query(
      `${cte} SELECT id_contrato,codigo_completo,descripcion,entity_name,department,cubso_segmento,
         cubso_name,estado_resultado,supplier_ruc,supplier_name,precio_total,fec_publica,source_document_url,
         count(*) OVER()::int AS total_count
       FROM filtered ORDER BY COALESCE(fec_fin_cotizacion,fec_publica,created_at) DESC NULLS LAST
       LIMIT 20 OFFSET ${offset}`,
      values,
    ),
  ]);

  return {
    filters,
    profile: profile.rows[0] ?? null,
    options: options.rows[0] ?? {},
    summary: summary.rows[0] ?? {},
    trend: trend.rows,
    regions: regions.rows,
    entities: entities.rows,
    suppliers: suppliers.rows,
    contracts: contracts.rows,
    total_contracts: Number((contracts.rows[0] as any)?.total_count ?? 0),
  };
}

export async function getHistoricalSupplier(ruc: string) {
  const [supplier, contracts, entities, regions, segments] = await Promise.all([
    query(`SELECT * FROM historical_suppliers WHERE ruc=$1`, [ruc]),
    query(
      `SELECT id_contrato,codigo_completo,descripcion,entity_name,department,cubso_segmento,cubso_name,
         precio_total,fec_publica,source_document_url
       FROM historical_contract_outcomes
       WHERE supplier_ruc=$1 AND estado_resultado='ADJUDICADO'
       ORDER BY COALESCE(fec_fin_cotizacion,fec_publica,created_at) DESC`,
      [ruc],
    ),
    query(`SELECT entity_name AS name,count(*)::int AS count,sum(precio_total) AS amount FROM historical_contract_outcomes WHERE supplier_ruc=$1 GROUP BY entity_name ORDER BY count DESC LIMIT 8`, [ruc]),
    query(`SELECT department AS name,count(*)::int AS count FROM historical_contract_outcomes WHERE supplier_ruc=$1 AND department IS NOT NULL GROUP BY department ORDER BY count DESC LIMIT 8`, [ruc]),
    query(`SELECT cubso_segmento AS code,COALESCE(cubso_name,'Segmento '||cubso_segmento) AS name,count(*)::int AS count FROM historical_contract_outcomes WHERE supplier_ruc=$1 GROUP BY cubso_segmento,cubso_name ORDER BY count DESC LIMIT 8`, [ruc]),
  ]);
  if (!supplier.rows[0]) return null;
  const prices = contracts.rows.map((row: any) => Number(row.precio_total)).filter((value) => value > 0).sort((a, b) => a - b);
  return {
    supplier: supplier.rows[0],
    contracts: contracts.rows,
    entities: entities.rows,
    regions: regions.rows,
    segments: segments.rows,
    median_price: prices.length ? prices[Math.floor(prices.length / 2)] : null,
  };
}
