import { NextRequest, NextResponse } from "next/server";
import { query } from "@/server/db/client";
import { requireTenantId, requireTenantRole } from "@/server/auth/tenant";

const defaults = {
  enabled: true,
  min_fit_level: 2,
  min_notification_score: 50,
  max_daily_evaluations: 0,
  min_hours_before_close: 0,
};

export async function GET() {
  const tenantId = await requireTenantId();
  const result = await query(
    `SELECT COALESCE(ar.enabled,true) AS enabled,
       COALESCE(ar.min_fit_level,2) AS min_fit_level,
       COALESCE(ar.min_notification_score,50) AS min_notification_score,
       COALESCE(ar.max_daily_evaluations,0) AS max_daily_evaluations,
       COALESCE(ar.min_hours_before_close,0) AS min_hours_before_close
     FROM company_profiles cp
     LEFT JOIN automation_rules ar ON ar.profile_id=cp.id
     WHERE cp.tenant_id=$1 AND cp.is_active=true
     ORDER BY cp.updated_at DESC LIMIT 1`,
    [tenantId],
  );
  return NextResponse.json({ data: result.rows[0] ?? defaults });
}

export async function PUT(request: NextRequest) {
  const { tenantId } = await requireTenantRole();
  const body = await request.json();
  const profile = await query<{ id: string }>(
    "SELECT id FROM company_profiles WHERE tenant_id=$1 AND is_active=true ORDER BY updated_at DESC LIMIT 1",
    [tenantId],
  );
  if (!profile.rows[0]) return NextResponse.json({ error: "Configura primero el perfil de empresa" }, { status: 409 });

  const minFitLevel = Math.min(3, Math.max(1, Number(body.min_fit_level) || 2));
  const minScore = Math.min(99, Math.max(0, Number(body.min_notification_score) || 50));
  const maxDaily = Math.min(200, Math.max(0, Number(body.max_daily_evaluations) || 0));
  const minHours = Math.min(168, Math.max(0, Number(body.min_hours_before_close) || 0));
  const result = await query(
    `INSERT INTO automation_rules (
       profile_id,enabled,min_fit_level,min_notification_score,max_daily_evaluations,min_hours_before_close
     ) VALUES ($1,$2,$3,$4,$5,$6)
     ON CONFLICT (profile_id) DO UPDATE SET
       enabled=EXCLUDED.enabled,min_fit_level=EXCLUDED.min_fit_level,
       min_notification_score=EXCLUDED.min_notification_score,
       max_daily_evaluations=EXCLUDED.max_daily_evaluations,
       min_hours_before_close=EXCLUDED.min_hours_before_close,updated_at=now()
     RETURNING *`,
    [profile.rows[0].id, body.enabled !== false, minFitLevel, minScore, maxDaily, minHours],
  );
  return NextResponse.json({ data: result.rows[0] });
}
