const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require(path.resolve(__dirname, "../../../../node_modules/typescript"));

const webRoot = path.resolve(__dirname, "../../../..");
const cache = new Map();
function load(filename) {
  if (cache.has(filename)) return cache.get(filename).exports;
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const local = new Module(filename, module);
  local.filename = filename;
  local.paths = Module._nodeModulePaths(path.dirname(filename));
  const standardRequire = local.require.bind(local);
  local.require = (name) => {
    if (name.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => (key === "__esModule" ? false : String(key)) });
    if (name.startsWith("@/")) {
      const base = path.join(webRoot, name.slice(2));
      const file = [".ts", ".tsx", "/index.ts", "/index.tsx"].map((suffix) => base + suffix).find(fs.existsSync);
      if (file) return load(file);
    }
    if (name.startsWith(".")) {
      const base = path.resolve(path.dirname(filename), name);
      const file = [".ts", ".tsx", "/index.ts", "/index.tsx"].map((suffix) => base + suffix).find(fs.existsSync);
      if (file) return load(file);
    }
    return standardRequire(name);
  };
  cache.set(filename, local);
  local._compile(source, filename);
  return local.exports;
}

const { CompanyAffinity } = load(path.join(__dirname, "CompanyAffinity.tsx"));
const render = (rows) => renderToStaticMarkup(React.createElement(CompanyAffinity, { rows }));

const accepted = {
  profile_id: "a", profile_name: "VEYON SAC", is_current_profile: true, score: 91.25, threshold: 60, decision: "accepted", decided_by: "jev",
  subcategory: "licencias_software", domain: "informatica", information: "claro", decided_at: "2026-10-25T00:20:36Z",
};

test("renders nothing when the contract has no JEV decisions", () => {
  for (const rows of [undefined, null, [], [{}]]) assert.equal(render(rows), "");
});

test("shows one row per company with a 0-100 meter, threshold, state text and the disclaimer", () => {
  const html = render([accepted, { profile_id: "b", profile_name: "OTRA SAC", score: 41, threshold: 60, decision: "rejected", decided_by: "jev",
    subcategory: "otro", domain: "ambiguo", information: "insuficiente" }]);
  assert.match(html, /<h2[^>]*>Afinidad por empresa<\/h2>/);
  assert.match(html, /Calificación preliminar de afinidad comercial \(JEV\); no evalúa requisitos ni elegibilidad\./);
  assert.equal((html.match(/<li /g) ?? []).length, 2);
  assert.match(html, /VEYON SAC/);
  assert.match(html, /Perfil activo/);
  assert.equal((html.match(/Perfil activo/g) ?? []).length, 1);
  assert.match(html, /role="meter"/);
  assert.match(html, /aria-valuenow="91"/);
  assert.match(html, /aria-valuemin="0"/);
  assert.match(html, /aria-valuemax="100"/);
  assert.match(html, /aria-valuetext="91 de 100; umbral 60"/);
  assert.match(html, /style="width:91%"/);
  assert.match(html, /--threshold:60%/);
  assert.match(html, /Umbral 60/);
  assert.match(html, />Aprobada</);
  assert.match(html, />Descartada</);
  assert.match(html, /Licencias de software · Informática/);
  assert.match(html, /Información suficiente/);
  assert.match(html, /Información insuficiente/);
  assert.match(html, /class="[^"]*\bgreen\b/);
  assert.match(html, /class="[^"]*\bred\b/);
});

test("rows without a score show an explanation instead of an invented meter", () => {
  const html = render([
    { profile_id: "c", profile_name: "PENDIENTE SAC", score: null, decision: "pending", decided_by: "jev" },
    { profile_id: "d", profile_name: "PREFILTRO SAC", score: null, decision: "rejected", decided_by: "prefilter" },
  ]);
  assert.doesNotMatch(html, /role="meter"/);
  assert.match(html, />Pendiente</);
  assert.match(html, /En espera de la calificación JEV\./);
  assert.match(html, /Sin calificación JEV: decidida por prefiltro\./);
  assert.match(html, /class="[^"]*\bamber\b/);
});

test("a zero score is a real score and keeps its meter", () => {
  const html = render([{ ...accepted, score: 0, decision: "rejected" }]);
  assert.match(html, /aria-valuenow="0"/);
  assert.match(html, /style="width:0%"/);
});
