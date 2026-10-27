/**
 * Freno compartido de salida a SEACE.
 *
 * La web y los workers PROD6 y PROD2 salen por la misma IP, y SEACE corta la IP completa ante una
 * ráfaga. Con `SEACE_SHARED_GATE` activo y `REDIS_URL`, los tres comparten en Redis un solo turno
 * global de peticiones y una sola pausa ante 403/429. Los workers implementan el mismo protocolo
 * (claves, script Lua y JSON del bloqueo); cualquier cambio aquí debe replicarse allí.
 *
 * Si Redis falla con el freno activo la web cierra sin enviar. Desactivado, no hace nada.
 */

export const NEXT_SLOT_KEY = "seace:egress:next_slot_ms";
export const BLOCKED_KEY = "seace:egress:blocked";

// KEYS[1]=next_slot, KEYS[2]=blocked, ARGV[1]=interval_ms, ARGV[2]=max_wait_ms (-1 = sin límite).
// Devuelve: >=0 espera en ms antes de enviar, -1 bloqueado, -2 turno ocupado.
export const RESERVE_SCRIPT = `
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, nxt)
local delay = slot - now
local max_wait = tonumber(ARGV[2])
if max_wait >= 0 and delay > max_wait then return -2 end
redis.call('SET', KEYS[1], slot + tonumber(ARGV[1]), 'PX', 86400000)
return delay
`;

// Un comando de Redis nunca debe dejar colgada una petición de la web.
const OPERATION_TIMEOUT_MS = 2000;

export type SharedSlot =
  | { kind: "ok"; delayMs: number }
  | { kind: "blocked"; remainingMs: number }
  | { kind: "busy" };

/** Las tres operaciones sobre Redis; las pruebas inyectan un fake con la misma semántica. */
export type SharedGateBackend = {
  /** Ejecuta `RESERVE_SCRIPT`: >=0 espera en ms, -1 bloqueado, -2 ocupado. */
  reserve(intervalMs: number, maxWaitMs: number): Promise<number>;
  /** PTTL del bloqueo compartido: negativo si no existe (-2) o no vence (-1). */
  blockTtlMs(): Promise<number>;
  /** SET NX PX: no pisa ni extiende un bloqueo ya escrito por otro proceso. */
  writeBlock(payload: string, ttlMs: number): Promise<void>;
};

type RedisLike = {
  eval(script: string, options: { keys: string[]; arguments: string[] }): Promise<unknown>;
  pTTL(key: string): Promise<number>;
  set(
    key: string,
    value: string,
    options: { condition: "NX"; expiration: { type: "PX"; value: number } },
  ): Promise<unknown>;
};

type GateHolder = { override?: SharedGateBackend; real?: SharedGateBackend };

// Una sola conexión por proceso aunque Next empaquete el módulo en varias rutas.
const GATE_KEY = Symbol.for("buenapro.seaceSharedGate");
function holder(): GateHolder {
  const root = globalThis as unknown as Record<symbol, GateHolder | undefined>;
  root[GATE_KEY] ??= {};
  return root[GATE_KEY]!;
}

function numberEnv(name: string, fallback: number): number {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}

export function sharedGateEnabled(): boolean {
  return /^(1|true)$/i.test(process.env.SEACE_SHARED_GATE?.trim() ?? "")
    && Boolean(process.env.REDIS_URL?.trim());
}

function gateSettings() {
  return {
    intervalMs: Math.round(numberEnv("SEACE_SHARED_MIN_INTERVAL_SECONDS", 6) * 1000),
    blockMs: Math.round(numberEnv("SEACE_SHARED_BLOCK_SECONDS", 10800) * 1000),
  };
}

export function setSharedGateBackendForTests(backend: SharedGateBackend | undefined): void {
  holder().override = backend;
}

function withTimeout<T>(pending: Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(Object.assign(new Error("redis timeout"), { code: "ETIMEDOUT" })), OPERATION_TIMEOUT_MS);
    pending.then(resolve, reject).finally(() => clearTimeout(timer));
  });
}

function realBackend(): SharedGateBackend {
  const current = holder();
  if (current.real) return current.real;
  const clientPromise = (async () => {
    const { createClient } = await import("redis");
    const client = createClient({
      url: process.env.REDIS_URL,
      // Sin cola offline: con Redis caído el comando falla al instante en vez de acumularse.
      disableOfflineQueue: true,
      socket: { connectTimeout: OPERATION_TIMEOUT_MS, reconnectStrategy: (retries: number) => Math.min(250 * 2 ** retries, 5000) },
    });
    // Sin escucha de "error" un fallo de socket tumbaría el proceso de la web.
    client.on("error", () => {});
    // Reconecta solo; la petición que lo espera lo hace con tope de tiempo.
    const connected = client.connect().then(() => client);
    connected.catch(() => {});
    return { client: client as unknown as RedisLike, connected };
  })();
  const use = async <T>(run: (client: RedisLike) => Promise<T>): Promise<T> => {
    const { client, connected } = await clientPromise;
    await connected;
    return run(client);
  };
  const backend: SharedGateBackend = {
    reserve: async (intervalMs, maxWaitMs) => Number(await use((client) => client.eval(RESERVE_SCRIPT, {
      keys: [NEXT_SLOT_KEY, BLOCKED_KEY],
      arguments: [String(intervalMs), String(maxWaitMs)],
    }))),
    blockTtlMs: () => use((client) => client.pTTL(BLOCKED_KEY)),
    writeBlock: async (payload, ttlMs) => {
      await use((client) => client.set(BLOCKED_KEY, payload, { condition: "NX", expiration: { type: "PX", value: ttlMs } }));
    },
  };
  // Si no se pudo ni importar el cliente, el siguiente intento vuelve a probar.
  clientPromise.catch(() => { delete current.real; });
  current.real = backend;
  return backend;
}

function backendNow(): SharedGateBackend {
  return holder().override ?? realBackend();
}

function warnFailure(operation: string, error: unknown): void {
  const code = (error as { code?: unknown } | null)?.code;
  console.warn(JSON.stringify({
    seace_shared_gate_error: operation,
    error: error instanceof Error ? error.constructor.name : "unknown",
    ...(typeof code === "string" ? { code } : {}),
  }));
}

/**
 * Reserva el siguiente turno global. `maxWaitMs` es lo máximo que esta petición acepta esperar;
 * con un turno más lejano responde `busy` sin reservar nada. Redis caído también es `busy`.
 */
export async function reserveSharedSlot(maxWaitMs: number): Promise<SharedSlot> {
  const { intervalMs, blockMs } = gateSettings();
  try {
    const backend = backendNow();
    const reply = await withTimeout(backend.reserve(intervalMs, Math.max(0, Math.floor(maxWaitMs))));
    if (reply === -2) return { kind: "busy" };
    if (reply === -1) {
      const ttl = await withTimeout(backend.blockTtlMs());
      // -2: el bloqueo venció entre el script y la lectura; -1: existe sin vencimiento.
      return { kind: "blocked", remainingMs: ttl === -1 ? blockMs : Math.max(0, ttl) };
    }
    if (Number.isFinite(reply) && reply >= 0) return { kind: "ok", delayMs: reply };
    throw new Error("unexpected reply");
  } catch (error) {
    warnFailure("reserve", error);
    return { kind: "busy" };
  }
}

/** Escribe la pausa compartida ante 403/429. Es de mejor esfuerzo: nunca lanza. */
export async function recordSharedBlock(status: number): Promise<void> {
  if (!sharedGateEnabled()) return;
  try {
    const payload = JSON.stringify({ status, source: "web", at: new Date().toISOString() });
    await withTimeout(backendNow().writeBlock(payload, gateSettings().blockMs));
  } catch (error) {
    warnFailure("block", error);
  }
}
