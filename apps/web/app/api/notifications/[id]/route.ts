import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { query } from "@/server/db/client";
import { buildPatch, jsonError } from "@/server/services/crud";

async function notificationBelongsToTenant(tenantId: string, id: string) {
  const result = await query(
    `
    SELECT 1
    FROM notifications n
    JOIN tenant_members tm ON tm.user_id = n.user_id
    WHERE tm.tenant_id = $1 AND n.id = $2
    LIMIT 1
    `,
    [tenantId, id],
  );
  return Boolean(result.rows[0]);
}

export async function GET(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  if (!(await notificationBelongsToTenant(tenantId, id))) return jsonError("Not found", 404);
  const result = await query("SELECT * FROM notifications WHERE id = $1", [id]);
  return NextResponse.json({ data: result.rows[0] });
}

export async function PATCH(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  if (!(await notificationBelongsToTenant(tenantId, id))) return jsonError("Not found", 404);
  const body = await request.json();
  const patch = buildPatch(
    body,
    [
      { column: "channel" },
      { column: "reason" },
      { column: "payload", cast: "::jsonb", transform: (value) => JSON.stringify(value ?? {}) },
      { column: "status" },
      { column: "sent_at" },
      { column: "read_at" },
    ],
    2,
  );
  if (!patch.sets.length) return jsonError("No patch fields provided");
  const result = await query(`UPDATE notifications SET ${patch.sets.join(", ")} WHERE id = $1 RETURNING *`, [
    id,
    ...patch.values,
  ]);
  return NextResponse.json({ data: result.rows[0] });
}

export async function DELETE(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  if (!(await notificationBelongsToTenant(tenantId, id))) return jsonError("Not found", 404);
  const result = await query("DELETE FROM notifications WHERE id = $1 RETURNING *", [id]);
  return NextResponse.json({ data: result.rows[0] });
}
