const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require(path.resolve(__dirname, "../../../node_modules/typescript"));

const web = path.resolve(__dirname, "../../..");

function load(file, mocks, globals) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports, Error, Object, String, Number, JSON, Headers, Promise, encodeURIComponent, ...globals,
    require(name) {
      if (Object.hasOwn(mocks, name)) return mocks[name];
      throw new Error(`Unexpected module: ${name}`);
    },
  });
  return exports;
}

function client(fetchMock) {
  const calls = [];
  const globals = { fetch: async (url, options) => { calls.push({ url, options }); return fetchMock(url, options); } };
  const apiClient = load(path.join(web, "lib/api/client.ts"), {}, globals);
  const model = load(path.join(__dirname, "../model/manualGrading.ts"), {}, {});
  const api = load(path.join(__dirname, "manualGrading.ts"), { "@/lib/api/client": apiClient, "../model/manualGrading": model }, globals);
  return { api, calls };
}
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const plain = value => JSON.parse(JSON.stringify(value));
const stamps = { created_at: "2026-10-30T10:00:00.000Z", updated_at: "2026-10-30T10:00:00.000Z" };

test("status is read without caching, scoped to the profile, and normalized", async () => {
  const { api, calls } = client(() => json(200, { data: { request: { status: "reading", mode: "queued", reason: null, ...stamps }, usage: { used: 4, limit: 100 } } }));
  const snapshot = await api.fetchGradingStatus(501, "33333333-3333-4333-8333-333333333333");
  assert.equal(calls[0].url, "/api/contracts/501/grade?profile_id=33333333-3333-4333-8333-333333333333");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(plain(snapshot).request.status, "reading");
  assert.deepEqual(plain(snapshot).usage, { used: 4, limit: 100 });
  await api.fetchGradingStatus(501, null);
  assert.equal(calls[1].url, "/api/contracts/501/grade");
});

test("a response without a request or with garbage yields nulls instead of crashing", async () => {
  const { api } = client(() => json(200, { data: { request: { status: "bogus" } } }));
  assert.deepEqual(plain(await api.fetchGradingStatus(501)), { request: null, usage: null });
});

test("submitting sends force and the profile, and tells done from queued", async () => {
  const done = client(() => json(200, { data: { status: "done", result: { match_id: 1 }, usage: { used: 5, limit: 100 } } }));
  const result = await done.api.submitGrading(501, { profileId: "33333333-3333-4333-8333-333333333333", force: true });
  assert.deepEqual(plain(result), { kind: "done", usage: { used: 5, limit: 100 } });
  assert.equal(done.calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(done.calls[0].options.body), { force: true, profile_id: "33333333-3333-4333-8333-333333333333" });

  const queued = client(() => json(202, { data: { status: "queued", request: { status: "pending", mode: "queued", reason: null, ...stamps }, usage: { used: 6, limit: 100 } } }));
  const accepted = plain(await queued.api.submitGrading(501, { force: false }));
  assert.equal(accepted.kind, "queued");
  assert.equal(accepted.request.status, "pending");
  assert.deepEqual(JSON.parse(queued.calls[0].options.body), { force: false });
});

test("expected failures come back as results the screen can render", async () => {
  const limit = client(() => json(429, { error: "Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.", code: "daily_limit_reached", usage: { used: 100, limit: 100 } }));
  assert.deepEqual(plain(await limit.api.submitGrading(501, { force: false })), {
    kind: "problem", problem: { kind: "limit", message: "Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa." }, usage: { used: 100, limit: 100 },
  });
  const profile = client(() => json(409, { error: "No se encontró un perfil activo para esta empresa.", code: "profile_not_found" }));
  assert.deepEqual(plain(await profile.api.submitGrading(501, { force: false })), { kind: "problem", problem: { kind: "no_profile" }, usage: null });
  const failed = client(() => json(422, { error: "x", code: "grading_failed", reason: "invalid_model_output" }));
  assert.deepEqual(plain(await failed.api.submitGrading(501, { force: false })), { kind: "failed", reason: "invalid_model_output", usage: null });
  const missing = client(() => json(404, { error: "No se encontró la licitación.", code: "contract_not_found" }));
  assert.equal(plain(await missing.api.submitGrading(501, { force: false })).reason, "contract_not_found");
  const html = client(() => new Response("<html>Bad gateway</html>", { status: 502, headers: { "content-type": "text/html" } }));
  assert.deepEqual(plain(await html.api.submitGrading(501, { force: false })), { kind: "failed", reason: null, usage: null });
});

test("a network error is reported as a retryable problem", async () => {
  const { api } = client(() => { throw new TypeError("fetch failed"); });
  assert.deepEqual(plain(await api.submitGrading(501, { force: false })), { kind: "problem", problem: { kind: "network" }, usage: null });
});
