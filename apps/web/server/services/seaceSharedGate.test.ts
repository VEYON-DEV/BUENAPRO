import test from "node:test";
import assert from "node:assert/strict";
import { resetSeaceEgressForTests, seaceFetch, SeaceUnavailable } from "./seaceEgress.ts";
import {
  BLOCKED_KEY, NEXT_SLOT_KEY, recordSharedBlock, setSharedGateBackendForTests, sharedGateEnabled,
  type SharedGateBackend,
} from "./seaceSharedGate.ts";

// Nunca toca SEACE ni Redis: el fetch y el backend del freno son simulados.
const URL_A = "https://prod6.seace.gob.pe/v1/s8uit-services/archivo/descargar?id=1";

/** Mismo contrato que Redis: reserva atómica con -1 bloqueado y -2 ocupado, y bloqueo NX con vencimiento. */
function fakeRedis() {
  const store = { nextSlotMs: 0, blocked: null as null | { payload: string; expiresAt: number } };
  const reserveCalls: Array<{ intervalMs: number; maxWaitMs: number }> = [];
  const live = () => {
    if (store.blocked && store.blocked.expiresAt <= Date.now()) store.blocked = null;
    return store.blocked;
  };
  const flags = { down: false, hang: false };
  const backend: SharedGateBackend = {
    async reserve(intervalMs: number, maxWaitMs: number) {
      reserveCalls.push({ intervalMs, maxWaitMs });
      if (flags.down) throw new Error("connect ECONNREFUSED redis://user:secret@redis:6379");
      if (flags.hang) return new Promise<number>(() => {});
      if (live()) return -1;
      const now = Date.now();
      const slot = Math.max(now, store.nextSlotMs);
      const delay = slot - now;
      if (maxWaitMs >= 0 && delay > maxWaitMs) return -2;
      store.nextSlotMs = slot + intervalMs;
      return delay;
    },
    async blockTtlMs() {
      const block = live();
      return block ? block.expiresAt - Date.now() : -2;
    },
    async writeBlock(payload: string, ttlMs: number) {
      if (flags.down) throw new Error("connect ECONNREFUSED");
      if (!live()) store.blocked = { payload, expiresAt: Date.now() + ttlMs };
    },
  };
  return Object.assign(backend, { store, reserveCalls, flags });
}

function setup(responses: Array<{ status: number }>, env: Record<string, string> = {}) {
  const calls: Array<{ url: string; init: any }> = [];
  Object.assign(process.env, {
    SEACE_EGRESS_PROXY: "", SEACE_EGRESS_REQUIRED: "", WEB_SEACE_MIN_INTERVAL_SECONDS: "0",
    WEB_SEACE_MAX_WAIT_SECONDS: "20", WEB_SEACE_MAX_REQUESTS_PER_HOUR: "20", SEACE_USER_AGENT: "QA-Browser",
    SEACE_SHARED_GATE: "1", REDIS_URL: "redis://user:secret@redis:6379",
    SEACE_SHARED_MIN_INTERVAL_SECONDS: "2", SEACE_SHARED_BLOCK_SECONDS: "120",
    ...env,
  });
  resetSeaceEgressForTests((async (url: string, init: any) => {
    calls.push({ url, init });
    return new Response(null, { status: (responses.shift() ?? { status: 500 }).status });
  }) as any);
  const fake = fakeRedis();
  setSharedGateBackendForTests(fake);
  return { calls, fake };
}

async function rejectsWith(promise: Promise<unknown>, reason: string) {
  await assert.rejects(promise, (error: unknown) => error instanceof SeaceUnavailable && error.reason === reason);
}

test("the shared gate needs the flag and a Redis URL", () => {
  const original = { gate: process.env.SEACE_SHARED_GATE, url: process.env.REDIS_URL };
  const enabled = (gate: string, url: string) => {
    Object.assign(process.env, { SEACE_SHARED_GATE: gate, REDIS_URL: url });
    return sharedGateEnabled();
  };
  assert.deepEqual(
    [enabled("1", "redis://r"), enabled("true", "redis://r"), enabled("TRUE", "redis://r")],
    [true, true, true],
  );
  assert.deepEqual(
    [enabled("", "redis://r"), enabled("0", "redis://r"), enabled("false", "redis://r"), enabled("1", ""), enabled("1", "  ")],
    [false, false, false, false, false],
  );
  Object.assign(process.env, { SEACE_SHARED_GATE: original.gate ?? "", REDIS_URL: original.url ?? "" });
});

test("a disabled gate never touches Redis and behaves as before", async () => {
  const { calls, fake } = setup([{ status: 200 }, { status: 403 }], { SEACE_SHARED_GATE: "" });
  fake.flags.down = true;
  assert.equal((await seaceFetch(URL_A)).status, 200);
  assert.equal((await seaceFetch(URL_A)).status, 403);
  await rejectsWith(seaceFetch(URL_A), "blocked");
  assert.equal(calls.length, 2);
  assert.equal(fake.reserveCalls.length, 0);
  assert.equal(fake.store.blocked, null);
});

test("a block written by another process stops the request and pauses the web locally", async () => {
  const { calls, fake } = setup([{ status: 200 }]);
  fake.store.blocked = {
    payload: JSON.stringify({ status: 403, source: "prod6", at: new Date().toISOString() }),
    expiresAt: Date.now() + 3600_000,
  };
  await rejectsWith(seaceFetch(URL_A), "blocked");
  assert.equal(calls.length, 0);
  // La pausa ya quedó en el proceso: sin el bloqueo compartido sigue cerrado y no vuelve a consultar Redis.
  fake.store.blocked = null;
  await rejectsWith(seaceFetch(URL_A), "blocked");
  assert.equal(calls.length, 0);
  assert.equal(fake.reserveCalls.length, 1);
});

test("a 403 or 429 writes the shared block as web with the shared duration", async () => {
  for (const status of [403, 429]) {
    const { calls, fake } = setup([{ status }]);
    assert.equal((await seaceFetch(URL_A)).status, status);
    assert.equal(calls.length, 1);
    const block = fake.store.blocked;
    assert.ok(block);
    const payload = JSON.parse(block.payload);
    assert.equal(payload.status, status);
    assert.equal(payload.source, "web");
    assert.match(payload.at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/);
    const remaining = block.expiresAt - Date.now();
    assert.ok(remaining > 119_000 && remaining <= 120_000, `remaining ${remaining}`);
  }
});

test("an existing shared block is not replaced or extended by the web", async () => {
  const { fake } = setup([]);
  const original = { payload: '{"status":429,"source":"prod2"}', expiresAt: Date.now() + 5_000 };
  fake.store.blocked = original;
  await recordSharedBlock(403);
  assert.deepEqual(fake.store.blocked, original);
});

test("a failed block write never throws", async (t) => {
  const { fake } = setup([]);
  fake.flags.down = true;
  const warn = t.mock.method(console, "warn", () => {});
  await recordSharedBlock(403);
  assert.equal(JSON.parse(warn.mock.calls[0].arguments[0]).seace_shared_gate_error, "block");
});

test("wait:false with the shared turn taken answers busy without fetch or local spending", async () => {
  const { calls, fake } = setup(
    [{ status: 200 }, { status: 200 }],
    { WEB_SEACE_MIN_INTERVAL_SECONDS: "60", WEB_SEACE_MAX_REQUESTS_PER_HOUR: "1" },
  );
  fake.store.nextSlotMs = Date.now() + 60_000;
  await rejectsWith(seaceFetch(URL_A, { wait: false }), "busy");
  assert.equal(calls.length, 0);
  assert.deepEqual(fake.reserveCalls, [{ intervalMs: 2000, maxWaitMs: 0 }]);
  // Si el intento fallido hubiera gastado el ritmo local o el tope por hora, este también sería busy.
  fake.store.nextSlotMs = 0;
  assert.equal((await seaceFetch(URL_A, { wait: false })).status, 200);
  assert.equal(calls.length, 1);
});

test("a free shared turn is sent right away and the next one is reserved after the interval", async () => {
  const { calls, fake } = setup([{ status: 200 }]);
  const before = Date.now();
  assert.equal((await seaceFetch(URL_A)).status, 200);
  assert.equal(calls.length, 1);
  assert.deepEqual(fake.reserveCalls, [{ intervalMs: 2000, maxWaitMs: 20_000 }]);
  assert.ok(fake.store.nextSlotMs >= before + 2000);
});

test("the request waits for the shared turn", async () => {
  const { calls, fake } = setup([{ status: 200 }]);
  fake.store.nextSlotMs = Date.now() + 400;
  const started = Date.now();
  assert.equal((await seaceFetch(URL_A)).status, 200);
  assert.ok(Date.now() - started >= 150, "esperó el turno compartido");
  assert.equal(calls.length, 1);
});

test("a shared turn farther than the wait limit answers busy", async () => {
  const { calls, fake } = setup([{ status: 200 }]);
  fake.store.nextSlotMs = Date.now() + 60_000;
  await rejectsWith(seaceFetch(URL_A), "busy");
  assert.equal(calls.length, 0);
});

test("an interactive request takes the shared turn with its own longer wait limit", async () => {
  const { calls, fake } = setup([{ status: 200 }, { status: 200 }]);
  assert.equal((await seaceFetch(URL_A, { interactive: true })).status, 200);
  assert.deepEqual(fake.reserveCalls, [{ intervalMs: 2000, maxWaitMs: 45_000 }]);
  // El turno compartido sigue siendo uno: a más de 45 s también se rechaza, sin enviar nada.
  fake.store.nextSlotMs = Date.now() + 60_000;
  await rejectsWith(seaceFetch(URL_A, { interactive: true }), "busy");
  assert.equal(calls.length, 1);
});

test("a shared block stops interactive requests and an interactive 403 writes it", async () => {
  const blocked = setup([{ status: 200 }]);
  blocked.fake.store.blocked = {
    payload: JSON.stringify({ status: 429, source: "prod6", at: new Date().toISOString() }),
    expiresAt: Date.now() + 3600_000,
  };
  await rejectsWith(seaceFetch(URL_A, { interactive: true }), "blocked");
  assert.equal(blocked.calls.length, 0);

  const first = setup([{ status: 403 }, { status: 200 }]);
  assert.equal((await seaceFetch(URL_A, { interactive: true })).status, 403);
  assert.equal(JSON.parse(first.fake.store.blocked!.payload).source, "web");
  await rejectsWith(seaceFetch(URL_A, { interactive: true }), "blocked");
  assert.equal(first.calls.length, 1);
});

test("the local wait is discounted from the shared budget and still applies", async () => {
  const { calls, fake } = setup(
    [{ status: 200 }, { status: 200 }],
    { WEB_SEACE_MIN_INTERVAL_SECONDS: "0.3", SEACE_SHARED_MIN_INTERVAL_SECONDS: "0" },
  );
  await seaceFetch(URL_A);
  const started = Date.now();
  await seaceFetch(URL_A);
  assert.equal(calls.length, 2);
  assert.equal(fake.reserveCalls[0].maxWaitMs, 20_000);
  const localWait = 20_000 - fake.reserveCalls[1].maxWaitMs;
  assert.ok(localWait > 0 && localWait <= 300, `local wait ${localWait}`);
  assert.ok(Date.now() - started >= localWait - 20, "esperó el ritmo local");
});

test("two simultaneous requests never take the same local turn while waiting for Redis", async () => {
  const { calls, fake } = setup(
    [{ status: 200 }, { status: 200 }],
    { WEB_SEACE_MIN_INTERVAL_SECONDS: "60", SEACE_SHARED_MIN_INTERVAL_SECONDS: "0" },
  );
  const results = await Promise.allSettled([seaceFetch(URL_A, { wait: false }), seaceFetch(URL_A, { wait: false })]);
  assert.deepEqual(results.map((result) => result.status).sort(), ["fulfilled", "rejected"]);
  const rejected = results.find((result) => result.status === "rejected") as PromiseRejectedResult;
  assert.ok(rejected.reason instanceof SeaceUnavailable && rejected.reason.reason === "busy");
  assert.equal(calls.length, 1);
  assert.equal(fake.reserveCalls.length, 2);
});

test("Redis down closes busy without fetch and logs no secret, then recovers", async (t) => {
  const { calls, fake } = setup([{ status: 200 }]);
  fake.flags.down = true;
  const warn = t.mock.method(console, "warn", () => {});
  await rejectsWith(seaceFetch(URL_A), "busy");
  assert.equal(calls.length, 0);
  const logged = String(warn.mock.calls[0].arguments[0]);
  assert.equal(JSON.parse(logged).seace_shared_gate_error, "reserve");
  assert.ok(!logged.includes("secret") && !logged.includes("redis://"));
  // Caerse Redis no abre la pausa local: al volver, la web envía de nuevo.
  fake.flags.down = false;
  assert.equal((await seaceFetch(URL_A)).status, 200);
  assert.equal(calls.length, 1);
});

test("a Redis that never answers closes busy instead of hanging the request", async (t) => {
  const { calls, fake } = setup([{ status: 200 }]);
  fake.flags.hang = true;
  const warn = t.mock.method(console, "warn", () => {});
  await rejectsWith(seaceFetch(URL_A), "busy");
  assert.equal(calls.length, 0);
});

test("the protocol keys are the ones shared with the workers", () => {
  assert.equal(NEXT_SLOT_KEY, "seace:egress:next_slot_ms");
  assert.equal(BLOCKED_KEY, "seace:egress:blocked");
});
