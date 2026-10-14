const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require(path.resolve(__dirname, "../../../../node_modules/typescript"));

const filename = path.join(__dirname, "DecisionOverview.tsx");
const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
const local = new Module(filename, module);
local.filename = filename;
local.paths = Module._nodeModulePaths(__dirname);
const standardRequire = local.require.bind(local);
local.require = name => {
  if (name.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => key === "__esModule" ? false : String(key) });
  if (name === "@/lib/scorePresentation") {
    const helperFile = path.resolve(__dirname, "../../../../lib/scorePresentation.ts");
    const helper = new Module(helperFile, module);
    helper._compile(ts.transpileModule(fs.readFileSync(helperFile, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText, helperFile);
    return helper.exports;
  }
  return standardRequire(name);
};
local._compile(source, filename);
const { DecisionOverview } = local.exports;
const render = props => renderToStaticMarkup(React.createElement(DecisionOverview, {
  analyzed: true, verdict: "Preparada", verdictTone: "green", fit: "Alta",
  deadline: "Abierta", deadlineDate: "14/10/2026", requirementsTotal: 3,
  missingCount: 3, coveredCount: 0, actions: null, ...props,
}));

test("a high provisional score uses green without claiming an approved decision", () => {
  const html = render({ score: null, provisionalScore: 95, scoreUnderReview: true });
  assert.match(html, /aria-label="95 de 100, puntaje provisional"/);
  assert.match(html, /<strong>95<\/strong>/);
  assert.match(html, /Puntaje provisional/);
  assert.match(html, /pendiente de validación/);
  assert.match(html, /tone_green/);
  assert.match(html, /data-score-tone="verde"/);
  assert.doesNotMatch(html, /Preparada/);
});

test("a stored provisional zero is distinguishable from an unknown score", () => {
  const zero = render({ score: null, provisionalScore: 0, scoreUnderReview: true });
  assert.match(zero, /aria-label="0 de 100, puntaje provisional"/);
  assert.match(zero, /<strong>0<\/strong>/);
  for (const provisionalScore of [null, undefined, "", "  ", NaN, Infinity, -1, 101]) {
    const unknown = render({ score: null, provisionalScore, scoreUnderReview: true });
    assert.match(unknown, /Puntaje por validar/);
    assert.doesNotMatch(unknown, /aria-label="0 de 100|<strong>0<\/strong>/);
  }
});

test("a retained public score cannot be used as a substitute for missing provisional data", () => {
  const html = render({ score: 95, provisionalScore: null, scoreUnderReview: true });
  assert.match(html, /Puntaje por validar/);
  assert.doesNotMatch(html, /<strong>95<\/strong>|tone_green/);
});

test("a validated zero uses red even when the stored evidence verdict is green", () => {
  const html = render({ score: 0, scoreUnderReview: false, provisionalScore: 95 });
  assert.match(html, /aria-label="0 de 100"/);
  assert.match(html, /<strong>0<\/strong>/);
  assert.match(html, /tone_red/);
  assert.match(html, /data-score-tone="rojo"/);
  assert.match(html, /Preparada/);
  assert.doesNotMatch(html, /Puntaje provisional|pendiente de validación/);
});

test("both final and provisional gauges obey the requested score boundaries", () => {
  for (const [score, tone] of [[0, "rojo"], [29, "rojo"], [29.9, "rojo"], [30, "ambar"], [80, "ambar"], [80.1, "verde"], [81, "verde"], [100, "verde"]]) {
    for (const scoreUnderReview of [false, true]) {
      const html = render({ score, provisionalScore: score, scoreUnderReview });
      assert.match(html, new RegExp(`data-score-tone="${tone}"`));
      if (scoreUnderReview) assert.match(html, /pendiente de validación/);
    }
  }
});

test("out-of-range final scores do not turn into a clamped green or red grade", () => {
  for (const score of [-1, 101, NaN, Infinity, null, undefined, "", "  "]) {
    const html = render({ score, scoreUnderReview: false });
    assert.match(html, /Puntaje no disponible/);
    assert.doesNotMatch(html, /data-score-tone="verde"|data-score-tone="rojo"/);
  }
});

test("no analysis never receives a numeric score even if incidental fields exist", () => {
  const html = render({ analyzed: false, score: 95, provisionalScore: 95, scoreUnderReview: true });
  assert.match(html, /Puntaje por validar/);
  assert.doesNotMatch(html, /<strong>95<\/strong>/);
});
