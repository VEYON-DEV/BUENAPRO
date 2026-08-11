import { redisClient } from "@/server/redis/client";

export type CodexHotSession = {
  status: "active";
  tenantId: string;
  chatSessionId: string;
  codexThreadId: string | null;
  workspacePath: string;
  contextHash: string;
  lastActivityAt: string;
};

const DEFAULT_TTL_SECONDS = 30 * 60;
const memoryStore = new Map<string, { value: CodexHotSession; expiresAt: number }>();

function key(tenantId: string, chatSessionId: string) {
  return `codex:chat:${tenantId}:${chatSessionId}:state`;
}

function lockKey(tenantId: string, chatSessionId: string) {
  return `codex:chat:${tenantId}:${chatSessionId}:lock`;
}

export function codexHotSessionTtlSeconds() {
  const parsed = Number(process.env.CODEX_AGENT_SESSION_TTL_SECONDS);
  return Number.isFinite(parsed) && parsed >= 60
    ? Math.floor(parsed)
    : DEFAULT_TTL_SECONDS;
}

export async function getCodexHotSession(
  tenantId: string,
  chatSessionId: string,
) {
  const redis = await redisClient();
  if (redis) {
    const raw = await redis.get(key(tenantId, chatSessionId));
    return raw ? (JSON.parse(raw) as CodexHotSession) : null;
  }
  const cached = memoryStore.get(key(tenantId, chatSessionId));
  if (!cached) return null;
  if (cached.expiresAt <= Date.now()) {
    memoryStore.delete(key(tenantId, chatSessionId));
    return null;
  }
  return cached.value;
}

export async function setCodexHotSession(state: CodexHotSession) {
  const ttl = codexHotSessionTtlSeconds();
  const redis = await redisClient();
  if (redis) {
    await redis.set(key(state.tenantId, state.chatSessionId), JSON.stringify(state), {
      EX: ttl,
    });
    return;
  }
  memoryStore.set(key(state.tenantId, state.chatSessionId), {
    value: state,
    expiresAt: Date.now() + ttl * 1000,
  });
}

export async function touchCodexHotSession(
  tenantId: string,
  chatSessionId: string,
) {
  const redis = await redisClient();
  if (redis) {
    await redis.expire(key(tenantId, chatSessionId), codexHotSessionTtlSeconds());
    return;
  }
  const cached = memoryStore.get(key(tenantId, chatSessionId));
  if (cached) cached.expiresAt = Date.now() + codexHotSessionTtlSeconds() * 1000;
}

export async function acquireCodexSessionLock(
  tenantId: string,
  chatSessionId: string,
) {
  const ttl = Math.max(60, Math.min(300, codexHotSessionTtlSeconds()));
  const redis = await redisClient();
  const lock = lockKey(tenantId, chatSessionId);
  if (redis) {
    const result = await redis.set(lock, "1", { NX: true, EX: ttl });
    return result === "OK";
  }
  if (memoryStore.has(lock)) return false;
  memoryStore.set(lock, {
    value: {
      status: "active",
      tenantId,
      chatSessionId,
      codexThreadId: null,
      workspacePath: "",
      contextHash: "",
      lastActivityAt: new Date().toISOString(),
    },
    expiresAt: Date.now() + ttl * 1000,
  });
  return true;
}

export async function releaseCodexSessionLock(
  tenantId: string,
  chatSessionId: string,
) {
  const redis = await redisClient();
  const lock = lockKey(tenantId, chatSessionId);
  if (redis) {
    await redis.del(lock);
    return;
  }
  memoryStore.delete(lock);
}
