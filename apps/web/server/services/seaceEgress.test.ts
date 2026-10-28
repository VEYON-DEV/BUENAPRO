import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { getSeacePdf, isSeaceUrl, resetSeaceEgressForTests, seaceFetch, SeaceUnavailable } from "./seaceEgress.ts";
import { FIREWALL_403 } from "./seaceEgress.test-support.ts";

// Nunca toca SEACE: el fetch es simulado y la caché vive en un directorio temporal.
const PDF = Buffer.from("%PDF-1.7\n1 0 obj\n");
const URL_A = "https://prod6.seace.gob.pe/v1/s8uit-services/archivo/descargar?id=1";

function setup(responses: Array<{ status: number; body?: Buffer | string }>, env: Record<string, string> = {}) {
  const calls: Array<{ url: string; init: any }> = [];
  const cacheDir = mkdtempSync(join(tmpdir(), "seace-egress-test-"));
  Object.assign(process.env, {
    SEACE_EGRESS_PROXY: "", SEACE_EGRESS_REQUIRED: "", WEB_SEACE_MIN_INTERVAL_SECONDS: "0",
    WEB_SEACE_MAX_REQUESTS_PER_HOUR: "20", WEB_SEACE_MAX_WAIT_SECONDS: "20", WEB_SEACE_INTERACTIVE_MAX_WAIT_SECONDS: "45",
    WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR: "60", SEACE_DOCUMENT_CACHE_DIR: cacheDir, SEACE_USER_AGENT: "QA-Browser",
    ...env,
  });
  resetSeaceEgressForTests((async (url: string, init: any) => {
    calls.push({ url, init });
    const next = responses.shift() ?? { status: 500 };
    const body = next.body ?? (next.status === 403 ? FIREWALL_403 : undefined);
    return new Response(typeof body === "string" ? body : new Uint8Array(body ?? []), { status: next.status });
  }) as any);
  return { calls, cacheDir };
}

async function rejectsWith(promise: Promise<unknown>, reason: string) {
  await assert.rejects(promise, (error: unknown) => error instanceof SeaceUnavailable && error.reason === reason);
}

test("only https SEACE hosts are accepted", () => {
  assert.equal(isSeaceUrl(URL_A), true);
  assert.equal(isSeaceUrl("https://seace.gob.pe/x"), true);
  assert.equal(isSeaceUrl("http://prod6.seace.gob.pe/x"), false);
  assert.equal(isSeaceUrl("https://seace.gob.pe.evil.test/x"), false);
  assert.equal(isSeaceUrl("https://user:pw@prod6.seace.gob.pe/x"), false);
  assert.equal(isSeaceUrl("http://127.0.0.1:9/x"), false);
});

test("a required egress without proxy fails closed before any request", async () => {
  const { calls } = setup([{ status: 200, body: PDF }], { SEACE_EGRESS_REQUIRED: "1" });
  await rejectsWith(seaceFetch(URL_A), "egress");
  assert.equal(calls.length, 0);
});

test("the PDF is downloaded once and served from cache, also for concurrent callers", async () => {
  const { calls, cacheDir } = setup([{ status: 200, body: PDF }]);
  const [first, second] = await Promise.all([getSeacePdf(URL_A), getSeacePdf(URL_A)]);
  const third = await getSeacePdf(URL_A);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.headers["User-Agent"], "QA-Browser");
  assert.deepEqual([first, second, third].map((b) => b.toString()), Array(3).fill(PDF.toString()));
  assert.deepEqual(readdirSync(cacheDir).filter((name) => name.endsWith(".tmp")), []);
});

test("a WAF page answered with 200 is never cached as a PDF", async () => {
  const { calls, cacheDir } = setup([{ status: 200, body: "<html>Request rejected</html>" }, { status: 200, body: PDF }]);
  await rejectsWith(getSeacePdf(URL_A), "not_pdf");
  assert.deepEqual(readdirSync(cacheDir), []);
  assert.equal((await getSeacePdf(URL_A)).toString(), PDF.toString());
  assert.equal(calls.length, 2);
});

test("403 pauses every later SEACE request of the web", async () => {
  const { calls } = setup([{ status: 403 }, { status: 200, body: PDF }]);
  await rejectsWith(getSeacePdf(URL_A), "upstream");
  await rejectsWith(getSeacePdf(URL_A.replace("id=1", "id=2")), "blocked");
  assert.equal(calls.length, 1);
});

test("pacing and hourly cap refuse instead of bursting", async () => {
  const paced = setup([{ status: 200, body: "{}" }, { status: 200, body: "{}" }], { WEB_SEACE_MIN_INTERVAL_SECONDS: "60" });
  await seaceFetch(URL_A, { wait: false });
  await rejectsWith(seaceFetch(URL_A, { wait: false }), "busy");
  assert.equal(paced.calls.length, 1);

  const capped = setup([{ status: 200, body: "{}" }, { status: 200, body: "{}" }], { WEB_SEACE_MAX_REQUESTS_PER_HOUR: "1" });
  await seaceFetch(URL_A);
  await rejectsWith(seaceFetch(URL_A), "busy");
  assert.equal(capped.calls.length, 1);
});

test("method, body and redirect reach the request only when asked, and the host is still validated", async () => {
  const { calls } = setup([{ status: 200, body: "{}" }, { status: 200, body: "{}" }]);
  await seaceFetch(URL_A, { headers: { Accept: "application/json" } });
  await seaceFetch(URL_A, {
    method: "POST", body: '{"a":1}', redirect: "manual", headers: { "Content-Type": "application/json", "User-Agent": "Other" },
  });
  assert.deepEqual(["method", "body", "redirect"].map((key) => key in calls[0].init), [false, false, false]);
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.body, '{"a":1}');
  assert.equal(calls[1].init.redirect, "manual");
  assert.equal(calls[1].init.headers["Content-Type"], "application/json");
  // La identidad de salida es siempre la configurada, también en las peticiones autenticadas.
  assert.equal(calls[1].init.headers["User-Agent"], "QA-Browser");
  await rejectsWith(seaceFetch("https://evil.test/x", { redirect: "manual" }), "upstream");
  assert.equal(calls.length, 2);
});

test("a manual redirect is returned as is so the caller can validate the next host", async () => {
  setup([]);
  resetSeaceEgressForTests((async () => new Response(null, { status: 302, headers: { location: "https://evil.test/x" } })) as any);
  const response = await seaceFetch(URL_A, { redirect: "manual" });
  assert.equal(response.status, 302);
  assert.equal(isSeaceUrl(response.headers.get("location")!), false);
});

test("interactive requests have their own hourly budget and leave the background one untouched", async () => {
  const run = setup(Array(8).fill({ status: 200, body: "{}" }), {
    WEB_SEACE_MAX_REQUESTS_PER_HOUR: "1", WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR: "3",
  });
  await seaceFetch(URL_A, { interactive: true });
  await seaceFetch(URL_A, { interactive: true });
  await seaceFetch(URL_A, { interactive: true });
  await rejectsWith(seaceFetch(URL_A, { interactive: true }), "busy");
  // El cupo de fondo (vistas previas, refresco del detalle) sigue intacto después de tres acciones de usuario.
  await seaceFetch(URL_A, { wait: false });
  await rejectsWith(seaceFetch(URL_A, { wait: false }), "busy");
  assert.equal(run.calls.length, 4);
});

test("interactive requests wait for their turn longer than background ones", async () => {
  const run = setup(Array(4).fill({ status: 200, body: "{}" }), {
    WEB_SEACE_MIN_INTERVAL_SECONDS: "0.3", WEB_SEACE_MAX_WAIT_SECONDS: "0.1", WEB_SEACE_INTERACTIVE_MAX_WAIT_SECONDS: "2",
  });
  await seaceFetch(URL_A, { interactive: true });
  // El ritmo es uno solo: el turno siguiente cae 300 ms después aunque cambie de carril.
  await rejectsWith(seaceFetch(URL_A), "busy");
  const started = Date.now();
  await seaceFetch(URL_A, { interactive: true });
  assert.ok(Date.now() - started >= 250, "the second turn waits for the shared pacing");
  assert.equal(run.calls.length, 2);
});

test("a 403 answered by the SEACE application (an expired session) pauses nobody", async () => {
  const run = setup([{ status: 403, body: JSON.stringify({ status: 403, error: "Forbidden", path: "/maestra" }) },
    { status: 200, body: "{}" }]);
  assert.equal((await seaceFetch(URL_A, { interactive: true })).status, 403);
  assert.equal((await seaceFetch(URL_A)).status, 200);
  assert.equal(run.calls.length, 2);
});

test("a 403 or 429 on an interactive request pauses every later request, interactive or not", async () => {
  const run = setup([{ status: 429 }, { status: 200, body: "{}" }]);
  assert.equal((await seaceFetch(URL_A, { interactive: true })).status, 429);
  await rejectsWith(seaceFetch(URL_A, { interactive: true }), "blocked");
  await rejectsWith(seaceFetch(URL_A), "blocked");
  assert.equal(run.calls.length, 1);
});

test("a required egress without proxy also closes interactive requests before any request", async () => {
  const run = setup([{ status: 200, body: "{}" }], { SEACE_EGRESS_REQUIRED: "1" });
  await rejectsWith(seaceFetch(URL_A, { interactive: true, method: "POST", body: "{}" }), "egress");
  assert.equal(run.calls.length, 0);
});
