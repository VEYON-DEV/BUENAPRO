const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require(path.resolve(__dirname, "../../../../node_modules/typescript"));

const webRoot = path.resolve(__dirname, "../../../..");
function load(filename) {
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const local = new Module(filename, module);
  local.filename = filename;
  local.paths = Module._nodeModulePaths(path.dirname(filename));
  const standardRequire = local.require.bind(local);
  local.require = (name) => {
    if (name.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => (key === "__esModule" ? false : String(key)) });
    if (name === "@/lib/jevAffinity") return load(path.join(webRoot, "lib/jevAffinity.ts"));
    return standardRequire(name);
  };
  local._compile(source, filename);
  return local.exports;
}

const { JevAffinityMark } = load(path.join(__dirname, "JevAffinityMark.tsx"));
const { jevAffinity } = load(path.join(webRoot, "lib/jevAffinity.ts"));
const affinity = jevAffinity({ jev_score: 77.6, jev_decision: "accepted", jev_subcategory: "desarrollo_sistemas", jev_threshold: 60 });
const render = (variant) => renderToStaticMarkup(React.createElement(JevAffinityMark, { affinity, variant }));

test("row variant is compact: percentage and label, with the full explanation as tooltip", () => {
  const html = render("row");
  assert.match(html, /<strong>78 \/ 100<\/strong>/);
  assert.match(html, /Afinidad JEV/);
  assert.doesNotMatch(html, /Desarrollo de sistemas<\/span>/, "the subcategory stays in the inspector");
  assert.match(html, /title="Afinidad JEV: 78 de 100\. Desarrollo de sistemas\. umbral 60\./);
  assert.doesNotMatch(html, /<svg/);
});

test("inspector variant adds subcategory, threshold and the no-eligibility disclaimer", () => {
  const html = render("inspector");
  assert.match(html, /<strong>78<\/strong><small>\/ 100<\/small>/);
  assert.match(html, /Desarrollo de sistemas/);
  assert.match(html, /Umbral de aprobación: 60\./);
  assert.match(html, /no evalúa requisitos ni elegibilidad/);
  assert.match(html, /aria-hidden="true"/);
});
