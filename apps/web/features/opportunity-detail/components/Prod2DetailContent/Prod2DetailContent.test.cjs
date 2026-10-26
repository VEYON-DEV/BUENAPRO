const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const webRoot = path.resolve(__dirname, "../../../..");
const ts = require(path.join(webRoot, "node_modules/typescript"));
const loaded = new Map();
const detailRoot = path.join(webRoot, "features/opportunity-detail");

function loadSource(filename) {
  if (loaded.has(filename)) return loaded.get(filename).exports;
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const local = new Module(filename, module);
  local.filename = filename;
  local.paths = Module._nodeModulePaths(path.dirname(filename));
  const normalRequire = local.require.bind(local);
  local.require = (specifier) => {
    if (specifier.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => key === "__esModule" ? false : String(key) });
    if (specifier === "next/link") return ({ href, children, ...props }) => React.createElement("a", { href, ...props }, children);
    if (specifier === "@/components/ui/Badge") return { Badge: ({ children }) => React.createElement("span", null, children) };
    if (specifier === "@/features/opportunities") return { SaveButton: ({ initialSaved }) => React.createElement("button", { "data-save": initialSaved ? "saved" : "unsaved" }, "Guardar") };
    if (specifier === "@/features/procurement") return { ParticipationTerms: () => null, ProcurementSchedule: () => null };
    if (specifier === "@/lib/participationTerms") return { participationReading: value => value };
    if (specifier === "@/lib/keywordAffinity") return { keywordAffinitySummary: () => null };
    if (specifier === "@/lib/scorePresentation") return loadSource(path.join(webRoot, "lib/scorePresentation.ts"));
    if (specifier === "@/lib/format/date") return { formatDateTime: value => value };
    if (specifier === "@/lib/extraction/opportunity") return loadSource(path.join(webRoot, "lib/extraction/opportunity.ts"));
    if (specifier === "../DecisionOverview") return loadSource(path.join(detailRoot, "components/DecisionOverview/DecisionOverview.tsx"));
    if (specifier === "../AdministrativeChecklist") return loadSource(path.join(detailRoot, "components/AdministrativeChecklist/AdministrativeChecklist.tsx"));
    if (specifier === "../DetailSectionNavigator") return { DetailSectionNavigator: ({ children }) => React.createElement("div", null, children) };
    if (specifier === "../../model/evaluationPresentation") return loadSource(path.join(detailRoot, "model/evaluationPresentation.ts"));
    if (specifier === "../../model/requirementText") return loadSource(path.join(detailRoot, "model/requirementText.ts"));
    return normalRequire(specifier);
  };
  loaded.set(filename, local);
  local._compile(compiled, filename);
  return local.exports;
}

const { Prod2DetailContent } = loadSource(path.join(__dirname, "Prod2DetailContent.tsx"));

function fixture(breakdown, facets, score = null) {
  return {
    process: { id_convocatoria: "source-123", nomenclatura: "AS-1-2026", procedure_type: "Selección", object_type: "service", description: "Servicio de API", buyer_name: "Entidad", source_url: "https://example.invalid/seace", document_inventory_complete: true },
    documents: [{ codigo_alfresco: "document-source", name: "Bases.pdf", extension: "pdf", download_url: "https://example.invalid/bases.pdf" }],
    official_schedule: [], profile: { id: "profile-dinaut", name: "DINAUT" },
    analysis: {
      extraction: { id: "extraction-1", codigo_alfresco: "document-source", raw_extraction_json: {}, requires_human_review: false },
      facets,
      match: breakdown ? { id: "match-source", score, verdict: "gris", breakdown_json: breakdown, missing_actions_json: [] } : null,
    },
  };
}
const render = detail => renderToStaticMarkup(React.createElement(Prod2DetailContent, { detail }));

test('published listing metadata never claims a ficha, documents, deadline or evaluated score', () => {
  const detail = fixture(null, []);
  detail.documents = [];
  detail.analysis.extraction = null;
  detail.process.has_official_detail = false;
  detail.process.source_url = 'https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml';
  detail.process.publication_status_label = 'Cerrada al revisar';
  const html = render(detail);
  assert.match(html, /Solo datos del listado/);
  assert.match(html, /Cerrada al revisar\. Estado conservado de la última revisión/);
  assert.match(html, /Fecha no informada/);
  assert.match(html, /No hay documentos registrados en esta ficha/);
  assert.doesNotMatch(html, /Abrir bases en PDF|<strong>0<\/strong>/);
});

test('internal JEV affinity never reaches the client detail, even if a stale payload carries it', () => {
  const detail = fixture(null, []);
  detail.analysis.extraction = null;
  detail.process.commercial_affinity = { index: 88, label: 'Afinidad comercial', basis: 'title' };
  detail.process.affinity = { index: 77, label: 'Afinidad comercial' };
  const html = render(detail);
  assert.doesNotMatch(html, /JEV|Afinidad comercial|88 \/ 100|77 \/ 100|por título/i);
  assert.match(html, /Evaluación documental pendiente/);
  assert.doesNotMatch(html, /Puntaje provisional|<strong>0<\/strong>/);
});

test("PROD2 detail uses the explicit provisional score while keeping documentary review visible", () => {
  const detail = fixture({ meta: { score_validated: false }, resumen: "Experiencia por confirmar", requisitos: [
    { requisito: "Experiencia", source_index: 1, source_facet: "economic_experience", estado: "requiere_revision" },
  ] }, [{ id: "1", facet: "economic_experience", label: "Experiencia", required: true }]);
  detail.analysis.match.provisional_score = 95;
  detail.analysis.extraction.requires_human_review = true;
  detail.process.match_review_required = true;
  const html = render(detail);
  assert.match(html, /95 de 100, puntaje provisional/);
  assert.match(html, /Puntaje provisional/);
  assert.match(html, /data-score-tone="verde"/);
  assert.match(html, /Las bases requieren revisión humana/);
  assert.match(html, /0 de 1/);
});

test("source-review provisional zero never becomes an approved legacy score in the detail", () => {
  const detail = fixture({ meta: { score_validated: true }, requisitos: [
    { requisito: "Personal", source_index: 1, source_facet: "key_personnel", estado: "requiere_revision" },
  ] }, [{ id: "1", facet: "key_personnel", label: "Personal", required: true }], 0);
  detail.analysis.match.provisional_score = 0;
  detail.analysis.match.verdict = "verde";
  detail.process.match_review_required = true;
  const html = render(detail);
  assert.match(html, /0 de 100, puntaje provisional/);
  assert.match(html, /Puntaje provisional/);
  assert.match(html, /tone_red/);
  assert.match(html, /data-score-tone="rojo"/);
  assert.doesNotMatch(html, /tone_green|Preparada/);
});

test("PROD2 retained score preserves analysis, substantive coverage, technical proposal and separate preparation", () => {
  const facets = [
    { id: "1", facet: "rnp", label: "RNP vigente", required: true },
    { id: "2", facet: "penalty_condition", label: "Mora", required: true },
    { id: "3", facet: "proposal_document", label: "Propuesta técnica API y SLA", required: true, evidence_json: [{ quote: "Servicio API disponible", page: 4 }] },
    { id: "4", facet: "key_personnel", label: "Personal", required: true },
    { id: "5", facet: "economic_experience", label: "Experiencia compatible", required: true },
  ];
  const html = render(fixture({
    resumen: "Validar experiencia computable; análisis disponible.",
    meta: { score_validated: false, raw_model_score: 95 },
    requisitos: [
      { requisito: facets[2].label, source_index: 3, source_facet: "proposal_document", categoria: "documentacion", estado: "requiere_revision", gap: "Confirmar SLA" },
      { requisito: "Personal", source_index: 4, source_facet: "key_personnel", estado: "cumple" },
      { requisito: "Experiencia compatible", source_index: 5, source_facet: "economic_experience", estado: "requiere_revision", gap: "Separar monedas y bienes" },
    ],
    administrative_checklist: [{ tipo: "rnp", requisito: "RNP vigente", estado: "preparar", score_impact: false }],
  }, facets));
  assert.match(html, /Puntaje por validar/);
  assert.doesNotMatch(html, /scoreGauge|<strong>95<\/strong>/);
  assert.match(html, /1 de 3/);
  assert.match(html, /Confirmar SLA/);
  assert.match(html, /Servicio API disponible · Página 4/);
  assert.equal((html.match(/RNP vigente/g) ?? []).length, 1);
  assert.match(html, /<details class="checklist"><summary>/);
  assert.match(html, /profile_id=profile-dinaut/);
  assert.match(html, /https:\/\/example.invalid\/bases.pdf/);
});

test("PROD2 legacy joint attribution and desirable evidence remain visible without inflating coverage", () => {
  const facets = [
    { id: "1", facet: "specific_experience", label: "Experiencia", required: true },
    { id: "2", facet: "training", label: "Capacitación deseable", required: false },
  ];
  const html = render(fixture({
    meta: { member_names: { "member-a": "Empresa A", "member-b": "Empresa B" } },
    requisitos: [
      { requirement_id: "r1", requisito: "Experiencia", categoria: "experiencia", estado: "cumple", member_gaps: [{ profile_id: "member-b", estado: "requiere_revision", gap: "Confirmar alcance de miembro" }] },
      { requirement_id: "r2", requisito: "Capacitación deseable", categoria: "formacion", estado: "requiere_revision" },
    ],
  }, facets, 42));
  assert.match(html, /Empresa A \+ Empresa B/);
  assert.match(html, /Confirmar alcance de miembro/);
  assert.match(html, /Deseable \/ no obligatorio/);
  assert.match(html, /1 de 1/);
  assert.match(html, /<strong>42<\/strong>/);
});

test("already read bases without an evaluation keep source evidence and a pending score", () => {
  const html = render(fixture(null, [
    { id: "1", facet: "ruc_status", label: "RUC vigente", required: true },
    { id: "2", facet: "license", label: "Licencia especializada", required: true, evidence_json: { quote: "Autorización obligatoria", page: 2 } },
  ]));
  assert.match(html, /Evaluación pendiente/);
  assert.match(html, /Por analizar/);
  assert.match(html, /Licencia especializada/);
  assert.match(html, /Autorización obligatoria · Página 2/);
  assert.equal((html.match(/RUC vigente/g) ?? []).length, 1);
  assert.doesNotMatch(html, /<strong>0<\/strong>/);
});

test("a validated zero score remains zero and is distinct from a withheld score", () => {
  const html = render(fixture({
    meta: { score_validated: true },
    requisitos: [{ requisito: "Licencia requerida", source_index: 1, source_facet: "license", estado: "no_cumple", gap: "No se acredita autorización" }],
    administrative_checklist: [],
  }, [{ id: "1", facet: "license", label: "Licencia requerida", required: true }], 0));
  assert.match(html, /<strong>0<\/strong>/);
  assert.match(html, /0 de 1/);
  assert.doesNotMatch(html, /Puntaje por validar/);
});

test("MYPE branches remain visible as conditional and never increase required coverage", () => {
  const html = render(fixture({
    meta: { score_validated: false },
    requisitos: [
      { requisito: "Experiencia general", source_index: 1, source_facet: "specific_experience", estado: "cumple" },
      { requisito: "Experiencia bajo condición MYPE", source_index: 2, source_facet: "economic_experience", conditional_branch: "mype", required: false, estado: "requiere_revision", accion: "Verificar condición MYPE" },
    ],
    administrative_checklist: [],
  }, [
    { id: "1", facet: "specific_experience", label: "Experiencia general", required: true },
    { id: "2", facet: "economic_experience", label: "Experiencia bajo condición MYPE", required: true },
  ]));
  assert.match(html, /Condicional · MYPE/);
  assert.match(html, /Verificar condición MYPE/);
  assert.match(html, /1 de 1/);
  assert.doesNotMatch(html, /Deseable \/ no obligatorio/);
});

test("complete saved source text is readable in the summary, capacity requirement and every gap", () => {
  const fullSummary = "Resumen íntegro: entrega en 45 días y acreditación de experiencia sin recortar.";
  const facets = [
    { id: "1", label: "RNP", facet: "rnp", required: true, details_json: { descripcion: "Texto que no corresponde a experiencia" } },
    ...Array.from({ length: 4 }, (_, i) => ({ id: String(i + 2), label: `Experiencia ${i + 1}…`, facet: "specific_experience", required: true,
      details_json: { descripcion: `Experiencia ${i + 1} completa con monto, periodo, bienes compatibles y respaldo documental.`, evidencia: "Bases" } })),
  ];
  const detail = fixture({ administrative_checklist: [], requisitos: facets.slice(1).map((facet, i) => ({ requisito: facet.label,
    source_index: i + 1, source_facet: facet.facet, estado: "requiere_revision", gap: `Brecha ${i + 1}` })) }, facets);
  detail.analysis.extraction.raw_extraction_json = { summary: { descripcion_corta: fullSummary } };
  detail.analysis.extraction.summary_json = { descripcion_corta: "Resumen…" };
  const html = render(detail);
  assert.match(html, new RegExp(fullSummary));
  assert.equal((html.match(/Ver requisito completo/g) ?? []).length, 4);
  assert.match(html, /Ver 1 brecha más/);
  for (const facet of facets.slice(1)) assert.equal((html.split(facet.details_json.descripcion).length - 1), 2);
  assert.doesNotMatch(html, /Texto que no corresponde a experiencia/);
});
