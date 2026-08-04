import { NextRequest, NextResponse } from "next/server";
import { pool, query } from "@/server/db/client";
import { requireTenantId, requireTenantRole } from "@/server/auth/tenant";
import { telegramRequest } from "@/server/services/telegram";

type RecipientInput = { id?: string; label?: string; chat_id?: string; enabled?: boolean };

export async function GET() {
  const tenantId = await requireTenantId();
  const integration = await query(
    `SELECT id,enabled,bot_username,token_hint,verified_at
     FROM telegram_integrations WHERE tenant_id=$1 LIMIT 1`,
    [tenantId],
  );
  if (!integration.rows[0]) {
    return NextResponse.json({ data: { configured: false, enabled: false, recipients: [] } });
  }
  const recipients = await query(
    `SELECT id,label,chat_id,enabled,last_tested_at
     FROM telegram_recipients WHERE integration_id=$1 ORDER BY created_at,id`,
    [integration.rows[0].id],
  );
  return NextResponse.json({ data: { configured: true, ...integration.rows[0], recipients: recipients.rows } });
}

export async function PUT(request: NextRequest) {
  const { tenantId, userId } = await requireTenantRole();
  const body = await request.json();
  const token = String(body.token ?? "").trim();
  const key = process.env.SETTINGS_ENCRYPTION_KEY;
  if (token && !key) {
    return NextResponse.json({ error: "Falta SETTINGS_ENCRYPTION_KEY en el servidor" }, { status: 503 });
  }

  let botUsername: string | null = null;
  if (token) {
    const validation = await telegramRequest(token, "getMe", {});
    botUsername = String(validation.result?.username ?? "");
  }

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const existing = await client.query(
      "SELECT id FROM telegram_integrations WHERE tenant_id=$1 FOR UPDATE",
      [tenantId],
    );
    let integrationId = existing.rows[0]?.id as string | undefined;
    if (!integrationId && !token) {
      await client.query("ROLLBACK");
      return NextResponse.json({ error: "Ingresa el token del bot" }, { status: 400 });
    }
    if (integrationId) {
      if (token) {
        await client.query(
          `UPDATE telegram_integrations SET
             token_ciphertext=pgp_sym_encrypt($2,$3,'cipher-algo=aes256'),
             token_hint=$4,bot_username=$5,enabled=$6,verified_at=now(),updated_at=now()
           WHERE id=$1`,
          [integrationId, token, key, token.slice(-6), botUsername, body.enabled !== false],
        );
      } else {
        await client.query(
          "UPDATE telegram_integrations SET enabled=$2,updated_at=now() WHERE id=$1",
          [integrationId, body.enabled !== false],
        );
      }
    } else {
      const inserted = await client.query(
        `INSERT INTO telegram_integrations (
           tenant_id,token_ciphertext,token_hint,bot_username,enabled,verified_at
         ) VALUES ($1,pgp_sym_encrypt($2,$3,'cipher-algo=aes256'),$4,$5,$6,now()) RETURNING id`,
        [tenantId, token, key, token.slice(-6), botUsername, body.enabled !== false],
      );
      integrationId = inserted.rows[0].id;
    }

    const recipients = Array.isArray(body.recipients) ? (body.recipients as RecipientInput[]) : [];
    const keepIds: string[] = [];
    for (const recipient of recipients) {
      const chatId = String(recipient.chat_id ?? "").trim();
      if (!/^-?\d{5,20}$/.test(chatId)) continue;
      const label = String(recipient.label ?? "Telegram").trim().slice(0, 80) || "Telegram";
      const saved = await client.query(
        `INSERT INTO telegram_recipients (id,integration_id,label,chat_id,enabled)
         VALUES (COALESCE($1::uuid,gen_random_uuid()),$2,$3,$4,$5)
         ON CONFLICT (integration_id,chat_id) DO UPDATE SET
           label=EXCLUDED.label,enabled=EXCLUDED.enabled,updated_at=now()
         RETURNING id`,
        [recipient.id ?? null, integrationId, label, chatId, recipient.enabled !== false],
      );
      keepIds.push(saved.rows[0].id);
    }
    await client.query(
      "DELETE FROM telegram_recipients WHERE integration_id=$1 AND NOT (id=ANY($2::uuid[]))",
      [integrationId, keepIds],
    );

    await client.query(
      `WITH updated AS (
         UPDATE notification_preferences SET enabled=$3,min_verdict='ambar',max_alerts_per_day=0,updated_at=now()
         WHERE tenant_id=$1 AND user_id=$2 AND channel='telegram' AND business_line_id IS NULL
         RETURNING id
       )
       INSERT INTO notification_preferences (
         tenant_id,user_id,channel,enabled,mode,min_verdict,max_alerts_per_day
       )
       SELECT $1,$2,'telegram',$3,'realtime','ambar',0
       WHERE NOT EXISTS (SELECT 1 FROM updated)`,
      [tenantId, userId, body.enabled !== false],
    );
    await client.query("COMMIT");
    return NextResponse.json({ data: { configured: true, bot_username: botUsername, recipients: keepIds.length } });
  } catch (error) {
    await client.query("ROLLBACK");
    const message = error instanceof Error ? error.message : "No se pudo guardar Telegram";
    return NextResponse.json({ error: message }, { status: 400 });
  } finally {
    client.release();
  }
}
