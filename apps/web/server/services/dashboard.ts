import { query } from "@/server/db/client";

export async function getDashboard(tenantId: string) {
  const [profile, actions, actionSummary, tracking, trackingSummary, market, marketTrend, suppliers] = await Promise.all([
    query(`SELECT id, razon_social FROM company_profiles WHERE tenant_id=$1 AND is_active=true LIMIT 1`, [tenantId]),
    query(
      `
      SELECT c.id_contrato, c.codigo, c.descripcion, c.entidad_nombre, c.fec_fin_cotizacion,
        COALESCE(fit.keyword_points, 0)::int AS fit_points
      FROM seace_contracts c
      LEFT JOIN LATERAL (
        SELECT greatest(
          COALESCE((SELECT count(*) * 15 FROM unnest(bl.keyword_phrases) phrase
            WHERE to_tsvector('spanish', c.descripcion) @@ phraseto_tsquery('spanish', phrase)), 0),
          COALESCE((SELECT count(*) * 8 FROM unnest(bl.keyword_terms) term
            WHERE to_tsvector('spanish', c.descripcion) @@ plainto_tsquery('spanish', term)), 0)
        )::int AS keyword_points
        FROM company_profiles cp JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
        WHERE cp.tenant_id=$1 AND cp.is_active=true AND c.cubso_segmento=ANY(bl.cubso_segmentos)
        ORDER BY keyword_points DESC LIMIT 1
      ) fit ON true
      WHERE c.estado_codigo=2 AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion >= now())
        AND EXISTS (
          SELECT 1 FROM company_profiles cp JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
          WHERE cp.tenant_id=$1 AND cp.is_active=true AND c.cubso_segmento=ANY(bl.cubso_segmentos)
        )
      ORDER BY CASE WHEN c.fec_fin_cotizacion < now() + interval '24 hours' THEN 0 ELSE 1 END,
        COALESCE(fit.keyword_points,0) DESC, c.fec_fin_cotizacion ASC NULLS LAST
      LIMIT 8
      `,
      [tenantId],
    ),
    query(
      `
      SELECT count(*)::int AS active,
        count(*) FILTER (
          WHERE c.fec_fin_cotizacion BETWEEN now() AND now() + interval '24 hours'
        )::int AS closing_24h,
        count(*) FILTER (
          WHERE c.fec_fin_cotizacion BETWEEN now() AND now() + interval '7 days'
        )::int AS closing_week
      FROM seace_contracts c
      WHERE c.estado_codigo=2
        AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion >= now())
        AND EXISTS (
          SELECT 1 FROM company_profiles cp
          JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
          WHERE cp.tenant_id=$1 AND cp.is_active=true
            AND c.cubso_segmento=ANY(bl.cubso_segmentos)
        )
      `,
      [tenantId],
    ),
    query(
      `
      SELECT m.id AS match_id, m.id_contrato, m.user_state, m.updated_at, c.codigo,
        c.descripcion, c.fec_fin_cotizacion, (ad.id IS NOT NULL) AS has_draft
      FROM matches m JOIN company_profiles cp ON cp.id=m.profile_id
      JOIN seace_contracts c ON c.id_contrato=m.id_contrato
      LEFT JOIN application_drafts ad ON ad.match_id=m.id
      WHERE cp.tenant_id=$1 AND m.user_state IN ('en_preparacion','postulada')
      ORDER BY CASE WHEN m.user_state='en_preparacion' THEN 0 ELSE 1 END,
        c.fec_fin_cotizacion ASC NULLS LAST LIMIT 8
      `,
      [tenantId],
    ),
    query(
      `
      SELECT count(*) FILTER (WHERE m.user_state='en_preparacion')::int AS preparing,
        count(*) FILTER (WHERE m.user_state='postulada')::int AS submitted,
        count(*) FILTER (WHERE ad.id IS NOT NULL)::int AS with_draft
      FROM matches m
      JOIN company_profiles cp ON cp.id=m.profile_id
      LEFT JOIN application_drafts ad ON ad.match_id=m.id
      WHERE cp.tenant_id=$1 AND m.user_state IN ('en_preparacion','postulada')
      `,
      [tenantId],
    ),
    query(
      `
      WITH segments AS (
        SELECT DISTINCT unnest(bl.cubso_segmentos) AS code
        FROM company_profiles cp JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
        WHERE cp.tenant_id=$1 AND cp.is_active=true
      )
      SELECT count(*)::int AS total,
        count(*) FILTER (WHERE estado_resultado='ADJUDICADO')::int AS adjudicados,
        count(*) FILTER (WHERE estado_resultado='DESIERTO')::int AS desiertos,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY precio_total) FILTER (WHERE precio_total IS NOT NULL) AS precio_median
      FROM historical_contract_outcomes h JOIN segments s ON s.code=h.cubso_segmento
      `,
      [tenantId],
    ),
    query(
      `
      WITH segments AS (
        SELECT DISTINCT unnest(bl.cubso_segmentos) AS code
        FROM company_profiles cp
        JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
        WHERE cp.tenant_id=$1 AND cp.is_active=true
      ), months AS (
        SELECT generate_series(
          date_trunc('month', now()) - interval '11 months',
          date_trunc('month', now()),
          interval '1 month'
        ) AS month
      )
      SELECT months.month,
        count(h.id_contrato) FILTER (WHERE h.estado_resultado='ADJUDICADO')::int AS adjudicados,
        count(h.id_contrato) FILTER (WHERE h.estado_resultado='DESIERTO')::int AS desiertos
      FROM months
      LEFT JOIN historical_contract_outcomes h
        ON date_trunc('month', COALESCE(h.fec_fin_cotizacion, h.fec_publica, h.created_at))=months.month
        AND EXISTS (SELECT 1 FROM segments s WHERE s.code=h.cubso_segmento)
      GROUP BY months.month
      ORDER BY months.month
      `,
      [tenantId],
    ),
    query(
      `
      WITH segments AS (
        SELECT DISTINCT unnest(bl.cubso_segmentos) AS code
        FROM company_profiles cp JOIN business_lines bl ON bl.profile_id=cp.id AND bl.is_active=true
        WHERE cp.tenant_id=$1 AND cp.is_active=true
      )
      SELECT h.supplier_name AS name, count(*)::int AS awards, sum(h.precio_total) AS amount
      FROM historical_contract_outcomes h JOIN segments s ON s.code=h.cubso_segmento
      WHERE h.estado_resultado='ADJUDICADO' AND h.supplier_name IS NOT NULL
      GROUP BY h.supplier_name ORDER BY awards DESC, amount DESC NULLS LAST LIMIT 6
      `,
      [tenantId],
    ),
  ]);
  const marketRow: any = market.rows[0] ?? {};
  const total = Number(marketRow.total ?? 0);
  const deserted = Number(marketRow.desiertos ?? 0);
  return {
    profile: profile.rows[0] ?? null,
    actions: actions.rows,
    action_summary: actionSummary.rows[0] ?? { active: 0, closing_24h: 0, closing_week: 0 },
    tracking: tracking.rows,
    tracking_summary: trackingSummary.rows[0] ?? { preparing: 0, submitted: 0, with_draft: 0 },
    market: {
      ...marketRow,
      total,
      desiertos_pct: total ? Math.round((deserted / total) * 100) : 0,
    },
    market_trend: marketTrend.rows,
    frequent_suppliers: suppliers.rows,
  };
}
