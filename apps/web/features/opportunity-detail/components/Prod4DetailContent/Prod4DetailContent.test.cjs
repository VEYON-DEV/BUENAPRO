const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const filename = path.join(__dirname, "Prod4DetailContent.tsx");
const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
const source = new Module(filename, module);
source.filename = filename;
source.paths = Module._nodeModulePaths(__dirname);
const normalRequire = source.require.bind(source);
source.require = specifier => {
  if (specifier.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => key === "__esModule" ? false : String(key) });
  if (specifier === "next/link") return ({ href, children, ...props }) => React.createElement("a", { href, ...props }, children);
  if (specifier === "@/components/ui/Badge") return { Badge: ({ children }) => React.createElement("span", null, children) };
  if (specifier === "@/features/procurement") return { ParticipationTerms: () => null, ProcurementSchedule: () => null };
  if (specifier === "@/lib/participationTerms") return { participationReading: value => value };
  if (specifier === "@/lib/format/date") return { formatDateTime: value => value };
  if (specifier === "@/lib/extraction/opportunity") return {
    verdictShortLabels: { gris: "Revisar", verde: "Preparada" },
    opportunityFacts: () => ({ observaciones: [], lecturaRapida: "Bases históricas", roles: [] }),
  };
  if (specifier === "../DecisionOverview") return { DecisionOverview: ({ actions }) => React.createElement("section", null, actions) };
  if (specifier === "../DetailSectionNavigator") return { DetailSectionNavigator: ({ children }) => React.createElement("div", null, children) };
  if (specifier === "../Prod4AnalyzeButton") throw new Error("Retired detail must never import an analysis trigger");
  return normalRequire(specifier);
};
source._compile(compiled, filename);
const { Prod4DetailContent } = source.exports;

function fixture(sourceUrl, match) {
  return {
    process: { id_procedimiento: "42", nomenclatura: "CP-42-2026", object_type: "service", title: "Concurso histórico", source_window_status: "exited", source_url: sourceUrl, buyer_name: "Entidad" },
    documents: [], items: [], official_schedule: [],
    analysis: { status: "matched", extraction: { id: "1", quality: "auto", requires_human_review: false, raw_extraction_json: {}, created_at: "2026-10-05" },
      facets: [{ id: "1", label: "Experiencia", details_json: {}, evidence_json: [] }], match },
  };
}
const render = detail => renderToStaticMarkup(React.createElement(Prod4DetailContent, { detail }));

test("retired PROD4 detail keeps the official primary action instead of an IA analysis trigger", () => {
  for (const match of [null, { score: 95, verdict: "verde", breakdown_json: { requisitos: [] } }]) {
    const html = render(fixture("https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/42", match));
    assert.match(html, /class="primaryAction"[^>]*href="https:\/\/prod4\.seace\.gob\.pe\/openegocio\/#\/ficha\/idProceso\/42"/);
    assert.match(html, /Revisar en SEACE/);
    assert.doesNotMatch(html, /Analizar con IA|Volver a analizar|Consultar análisis|api\/prod4\/.*analyze/);
  }
});

test("retired historical detail falls back to the stable official portal when a deep link is missing", () => {
  const html = render(fixture(null, null));
  assert.match(html, /class="primaryAction"[^>]*href="https:\/\/prod4\.seace\.gob\.pe\/openegocio\/"/);
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(html, /Analizar con IA|Volver a analizar/);
});
