declare global {
  // eslint-disable-next-line no-var
  var buenaproRedis: any | undefined;
}

export function hasRedisConfig() {
  return Boolean(process.env.REDIS_URL);
}

export async function redisClient() {
  if (!process.env.REDIS_URL) return null;
  const existing = globalThis.buenaproRedis;
  if (existing?.isOpen) return existing;
  const { createClient } = await import("redis");
  const client =
    existing ??
    createClient({
      url: process.env.REDIS_URL,
    });
  if (!client.isOpen) await client.connect();
  if (process.env.NODE_ENV !== "production") {
    globalThis.buenaproRedis = client;
  }
  return client;
}
