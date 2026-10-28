import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fetch as undiciFetch, ProxyAgent } from "undici";
import {
  recordSharedBlock, reserveSharedSlot, setSharedGateBackendForTests, sharedGateEnabled,
} from "./seaceSharedGate.ts";

/**
 * Salida de la web hacia SEACE.
 *
 * SEACE bloquea la IP de la VM, así que, igual que el worker, la web sale por el proxy peruano
 * (`SEACE_EGRESS_PROXY`) solo para `seace.gob.pe` y `*.seace.gob.pe`; con `SEACE_EGRESS_REQUIRED`
 * nunca sale directo. La IP de salida es la misma que usa el worker, así que la web pide poco:
 * ritmo mínimo entre peticiones, tope por hora, pausa larga ante 403/429 y caché de documentos.
 * Con `SEACE_SHARED_GATE` el turno y la pausa se comparten además con los workers vía Redis
 * (`seaceSharedGate.ts`); desactivado, todo es local al proceso.
 *
 * Las acciones que un usuario pide a propósito (conectar su cuenta, abrir un documento, preparar una
 * postulación) usan `interactive: true`: mismo ritmo, mismo turno compartido y misma pausa, pero con
 * tope por hora y espera máxima propios, para que ni agoten el cupo de las vistas previas ni se
 * rechacen por unos segundos de cola. Un login autenticado son varias peticiones seguidas.
 */

export type SeaceUnavailableReason = "blocked" | "busy" | "egress" | "network" | "upstream" | "not_pdf";

export class SeaceUnavailable extends Error {
  readonly reason: SeaceUnavailableReason;
  readonly status?: number;

  constructor(reason: SeaceUnavailableReason, status?: number) {
    super(`SEACE unavailable: ${reason}${status ? ` (${status})` : ""}`);
    this.name = "SeaceUnavailable";
    this.reason = reason;
    this.status = status;
  }
}

const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";
const MAX_DOCUMENT_BYTES = 40 * 1024 * 1024;

function numberEnv(name: string, fallback: number): number {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}

function settings() {
  return {
    proxy: process.env.SEACE_EGRESS_PROXY?.trim() || null,
    required: /^(1|true|yes)$/i.test(process.env.SEACE_EGRESS_REQUIRED?.trim() ?? ""),
    userAgent: process.env.SEACE_USER_AGENT?.trim() || DEFAULT_USER_AGENT,
    minIntervalMs: numberEnv("WEB_SEACE_MIN_INTERVAL_SECONDS", 8) * 1000,
    maxWaitMs: numberEnv("WEB_SEACE_MAX_WAIT_SECONDS", 20) * 1000,
    maxPerHour: numberEnv("WEB_SEACE_MAX_REQUESTS_PER_HOUR", 20),
    interactiveMaxWaitMs: numberEnv("WEB_SEACE_INTERACTIVE_MAX_WAIT_SECONDS", 45) * 1000,
    interactiveMaxPerHour: numberEnv("WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR", 60),
    blockMs: numberEnv("WEB_SEACE_BLOCK_BACKOFF_SECONDS", 10800) * 1000,
    timeoutMs: numberEnv("WEB_SEACE_TIMEOUT_SECONDS", 30) * 1000,
    cacheDir: process.env.SEACE_DOCUMENT_CACHE_DIR?.trim() || join(tmpdir(), "buenapro-seace-documents"),
    cacheTtlMs: numberEnv("SEACE_DOCUMENT_CACHE_HOURS", 24) * 3600 * 1000,
  };
}

type EgressState = {
  nextSlotAt: number;
  blockedUntil: number;
  recent: { background: number[]; interactive: number[] };
  agent: { proxy: string; dispatcher: ProxyAgent } | null;
  inflight: Map<string, Promise<Buffer>>;
  fetchImpl: typeof undiciFetch;
};

// Un solo estado por proceso aunque Next empaquete el módulo en varias rutas.
const STATE_KEY = Symbol.for("buenapro.seaceEgress");
function state(): EgressState {
  const holder = globalThis as unknown as Record<symbol, EgressState | undefined>;
  holder[STATE_KEY] ??= {
    nextSlotAt: 0, blockedUntil: 0, recent: { background: [], interactive: [] }, agent: null, inflight: new Map(),
    fetchImpl: undiciFetch,
  };
  return holder[STATE_KEY]!;
}

export function resetSeaceEgressForTests(fetchImpl?: typeof undiciFetch): void {
  const holder = globalThis as unknown as Record<symbol, EgressState | undefined>;
  delete holder[STATE_KEY];
  setSharedGateBackendForTests(undefined);
  if (fetchImpl) state().fetchImpl = fetchImpl;
}

export function isSeaceUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    return url.protocol === "https:" && !url.username && !url.password
      && (host === "seace.gob.pe" || host.endsWith(".seace.gob.pe"));
  } catch {
    return false;
  }
}

function dispatcherFor(proxy: string): ProxyAgent {
  const current = state();
  if (current.agent?.proxy !== proxy) current.agent = { proxy, dispatcher: new ProxyAgent(proxy) };
  return current.agent.dispatcher;
}

/** Turno local: valida bloqueo, tope por hora y ritmo, sin consumir nada. El ritmo es uno solo para ambos carriles. */
function localSlot(maxWaitMs: number, interactive: boolean): { slot: number; delay: number } {
  const config = settings();
  const current = state();
  const now = Date.now();
  if (current.blockedUntil > now) throw new SeaceUnavailable("blocked");
  const lane = interactive ? "interactive" : "background";
  current.recent[lane] = current.recent[lane].filter((at) => at > now - 3600_000);
  if (current.recent[lane].length >= (interactive ? config.interactiveMaxPerHour : config.maxPerHour)) {
    throw new SeaceUnavailable("busy");
  }
  const slot = Math.max(now, current.nextSlotAt);
  const delay = slot - now;
  if (delay > maxWaitMs) throw new SeaceUnavailable("busy");
  return { slot, delay };
}

/** Reserva un turno; `wait: false` no espera (refrescos de fondo que pueden omitirse). */
async function acquireSlot(wait: boolean, interactive: boolean): Promise<void> {
  const config = settings();
  const current = state();
  const maxWaitMs = wait ? (interactive ? config.interactiveMaxWaitMs : config.maxWaitMs) : 0;
  let local = localSlot(maxWaitMs, interactive);
  let sharedDelay = 0;
  if (sharedGateEnabled()) {
    // El turno compartido se pide antes de consumir el local: si falla, no se gasta ninguno.
    const shared = await reserveSharedSlot(Math.max(0, maxWaitMs - local.delay));
    if (shared.kind === "blocked") {
      current.blockedUntil = Date.now() + shared.remainingMs;
      throw new SeaceUnavailable("blocked");
    }
    if (shared.kind === "busy") throw new SeaceUnavailable("busy");
    sharedDelay = shared.delayMs;
    // Otra petición pudo tomar el turno local, o abrir la pausa, mientras se esperaba a Redis.
    local = localSlot(maxWaitMs, interactive);
  }
  current.nextSlotAt = local.slot + config.minIntervalMs;
  current.recent[interactive ? "interactive" : "background"].push(local.slot);
  const delay = Math.max(local.delay, sharedDelay);
  if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
}

export type SeaceFetchOptions = {
  headers?: Record<string, string>;
  method?: string;
  body?: string;
  /** Sin indicar sigue las redirecciones; quien necesite validar cada salto usa "manual" y vuelve a llamar. */
  redirect?: "follow" | "manual" | "error";
  wait?: boolean;
  /** Acción pedida por un usuario: tope por hora y espera máxima propios, mismo ritmo y misma pausa. */
  interactive?: boolean;
  timeoutMs?: number;
};

export async function seaceFetch(url: string, options: SeaceFetchOptions = {}) {
  if (!isSeaceUrl(url)) throw new SeaceUnavailable("upstream");
  const config = settings();
  if (!config.proxy && config.required) throw new SeaceUnavailable("egress");
  await acquireSlot(options.wait ?? true, options.interactive ?? false);
  let response;
  try {
    response = await state().fetchImpl(url, {
      ...(options.method ? { method: options.method } : {}),
      ...(options.body !== undefined ? { body: options.body } : {}),
      ...(options.redirect ? { redirect: options.redirect } : {}),
      headers: { ...options.headers, "User-Agent": config.userAgent },
      signal: AbortSignal.timeout(options.timeoutMs ?? config.timeoutMs),
      ...(config.proxy ? { dispatcher: dispatcherFor(config.proxy) } : {}),
    });
  } catch {
    // Proxy caído o red: no es un bloqueo de SEACE y no abre la pausa larga.
    throw new SeaceUnavailable(config.proxy ? "egress" : "network");
  }
  if (response.status === 403 || response.status === 429) {
    state().blockedUntil = Date.now() + config.blockMs;
    console.warn(JSON.stringify({ seace_web_block: response.status, paused_seconds: config.blockMs / 1000 }));
    await recordSharedBlock(response.status);
  }
  return response;
}

async function readCached(path: string, ttlMs: number): Promise<Buffer | null> {
  try {
    const info = await stat(path);
    if (Date.now() - info.mtimeMs > ttlMs) return null;
    return await readFile(path);
  } catch {
    return null;
  }
}

async function pruneCache(dir: string, ttlMs: number): Promise<void> {
  const now = Date.now();
  for (const name of await readdir(dir).catch(() => [] as string[])) {
    const path = join(dir, name);
    const info = await stat(path).catch(() => null);
    if (info && now - info.mtimeMs > ttlMs) await rm(path, { force: true });
  }
}

async function downloadPdf(url: string, path: string, dir: string, ttlMs: number): Promise<Buffer> {
  const response = await seaceFetch(url, { headers: { Accept: "application/pdf,*/*" } });
  if (!response.ok) {
    await response.body?.cancel();
    throw new SeaceUnavailable("upstream", response.status);
  }
  if (Number(response.headers.get("content-length") ?? 0) > MAX_DOCUMENT_BYTES) {
    await response.body?.cancel();
    throw new SeaceUnavailable("not_pdf");
  }
  const body = Buffer.from(await response.arrayBuffer());
  // Una página de error del WAF con 200 nunca se guarda como PDF.
  if (body.length > MAX_DOCUMENT_BYTES || body.subarray(0, 5).toString("latin1") !== "%PDF-") {
    throw new SeaceUnavailable("not_pdf");
  }
  await mkdir(dir, { recursive: true });
  const temporary = `${path}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(temporary, body);
  await rename(temporary, path);
  await pruneCache(dir, ttlMs);
  return body;
}

/**
 * PDF de SEACE desde la caché local; si no está, una sola descarga aunque varias rutas
 * (miniatura y visor) lo pidan a la vez.
 */
export async function getSeacePdf(url: string): Promise<Buffer> {
  const config = settings();
  const key = createHash("sha256").update(url, "utf8").digest("hex");
  const path = join(config.cacheDir, `${key}.pdf`);
  const cached = await readCached(path, config.cacheTtlMs);
  if (cached) return cached;
  const inflight = state().inflight;
  const pending = inflight.get(key);
  if (pending) return pending;
  const download = downloadPdf(url, path, config.cacheDir, config.cacheTtlMs).finally(() => inflight.delete(key));
  inflight.set(key, download);
  return download;
}
