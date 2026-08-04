import { query } from "@/server/db/client";

export async function getDashboard(tenantId: string) {
  const [profile, actions, actionSummary, tracking, trackingSummary, market, marketTrend, suppliers] = await Promise.all([
    query(`SELECT id, razon_social FROM company_profiles WHERE tenant_id=$1 AND is_active=true LIMIT 1`, [tenantId]),
    query(
      `
      SELECT c.id_contrato, c.codigo, c.descripcion, c.entidad_nombre, c.departamento,
        c.provincia, c.fec_fin_cotizacion, sc.created_at AS saved_at,
        match.score, match.verdict
      FROM saved_contracts sc
      JOIN seace_contracts c ON c.id_contrato=sc.id_contrato
      LEFT JOIN LATERAL (
        SELECT m.score, m.verdict
        FROM matches m
        JOIN company_profiles cp ON cp.id=m.profile_id
        WHERE cp.tenant_id=$1 AND cp.is_active=true AND m.id_contrato=c.id_contrato
        ORDER BY m.updated_at DESC
        LIMIT 1
      ) match ON true
      WHERE sc.tenant_id=$1
        AND c.estado_codigo=2
        AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion >= now())
      ORDER BY c.fec_fin_cotizacion ASC NULLS LAST, sc.created_at DESC
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
          WHERE c.fec_fin_cotizacion BETWEEN now() AND now() + interval '48 hours'
        )::int AS closing_48h,
        count(*) FILTER (
          WHERE c.fec_fin_cotizacion BETWEEN now() AND now() + interval '7 days'
        )::int AS closing_week
      FROM saved_contracts sc
      JOIN seace_contracts c ON c.id_contrato=sc.id_contrato
      WHERE sc.tenant_id=$1
        AND c.estado_codigo=2
        AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion >= now())
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
        count(DISTINCT supplier_ruc)::int AS suppliers,
        count(DISTINCT seace_entity_id)::int AS entities,
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
    action_summary: actionSummary.rows[0] ?? { active: 0, closing_24h: 0, closing_48h: 0, closing_week: 0 },
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
