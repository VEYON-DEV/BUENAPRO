import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as egress from "./seaceEgress.ts";
import { fakeEgress, loadServerModule, nextServerStub } from "./seaceEgress.test-support.ts";

// Las cuatro piezas del servidor que antes salían directo a SEACE: PROD2 (documento oficial), PROD4 (PDF),
// la sesión autenticada y el agente documental. Todo con fetch simulado: nunca hay red ni SEACE.
const { SeaceUnavailable } = egress;
const PROXY = "http://127.0.0.1:9";
const uuid = "00000000-0000-0000-0000-000000000010";
const PDF = "%PDF-1.7\n1 0 obj\n";
const SAFE_PROD2_MESSAGE = "SEACE no pudo proporcionar el enlace de descarga";
const BASE = "https://prod6.seace.gob.pe/v1/s8uit-services";

const http = () => loadServerModule("../http/seaceUnavailable.ts", { "next/server": nextServerStub }).exports;

test("the four server pieces never call the global fetch", () => {
  for (const file of [
    "./prod2OfficialDocument.ts",
    "../../app/api/prod4/opportunities/[id]/documents/[code]/route.ts",
    "./seaceAuthenticated.ts",
    "../agent/codexAgent.ts",
  ]) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(source, /(?<![\w.])fetch\(/, file);
    assert.match(source, /seaceEgress/, file);
  }
});

// ---------------------------------------------------------------------------------------------- PROD2

function prod2() {
  return loadServerModule("./prod2OfficialDocument.ts", { "./seaceEgress": egress });
}

test("PROD2 resolves the download ticket through the egress and the proxy, not through global fetch", async () => {
  const { calls } = fakeEgress(
    [{ status: 200, body: JSON.stringify({ downloadUrl: "/service/api/node/content/file.pdf?ticket=fresh" }) }],
    { SEACE_EGRESS_PROXY: PROXY },
  );
  const { exports, directFetches } = prod2();
  assert.equal(
    await exports.resolveProd2OfficialDocument(uuid),
    "https://alfprod.seace.gob.pe/alfresco/service/api/node/content/file.pdf?ticket=fresh",
  );
  assert.equal(calls.length, 1);
  const endpoint = new URL(calls[0].url);
  assert.equal(endpoint.host, "alfprod.seace.gob.pe");
  assert.equal(endpoint.searchParams.get("id"), uuid);
  assert.equal(endpoint.searchParams.get("doc"), "buenaproDocument");
  assert.ok(calls[0].init.dispatcher, "sale por el proxy peruano");
  assert.equal(calls[0].init.redirect, "error");
  assert.equal(calls[0].init.headers["User-Agent"], "QA-Browser");
  assert.deepEqual(directFetches, []);
});

test("PROD2 turns a required-but-missing egress, a SEACE block and a full budget into the fixed safe error", async () => {
  const scenarios: Array<[string, Record<string, string>, boolean]> = [
    ["egress required without proxy", { SEACE_EGRESS_REQUIRED: "1" }, false],
    ["interactive budget spent", { WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR: "0" }, false],
    ["SEACE denies the request", {}, true],
  ];
  for (const [name, env, sent] of scenarios) {
    const { calls } = fakeEgress([{ status: 403, body: "secret waf page" }], env);
    const { exports, directFetches } = prod2();
    await assert.rejects(exports.resolveProd2OfficialDocument(uuid),
      (error: Error) => error.message.includes(SAFE_PROD2_MESSAGE) && !/secret|blocked|busy|egress/i.test(error.message), name);
    assert.equal(calls.length, sent ? 1 : 0, name);
    assert.deepEqual(directFetches, [], name);
  }
  // Tras el 403 la pausa sigue abierta: la siguiente resolución ni siquiera sale.
  const { calls } = fakeEgress([{ status: 403 }, { status: 200, body: "{}" }]);
  const { exports } = prod2();
  await assert.rejects(exports.resolveProd2OfficialDocument(uuid));
  await assert.rejects(exports.resolveProd2OfficialDocument(uuid), (error: Error) => error.message.includes(SAFE_PROD2_MESSAGE));
  assert.equal(calls.length, 1);
});

// ---------------------------------------------------------------------------------------------- PROD4

function prod4Route() {
  return loadServerModule("../../app/api/prod4/opportunities/[id]/documents/[code]/route.ts", {
    "next/server": nextServerStub,
    "@/server/auth/tenant": { requireTenantId: async () => "tenant" },
    "@/server/db/client": { query: async () => ({ rows: [{ name: "bases.pdf", extension: "pdf" }] }) },
    "@/server/http/seaceUnavailable": http(),
    "@/server/services/seaceEgress": egress,
  });
}

const prod4Get = (route: ReturnType<typeof prod4Route>) =>
  route.exports.GET(new Request("https://local.test"), { params: Promise.resolve({ id: "123", code: uuid }) });

test("PROD4 streams the official PDF through the egress and the proxy, not through global fetch", async () => {
  const { calls } = fakeEgress([{ status: 200, body: PDF }], { SEACE_EGRESS_PROXY: PROXY });
  const route = prod4Route();
  const response = await prod4Get(route);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/pdf");
  assert.equal(Buffer.from(await response.arrayBuffer()).toString(), PDF);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=${uuid}`);
  assert.ok(calls[0].init.dispatcher, "sale por el proxy peruano");
  assert.equal(calls[0].init.redirect, "manual");
  assert.equal(calls[0].init.headers.Referer, "https://prod4.seace.gob.pe/openegocio/");
  assert.equal(calls[0].init.headers["User-Agent"], "QA-Browser");
  assert.deepEqual(route.directFetches, []);
});

test("PROD4 answers 503 with the 'Abrir en SEACE' page when the egress is closed, paused or denied", async () => {
  const official = `https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=${uuid}`;
  const expectPage = async (response: Response) => {
    assert.equal(response.status, 503);
    assert.match(response.headers.get("content-type") ?? "", /^text\/html/);
    assert.equal(response.headers.get("retry-after"), "600");
    const html = await response.text();
    assert.match(html, /Abrir en SEACE/);
    assert.ok(html.includes(`href="${official}"`));
  };

  const closed = fakeEgress([{ status: 200, body: PDF }], { SEACE_EGRESS_REQUIRED: "1" });
  const route = prod4Route();
  await expectPage(await prod4Get(route));
  assert.equal(closed.calls.length, 0);
  assert.deepEqual(route.directFetches, []);

  const denied = fakeEgress([{ status: 403, body: "waf" }, { status: 200, body: PDF }]);
  const second = prod4Route();
  await expectPage(await prod4Get(second));
  await expectPage(await prod4Get(second));
  assert.equal(denied.calls.length, 1, "after the 403 the pause keeps the next request from leaving");
});

test("PROD4 keeps its 502 JSON for other upstream failures and does not pause the egress", async () => {
  const { calls } = fakeEgress([{ status: 404, body: "no" }, { status: 200, body: PDF }]);
  const route = prod4Route();
  const missing = await prod4Get(route);
  assert.equal(missing.status, 502);
  assert.equal((await missing.json()).error, "Official document unavailable");
  assert.equal((await prod4Get(route)).status, 200);
  assert.equal(calls.length, 2);
});

// ------------------------------------------------------------------------------------ seaceAuthenticated

const jwt = (exp: number) => {
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${part({ alg: "none" })}.${part({ exp })}.signature`;
};
const inOneHour = () => Math.floor(Date.now() / 1000) + 3600;
const expired = () => Math.floor(Date.now() / 1000) - 3600;
const json = (value: unknown) => JSON.stringify(value);

function connectionRow(accessExp: number | null) {
  return {
    id: "conn-1", tenant_id: "t1", status: "connected",
    username_secret: "enc:20123456789", password_secret: "enc:contraseña-larga",
    session_secret: accessExp === null ? null : `enc:${json({ accessToken: jwt(accessExp), refreshToken: "refresh-1" })}`,
  };
}

function authenticated(row: ReturnType<typeof connectionRow> | null) {
  const statements: Array<{ sql: string; values: unknown[] }> = [];
  const query = async (sql: string, values: unknown[] = []) => {
    statements.push({ sql, values });
    if (/SELECT \* FROM seace_connections/.test(sql)) return { rows: row ? [row] : [] };
    if (/INSERT INTO seace_connections/.test(sql)) return { rows: [{ id: "conn-1" }] };
    return { rows: [] };
  };
  const service = loadServerModule("./seaceAuthenticated.ts", {
    "@/server/db/client": { query },
    "@/server/security/secrets": {
      encryptSecret: (value: string) => `enc:${value}`,
      decryptSecret: (value: string) => value.replace(/^enc:/, ""),
    },
    "@/server/services/seaceEgress": egress,
  });
  const marksInvalid = () => statements.some(({ sql }) => /status='invalid'/.test(sql));
  return { ...service, statements, marksInvalid };
}

test("the SEACE login and 2FA check leave through the egress with the account headers", async () => {
  const { calls } = fakeEgress([
    { status: 200, body: json({ respuesta: true, token: jwt(inOneHour()), refreshToken: "refresh-1" }) },
    { status: 200, body: json({ respuesta: true, data: { tipoPrimario: 0, tipoSecundario: 0 } }) },
  ], { SEACE_EGRESS_PROXY: PROXY });
  const service = authenticated(null);
  const result = await service.exports.connectSeace("t1", "user-1", "20123456789", "contraseña-larga");
  assert.equal(result.connected, true);
  assert.equal(result.requires2fa, false);
  assert.equal(calls.length, 2);
  const [login, twoFactor] = calls;
  assert.equal(login.url, `${BASE}/seguridadproveedor/seguridad/validausuariornp`);
  assert.equal(login.init.method, "POST");
  assert.deepEqual(JSON.parse(login.init.body), { username: "20123456789", password: "contraseña-larga" });
  assert.equal(login.init.redirect, "error");
  assert.ok(login.init.dispatcher, "sale por el proxy peruano");
  assert.equal(login.init.headers["Content-Type"], "application/json");
  assert.equal(login.init.headers.Origin, "https://prod6.seace.gob.pe");
  assert.equal(login.init.headers["client-s8uit"], json({ terminal: "0.0.0.0" }));
  assert.equal(login.init.headers["User-Agent"], "QA-Browser");
  assert.match(twoFactor.url, /\/twofactor\/config\/check\/2005\/ROL_GENERAL_2FA$/);
  assert.match(twoFactor.init.headers.Authorization, /^Bearer /);
  assert.equal("method" in twoFactor.init, false);
  assert.deepEqual(service.directFetches, []);
  assert.ok(service.statements.some(({ sql }) => /INSERT INTO seace_connections/.test(sql)));
});

test("an authenticated query reuses a valid session and sends it as a bearer token", async () => {
  const { calls } = fakeEgress([{ status: 200, body: json({ ok: 1 }) }], { SEACE_EGRESS_PROXY: PROXY });
  const service = authenticated(connectionRow(inOneHour()));
  const data = await service.exports.authenticatedSeaceJson("t1", "/maestra/maestras/listar-estados-consulta");
  assert.equal(data.ok, 1);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${BASE}/maestra/maestras/listar-estados-consulta`);
  assert.match(calls[0].init.headers.Authorization, /^Bearer ey/);
  assert.equal(calls[0].init.redirect, "error");
  assert.deepEqual(service.directFetches, []);
});

test("two parallel queries with an expired session share one refresh instead of spending two turns", async () => {
  const fresh = jwt(inOneHour());
  const { calls } = fakeEgress([
    { status: 200, body: json({ token: fresh, refreshToken: "refresh-2" }) },
    { status: 200, body: json({ n: 1 }) },
    { status: 200, body: json({ n: 2 }) },
  ]);
  const service = authenticated(connectionRow(expired()));
  const [first, second] = await Promise.all([
    service.exports.authenticatedSeaceJson("t1", "/cotizacion/cotizaciones/listar-completo?id_contrato=9"),
    service.exports.authenticatedSeaceJson("t1", "/contratacion/contrataciones/obtener-completo?id_contrato=9"),
  ]);
  assert.equal(first.n + second.n, 3);
  assert.match(calls[0].url, /\/tokens\/refresh$/);
  assert.deepEqual(calls.slice(1).map(({ url }) => new URL(url).pathname.split("/").pop()).sort(), ["listar-completo", "obtener-completo"]);
  assert.equal(calls.filter(({ url }) => url.includes("/tokens/refresh")).length, 1);
  assert.equal(calls[1].init.headers.Authorization, `Bearer ${fresh}`);
  assert.equal(calls[2].init.headers.Authorization, `Bearer ${fresh}`);
});

test("an unavailable egress is not a credential failure: the connection is not marked invalid", async () => {
  for (const env of [{ SEACE_EGRESS_REQUIRED: "1" }, { WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR: "0" }] as Array<Record<string, string>>) {
    const { calls } = fakeEgress([{ status: 200, body: json({ token: jwt(inOneHour()) }) }], env);
    const service = authenticated(connectionRow(expired()));
    await assert.rejects(service.exports.authenticatedSeaceJson("t1", "/maestra/x"), (error: unknown) => error instanceof SeaceUnavailable);
    assert.equal(calls.length, 0);
    assert.equal(service.marksInvalid(), false);
  }
});

test("a 403 from the API opens the pause and the retry login is refused without invalidating the connection", async () => {
  const { calls } = fakeEgress([{ status: 403, body: json({ message: "forbidden" }) }, { status: 200, body: json({}) }]);
  const service = authenticated(connectionRow(inOneHour()));
  await assert.rejects(service.exports.authenticatedSeaceJson("t1", "/maestra/x"),
    (error: unknown) => error instanceof SeaceUnavailable && error.reason === "blocked");
  assert.equal(calls.length, 1);
  assert.equal(service.marksInvalid(), false);
  assert.ok(service.statements.some(({ sql }) => /status='expired'/.test(sql)));
});

test("a real credential rejection still marks the connection invalid", async () => {
  fakeEgress([{ status: 200, body: json({ respuesta: false }) }]);
  const service = authenticated(connectionRow(null));
  await assert.rejects(service.exports.authenticatedSeaceJson("t1", "/maestra/x"), /SEACE_INVALID_CREDENTIALS/);
  assert.equal(service.marksInvalid(), true);
});

test("connecting stores nothing when the 2FA check cannot get a turn", async () => {
  // Cupo interactivo de una sola petición: el login la gasta y la verificación 2FA queda sin turno.
  const { calls } = fakeEgress(
    [{ status: 200, body: json({ respuesta: true, token: jwt(inOneHour()), refreshToken: "refresh-1" }) }],
    { WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR: "1" },
  );
  const service = authenticated(null);
  await assert.rejects(service.exports.connectSeace("t1", "user-1", "20123456789", "contraseña-larga"),
    (error: unknown) => error instanceof SeaceUnavailable && error.reason === "busy");
  assert.equal(calls.length, 1);
  assert.equal(service.statements.some(({ sql }) => /INSERT INTO seace_connections/.test(sql)), false);
});

// ----------------------------------------------------- rutas autenticadas: SeaceUnavailable -> 503 JSON

function authenticatedRoutes(row: ReturnType<typeof connectionRow> | null) {
  const service = authenticated(row);
  const shared = {
    "next/server": nextServerStub,
    "@/server/auth/tenant": {
      requireTenantId: async () => "t1", currentUserId: async () => "user-1",
      requireTenantRole: async () => ({ tenantId: "t1", userId: "user-1" }),
    },
    "@/server/db/client": { query: async () => ({ rows: [] }), pool: {} },
    "@/server/http/seaceUnavailable": http(),
    "@/server/services/crud": { tenantCanAccessContract: async () => true },
    "@/server/services/seaceAuthenticated": service.exports,
    "@/server/services/seaceEgress": egress,
  };
  const applications = loadServerModule("./applications.ts", {
    ...shared,
    "@/server/services/tracking": { ensureMatchForContract: async () => ({ data: { id: "1" } }), ensureDefaultTasks: async () => {} },
  });
  const load = (path: string, extra: Record<string, unknown> = {}) =>
    loadServerModule(path, { ...shared, ...extra }).exports;
  return {
    service,
    quoteContext: load("../../app/api/contracts/[id]/seace/quote-context/route.ts"),
    consultations: load("../../app/api/contracts/[id]/seace/consultations/route.ts"),
    applications: load("../../app/api/contracts/[id]/applications/route.ts", { "@/server/services/applications": applications.exports }),
    integrations: load("../../app/api/integrations/seace/route.ts"),
  };
}

async function expectUnavailable(response: Response, message: string) {
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("retry-after"), "600");
  const body = await response.json();
  assert.equal(body.code, "SEACE_UNAVAILABLE");
  assert.equal(body.error, message);
}

test("every authenticated route answers a controlled 503 when the egress is closed, never an uncontrolled 500", async () => {
  const { calls } = fakeEgress([{ status: 200, body: json({}) }], { SEACE_EGRESS_REQUIRED: "1" });
  const routes = authenticatedRoutes(connectionRow(inOneHour()));
  const context = { params: Promise.resolve({ id: "9600001" }) };
  const request = () => new Request("https://local.test/api?estado=0&page=1&page_size=10");

  await expectUnavailable(await routes.quoteContext.GET(request(), context), "SEACE no respondió con la cotización.");
  await expectUnavailable(await routes.consultations.GET(request(), context), "SEACE no respondió a la consulta.");
  await expectUnavailable(await routes.applications.POST(request(), context),
    "SEACE no está disponible para iniciar la postulación. Inténtalo en unos minutos.");
  await expectUnavailable(
    await routes.integrations.PUT(new Request("https://local.test/api", {
      method: "PUT", body: json({ username: "20123456789", password: "contraseña-larga" }),
    })),
    "SEACE no está disponible en este momento. Inténtalo en unos minutos.",
  );
  assert.equal(calls.length, 0);
  assert.equal(routes.service.directFetches.length, 0);
  assert.equal(routes.service.statements.some(({ sql }) => /INSERT INTO seace_connections/.test(sql)), false);
});

// ------------------------------------------------------------------------------------------- codexAgent

function codex() {
  return loadServerModule("../agent/codexAgent.ts", {
    "./licitationAgent": {}, "./codexSessionStore": {}, "./codexCommand": {}, "./documentDelivery": {},
    "@/server/services/seaceEgress": egress,
  });
}

const TDR = "https://prod6.seace.gob.pe/v1/s8uit-services/archivo/descargar?id=1";

test("the Codex agent downloads originals through the egress and validates the host after every redirect", async (t) => {
  const workspace = mkdtempSync(join(tmpdir(), "seace-codex-test-"));
  t.after(() => rmSync(workspace, { recursive: true, force: true }));
  const { calls } = fakeEgress([
    { status: 302, headers: { location: "/SeaceWeb-PRO/otro.pdf" } },
    { status: 200, body: PDF },
    { status: 302, headers: { location: "https://evil.example/x.pdf" } },
    { status: 403, body: "waf" },
  ], { SEACE_EGRESS_PROXY: PROXY });
  const { exports, directFetches } = codex();
  const document = (sourceId: string) => ({ sourceId, filename: `${sourceId}.pdf`, downloadUrl: TDR, sizeBytes: PDF.length });
  const result = await exports.materializeSourceDocuments(workspace, { sources: { documents: [
    document("a1"), document("a2"), document("a3"), document("a4"),
  ] } });

  assert.deepEqual([...result.sources.documents].map((item: any) => item.localStatus),
    ["available", "download-failed", "download-failed", "download-failed"]);
  // a1 siguió un salto dentro de SEACE; a2 intentó salir a un host ajeno y no se hizo esa petición; a3 recibió 403
  // y a4 ya no salió porque la pausa quedó abierta.
  assert.deepEqual(calls.map(({ url }) => url), [
    TDR, "https://prod6.seace.gob.pe/SeaceWeb-PRO/otro.pdf", TDR, TDR,
  ]);
  assert.ok(calls.every(({ init }) => init.dispatcher && init.redirect === "manual"));
  assert.equal(readdirSync(join(workspace, "context", "source-documents")).length, 1);
  assert.deepEqual(directFetches, []);
});

test("the Codex agent marks documents as not downloaded, without throwing, when the egress is closed", async (t) => {
  const workspace = mkdtempSync(join(tmpdir(), "seace-codex-test-"));
  t.after(() => rmSync(workspace, { recursive: true, force: true }));
  const { calls } = fakeEgress([{ status: 200, body: PDF }], { SEACE_EGRESS_REQUIRED: "1" });
  const { exports, directFetches } = codex();
  const result = await exports.materializeSourceDocuments(workspace, { sources: { documents: [
    { sourceId: "a1", filename: "a1.pdf", downloadUrl: TDR, sizeBytes: PDF.length },
  ] } });
  assert.equal(result.sources.documents[0].localStatus, "download-failed");
  assert.equal(calls.length, 0);
  assert.deepEqual(directFetches, []);
});

test("the Codex agent refuses foreign hosts up front and gives up after too many SEACE redirects", async () => {
  const { calls } = fakeEgress(Array.from({ length: 6 }, () => ({ status: 302, headers: { location: "/loop" } })));
  const { exports } = codex();
  await assert.rejects(exports.fetchOfficialDocument("https://evil.example/x.pdf"), /invalid SEACE URL/);
  await assert.rejects(exports.fetchOfficialDocument("http://prod6.seace.gob.pe/x.pdf"), /invalid SEACE URL/);
  assert.equal(calls.length, 0);
  await assert.rejects(exports.fetchOfficialDocument(TDR), /too many SEACE redirects/);
  assert.equal(calls.length, 4);
});
