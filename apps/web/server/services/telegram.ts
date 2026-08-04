import { query } from "@/server/db/client";

export async function decryptTelegramToken(tenantId: string) {
  const key = process.env.SETTINGS_ENCRYPTION_KEY;
  if (!key) throw new Error("Falta SETTINGS_ENCRYPTION_KEY");
  const result = await query<{ token: string }>(
    `SELECT pgp_sym_decrypt(token_ciphertext,$2) AS token
     FROM telegram_integrations WHERE tenant_id=$1 AND enabled=true`,
    [tenantId, key],
  );
  if (!result.rows[0]?.token) throw new Error("Telegram no está configurado");
  return result.rows[0].token;
}

export async function telegramRequest(token: string, method: string, payload: Record<string, unknown>) {
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    cache: "no-store",
  });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.description || "Telegram rechazó la solicitud");
  return data;
}
