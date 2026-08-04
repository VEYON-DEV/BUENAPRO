import { NextRequest, NextResponse } from "next/server";
import { query } from "@/server/db/client";
import { requireTenantRole } from "@/server/auth/tenant";
import { decryptTelegramToken, telegramRequest } from "@/server/services/telegram";

export async function POST(request: NextRequest) {
  const { tenantId } = await requireTenantRole();
  const body = await request.json();
  const recipient = await query<{ id: string; chat_id: string }>(
    `SELECT tr.id,tr.chat_id
     FROM telegram_recipients tr
     JOIN telegram_integrations ti ON ti.id=tr.integration_id
     WHERE tr.id=$1 AND ti.tenant_id=$2 AND tr.enabled=true AND ti.enabled=true`,
    [body.recipient_id, tenantId],
  );
  if (!recipient.rows[0]) return NextResponse.json({ error: "Destinatario no encontrado" }, { status: 404 });
  try {
    const token = await decryptTelegramToken(tenantId);
    await telegramRequest(token, "sendMessage", {
      chat_id: recipient.rows[0].chat_id,
      text: "🔎 BuenaPro está conectado. Las oportunidades con evaluación mayor a 50 llegarán a este chat.",
      disable_web_page_preview: true,
    });
    await query("UPDATE telegram_recipients SET last_tested_at=now(),updated_at=now() WHERE id=$1", [recipient.rows[0].id]);
    return NextResponse.json({ data: { sent: true } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "No se pudo enviar" }, { status: 400 });
  }
}
