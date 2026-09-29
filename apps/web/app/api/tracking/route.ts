import { NextRequest, NextResponse } from "next/server";
import { query } from "@/server/db/client";
import { requireTenantId } from "@/server/auth/tenant";

export async function GET(request: NextRequest) {
  const tenantId = await requireTenantId();
  const state = request.nextUrl.searchParams.get("state");
  const values: unknown[] = [tenantId];
  const where = ["cp.tenant_id = $1"];
  if (state) {
    values.push(state);
    where.push(`m.user_state = $${values.length}`);
  }
  const result = await query(
    `
    SELECT
      m.*,
      c.opportunity_id,
      'seace_prod6'::text AS source_key,
      o.record_kind,
      o.object_type,
      o.procurement_method,
      o.lifecycle_stage,
      o.participation_access,
      o.actionability,
      c.codigo,
      c.entidad_nombre,
      c.descripcion,
      c.objeto_codigo,
      c.estado_codigo,
      c.cubso_segmento,
      c.valor_estimado,
      c.valor_no_informado,
      c.cotizar,
      c.fec_fin_cotizacion,
      count(mt.id)::int AS tasks_count,
      count(mt.id) FILTER (WHERE mt.status = 'done')::int AS tasks_done_count
    FROM matches m
    JOIN company_profiles cp ON cp.id = m.profile_id
    JOIN seace_contracts c ON c.id_contrato = m.id_contrato
    LEFT JOIN opportunities o ON o.id = c.opportunity_id
    LEFT JOIN match_tasks mt ON mt.match_id = m.id
    WHERE ${where.join(" AND ")}
    GROUP BY m.id, c.id_contrato, o.id
    ORDER BY m.updated_at DESC
    `,
    values,
  );
  return NextResponse.json({ data: result.rows });
}
