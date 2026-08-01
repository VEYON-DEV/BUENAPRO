import { NextRequest, NextResponse } from "next/server";
import { query } from "@/server/db/client";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";

export async function GET() {
  const tenantId = await requireTenantId();
  const result = await query(
    `
    SELECT p.*
    FROM notification_preferences p
    JOIN users u ON u.id = p.user_id
    JOIN tenant_members tm ON tm.user_id = u.id
    WHERE tm.tenant_id = $1
    ORDER BY p.channel
    `,
    [tenantId],
  );
  return NextResponse.json({ data: result.rows });
}

export async function PUT(request: NextRequest) {
  const tenantId = await requireTenantId();
  const body = await request.json();
  const sessionUserId = await currentUserId();
  const member = await query(
    "SELECT user_id FROM tenant_members WHERE tenant_id = $1 AND ($2::uuid IS NULL OR user_id = $2) ORDER BY created_at LIMIT 1",
    [tenantId, sessionUserId],
  );
  if (!member.rows[0]) return NextResponse.json({ error: "Se requiere un usuario" }, { status: 400 });
  const channel = ["in_app", "email", "telegram"].includes(body.channel) ? body.channel : "in_app";
  const mode = body.mode === "digest" ? "digest" : "realtime";
  const minVerdict = body.min_verdict === "ambar" ? "ambar" : "verde";
  const maxAlerts = Math.min(10, Math.max(1, Number(body.max_alerts_per_day) || 5));
  const enabled = body.enabled !== false;
  const result = await query(
    `
    WITH current_pref AS (
      SELECT id FROM notification_preferences
      WHERE tenant_id=$1 AND user_id=$2 AND channel=$3 AND business_line_id IS NULL
      ORDER BY updated_at DESC LIMIT 1
    ), updated AS (
      UPDATE notification_preferences
      SET enabled=$4,mode=$5,min_verdict=$6,max_alerts_per_day=$7,
          quiet_hours_json=$8::jsonb,updated_at=now()
      WHERE id=(SELECT id FROM current_pref)
      RETURNING *
    ), inserted AS (
      INSERT INTO notification_preferences (
        tenant_id,user_id,channel,enabled,mode,min_verdict,max_alerts_per_day,quiet_hours_json
      )
      SELECT $1,$2,$3,$4,$5,$6,$7,$8::jsonb
      WHERE NOT EXISTS (SELECT 1 FROM updated)
      RETURNING *
    )
    SELECT * FROM updated UNION ALL SELECT * FROM inserted LIMIT 1
    `,
    [tenantId, member.rows[0].user_id, channel, enabled, mode, minVerdict, maxAlerts, JSON.stringify(body.quiet_hours_json ?? {})],
  );
  return NextResponse.json({ data: result.rows[0] });
}
