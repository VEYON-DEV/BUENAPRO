const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const feature = path.resolve(__dirname, "..");
const loaded = new Map();
let selectedStates = null;
function loadSource(filename) {
  if (loaded.has(filename)) return loaded.get(filename).exports;
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const local = new Module(filename, module);
  local.filename = filename;
  local.paths = Module._nodeModulePaths(path.dirname(filename));
  const normalRequire = local.require.bind(local);
  local.require = specifier => {
    if (specifier === "react") return { ...React, useState: value => selectedStates?.length ? [selectedStates.shift(), () => {}] : React.useState(value) };
    if (specifier.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => key === "__esModule" ? false : String(key) });
    if (specifier === "next/link") return ({ href, children, ...props }) => React.createElement("a", { href, ...props }, children);
    if (specifier === "@/components/ui/EmptyState") return { EmptyState: ({ title, children, action }) => React.createElement("div", null, title, children, React.createElement("a", { href: action.href }, action.label)) };
    if (specifier === "@/components/ui/Input") return { Input: props => React.createElement("input", props) };
    if (specifier === "@/components/ui/Select") return { Select: props => React.createElement("select", props) };
    if (specifier === "@/components/ui/AppIcon") return { AppIcon: () => React.createElement("span", { "aria-hidden": true }) };
    if (specifier === "@/features/procurement") return { ParticipationStatus: () => React.createElement("span", null, "Por leer"), ParticipationTerms: () => null, ProcurementSchedule: () => null };
    if (specifier === "@/lib/participationTerms") return { participationReading: () => ({ subcontratacion: {}, consorcio: {} }) };
    if (specifier === "@/lib/keywordAffinity") return { keywordAffinitySummary: row => row.fit_points == null ? null : ({ level: 3, label: "Afinidad alta", description: "Afinidad por keywords", lineName: "Software", pointLabel: "40", hits: ["software"] }) };
    if (specifier === "@/lib/scorePresentation") return loadSource(path.resolve(feature, "../../lib/scorePresentation.ts"));
    if (specifier === "@/lib/format/date") return { formatDeadline: () => "Cierra pronto", formatDateTime: value => value, formatShortDateTime: value => value };
    if (specifier === "@/lib/format/money") return { formatMoney: value => value };
    if (specifier === "@/lib/extraction/opportunity") return {
      verdictShortLabels: { gris: "Revisar", verde: "Preparada", rojo: "Sin encaje" },
      FIT_EXACTO: 70, FIT_RELACIONADO: 40, fitScore: value => Number(value), fitLabel: () => "Afinidad alta", fitShortLabel: () => "Alta",
      cotizacionStatus: () => ({ key: "abierta", label: "Cotización abierta" }),
      opportunityFacts: row => ({ descripcionCorta: row?.descripcion, roles: [] }), plazoLabel: () => "Por definir",
    };
    if (specifier === "../SaveButton") return { SaveButton: () => null };
    if (specifier === "../../api/seaceDetail") return { seaceDetailRequest: () => { throw new Error("SSR must not fetch"); } };
    if (specifier === "../../model/analysisPresentation") return loadSource(path.join(__dirname, "analysisPresentation.ts"));
    return normalRequire(specifier);
  };
  loaded.set(filename, local);
  local._compile(compiled, filename);
  return local.exports;
}

const { analysisPresentation, analysisFilterHref, provisionalAnalysisScore } = loadSource(path.join(__dirname, "analysisPresentation.ts"));
const { Prod4OpportunityList } = loadSource(path.join(feature, "components/Prod4OpportunityList/Prod4OpportunityList.tsx"));
const { OpportunityList } = loadSource(path.join(feature, "components/OpportunityList/OpportunityList.tsx"));
const { Prod4OpportunityToolbar } = loadSource(path.join(feature, "components/Prod4OpportunityToolbar/Prod4OpportunityToolbar.tsx"));
const { OpportunityToolbar } = loadSource(path.join(feature, "components/OpportunityToolbar/OpportunityToolbar.tsx"));
const render = (Component, props, state = null) => {
  selectedStates = state ? [...state] : null;
  try { return renderToStaticMarkup(React.createElement(Component, props)); }
  finally { selectedStates = null; }
};
const procedure = overrides => ({
  id_procedimiento: "1046842", id_convocatoria: "1046842", opportunity_id: "source-backup", nomenclatura: "CP-1-2026", title: "Servicio de backup", description: "Cloud",
  object_type: "service", buyer_name: "Congreso", documents_count: 1, source_window_status: "observed", proposals_closes_at: null, registration_closes_at: null,
  fit_points: 40, match_score: null, match_verdict: "gris", ...overrides,
});
const minor = overrides => ({ id_contrato: "101615", codigo: "CM-1-2026", descripcion: "Software estadístico", entidad_nombre: "Entidad", fit_points: 40, score: null, verdict: "gris", ...overrides });

test("withheld scores are analysis only with an explicit stored identity", () => {
  assert.equal(analysisPresentation({ matchId: "130", score: null, verdict: "gris" }), "review");
  assert.equal(analysisPresentation({ matchId: 920, score: null, verdict: "gris", reviewRequired: true }), "review");
  assert.equal(analysisPresentation({ score: null, verdict: "gris", reviewRequired: true }), "none");
  for (const matchId of [null, undefined, 0, "0", ""]) assert.equal(analysisPresentation({ matchId, score: null }), "none");
});

test("published zero and legacy numeric evaluations remain distinct from withheld scores", () => {
  assert.equal(analysisPresentation({ score: 0, verdict: "rojo" }), "evaluated");
  assert.equal(analysisPresentation({ matchId: "129", score: "0", verdict: "gris", reviewRequired: false }), "evaluated");
  assert.equal(analysisPresentation({ matchId: "129", score: 35, verdict: "gris", reviewRequired: true }), "review");
});

test("provisional scores require an existing review match and an explicit finite numeric 0–100 field", () => {
  const input = { matchId: "130", score: null, verdict: "gris", reviewRequired: true };
  assert.equal(provisionalAnalysisScore(input, 0), 0);
  assert.equal(provisionalAnalysisScore(input, 95), 95);
  assert.equal(provisionalAnalysisScore(input, 100), 100);
  for (const invalid of [null, undefined, "95", "", false, NaN, Infinity, -1, 101, { raw_model_score: 95 }]) {
    assert.equal(provisionalAnalysisScore(input, invalid), null);
  }
  assert.equal(provisionalAnalysisScore({ verdict: "gris", score: null }, 95), null);
  assert.equal(provisionalAnalysisScore({ matchId: "130", verdict: "gris", score: 0, reviewRequired: false }, 95), null);
});

test("analysis filter toggles without losing profile, source, query or object and resets pagination", () => {
  const href = analysisFilterHref({ source: "prod2", profile_id: "dinaut-profile", q: "Adobe & cloud", object: "service", page: "3", page_size: "25" }, "prod2");
  const query = new URL(href, "https://buenapro.invalid").searchParams;
  assert.equal(query.get("analysis"), "done");
  assert.equal(query.get("profile_id"), "dinaut-profile");
  assert.equal(query.get("q"), "Adobe & cloud");
  assert.equal(query.get("object"), "service");
  assert.equal(query.get("page_size"), "25");
  assert.equal(query.get("source"), "prod2");
  assert.equal(query.has("page"), false);
  const unset = new URL(analysisFilterHref(Object.fromEntries(query)), "https://buenapro.invalid").searchParams;
  assert.equal(unset.has("analysis"), false);
  assert.equal(unset.get("profile_id"), "dinaut-profile");
  const minorQuery = new URL(analysisFilterHref({ source: "prod6", profile_id: "dinaut-profile", objeto: "2", deadline: "all", saved: "true", open_only: "true", closing_before: "date" }), "https://buenapro.invalid").searchParams;
  assert.equal(minorQuery.get("objeto"), "2");
  assert.equal(minorQuery.get("deadline"), "all");
  assert.equal(minorQuery.get("saved"), "true");
  assert.equal(minorQuery.get("open_only"), "true");
  assert.equal(minorQuery.get("closing_before"), "date");
});

test("PROD2 rows show available analysis with no invented score, while unevaluated gray rows keep keyword affinity", () => {
  const html = render(Prod4OpportunityList, { source: "prod2", profileId: "dinaut-profile", rows: [procedure({ match_id: "130", match_review_required: true })] });
  assert.match(html, /Analizado/);
  assert.match(html, /Puntaje por validar/);
  assert.doesNotMatch(html, /scoreRing|Evaluación de requisitos: 0/);
  const pending = render(Prod4OpportunityList, { source: "prod2", rows: [procedure({ match_id: null })] });
  assert.match(pending, /Afinidad alta/);
  assert.doesNotMatch(pending, /Analizado|Puntaje por validar/);
});

test("PROD2 inspector explains the retained score and links the exact selected profile", () => {
  const html = render(Prod4OpportunityList, { source: "prod2", profileId: "dinaut-profile", rows: [procedure({ match_id: "130", match_review_required: true })] }, ["1046842", null, "idle"]);
  assert.match(html, /Análisis de requisitos/);
  assert.match(html, /El análisis está disponible en el detalle/);
  assert.match(html, /1046842\?profile_id=dinaut-profile/);
  assert.match(html, /40 de 45 puntos por palabras clave/);
  assert.equal((html.match(/Puntaje por validar/g) ?? []).length, 2);
});

test("PROD2 green provisional score remains explicitly under review in row and inspector with profile intact", () => {
  const html = render(Prod4OpportunityList, { source: "prod2", profileId: "dinaut-profile", rows: [procedure({ match_id: "130", match_review_required: true, match_verdict: "verde", provisional_score: 95 })] }, ["1046842", null, "idle"]);
  assert.equal((html.match(/Puntaje provisional: 95 de 100\. Por validar\./g) ?? []).length, 2);
  assert.equal((html.match(/scoreRing[^>]*>95<\/span>/g) ?? []).length, 2);
  assert.match(html, /El puntaje sigue por validar/);
  assert.match(html, /1046842\?profile_id=dinaut-profile/);
  assert.equal((html.match(/data-score-tone="verde"/g) ?? []).length, 2);
  assert.equal((html.match(/<small>Por validar<\/small>/g) ?? []).length, 2);
  assert.doesNotMatch(html, /verdict_verde|Preparada|Evaluación de requisitos: 95/);
});

test("PROD2 provisional zero is real, absent or invalid scores stay withheld, and no-analysis rows never invent one", () => {
  const score = render(Prod4OpportunityList, { source: "prod2", rows: [procedure({ match_id: "130", match_review_required: true, provisional_score: 0 })] });
  assert.match(score, /Puntaje provisional: 0 de 100/);
  assert.match(score, /scoreRing[^>]*>0<\/span>/);
  for (const provisional_score of [null, undefined, -3, 115, "95"]) {
    const retained = render(Prod4OpportunityList, { source: "prod2", rows: [procedure({ match_id: "130", match_review_required: true, provisional_score })] });
    assert.match(retained, /Puntaje por validar/);
    assert.doesNotMatch(retained, /scoreRing/);
  }
  const pending = render(Prod4OpportunityList, { source: "prod2", rows: [procedure({ match_id: null, provisional_score: 95 })] });
  assert.match(pending, /Afinidad alta/);
  assert.doesNotMatch(pending, /Provisional|scoreRing/);
});

test("legacy PROD2 zero displays its ring instead of a retained-score label", () => {
  const html = render(Prod4OpportunityList, { rows: [procedure({ match_score: 0, match_verdict: "rojo" })], source: "prod2" });
  assert.match(html, /Evaluación de requisitos: 0 de 100/);
  assert.match(html, /scoreRing[^>]*>0<\/span>/);
  assert.doesNotMatch(html, /Puntaje por validar/);
});

test("minor row and inspector show the existing analysis instead of a prompt to rerun IA", () => {
  const html = render(OpportunityList, { rows: [minor({ match_id: "916", match_review_required: true })] });
  assert.equal((html.match(/Puntaje por validar/g) ?? []).length, 2);
  assert.match(html, /Revisa el análisis en el detalle/);
  assert.match(html, /href="\/oportunidad\/101615"/);
  assert.doesNotMatch(html, /evalúa con IA|análisis pendiente|scoreRing/);
  const pending = render(OpportunityList, { rows: [minor({ match_id: null })] });
  assert.match(pending, /Afinidad preliminar · análisis pendiente/);
  assert.doesNotMatch(pending, /Analizado|Puntaje por validar/);
});

test("minor zero score remains visible for an evaluated legacy match", () => {
  const html = render(OpportunityList, { rows: [minor({ match_id: "916", score: 0, verdict: "rojo" })] });
  assert.match(html, /scoreRing[^>]*>0<\/span>/);
  assert.doesNotMatch(html, /Puntaje por validar/);
});

test("minor provisional 0 and 95 follow numeric color without turning review into approval", () => {
  for (const provisional_score of [0, 95]) {
    const html = render(OpportunityList, { rows: [minor({ match_id: "916", match_review_required: true, verdict: "verde", provisional_score })] });
    assert.equal((html.match(new RegExp(`Puntaje provisional: ${provisional_score} de 100\\. Por validar\\.`, "g")) ?? []).length, 2);
    assert.equal((html.match(new RegExp(`scoreRing[^>]*>${provisional_score}<\\/span>`, "g")) ?? []).length, 2);
    assert.match(html, /Revisa el análisis en el detalle/);
    assert.equal((html.match(new RegExp(`data-score-tone="${provisional_score === 0 ? "rojo" : "verde"}"`, "g")) ?? []).length, 2);
    assert.equal((html.match(/<small>Por validar<\/small>/g) ?? []).length, 2);
    assert.doesNotMatch(html, /class="scoreMark green"|Preparada/);
  }
});

test("both published and provisional rings use score bands instead of the stored verdict", () => {
  for (const [score, tone] of [[0, "rojo"], [29, "rojo"], [30, "ambar"], [80, "ambar"], [81, "verde"], [100, "verde"]]) {
    const prod2 = render(Prod4OpportunityList, { source: "prod2", rows: [procedure({ match_id: "130", match_score: score, match_verdict: "gris", match_review_required: false })] });
    assert.match(prod2, new RegExp(`data-score-tone="${tone}"`));
    assert.match(prod2, /<strong>Revisar<\/strong>/);
    const prod2Provisional = render(Prod4OpportunityList, { source: "prod2", rows: [procedure({ match_id: "130", provisional_score: score, match_review_required: true })] });
    assert.match(prod2Provisional, new RegExp(`data-score-tone="${tone}"`));
    assert.match(prod2Provisional, /<strong>Provisional<\/strong><small>Por validar<\/small>/);
    const minorPublished = render(OpportunityList, { rows: [minor({ match_id: "916", score, verdict: "gris", match_review_required: false })] });
    assert.equal((minorPublished.match(new RegExp(`data-score-tone="${tone}"`, "g")) ?? []).length, 2);
    assert.match(minorPublished, /<strong>Revisar<\/strong>/);
    const minorProvisional = render(OpportunityList, { rows: [minor({ match_id: "916", provisional_score: score, match_review_required: true })] });
    assert.equal((minorProvisional.match(new RegExp(`data-score-tone="${tone}"`, "g")) ?? []).length, 2);
    assert.match(minorProvisional, /<strong>Provisional<\/strong><small>Por validar<\/small>/);
  }
});

test("invalid published scores become review or preliminary marks, never clamped colored numbers", () => {
  for (const score of [null, NaN, Infinity, -1, 101, false, " ", "not-a-score"]) {
    const prod2 = render(Prod4OpportunityList, { source: "prod2", rows: [procedure({ match_id: "130", match_score: score, match_verdict: "verde" })] });
    assert.match(prod2, /Puntaje por validar/);
    assert.doesNotMatch(prod2, /scoreRing|data-score-tone/);
    const minorHtml = render(OpportunityList, { rows: [minor({ match_id: "916", score, verdict: "verde" })] });
    assert.match(minorHtml, /Puntaje por validar/);
    assert.doesNotMatch(minorHtml, /scoreRing|data-score-tone/);
  }
});

test("minor invalid or missing provisional data never becomes zero, while unevaluated rows remain preliminary", () => {
  for (const provisional_score of [null, undefined, -1, Infinity, "95"]) {
    const retained = render(OpportunityList, { rows: [minor({ match_id: "916", match_review_required: true, provisional_score })] });
    assert.match(retained, /Puntaje por validar/);
    assert.doesNotMatch(retained, /scoreRing/);
  }
  const pending = render(OpportunityList, { rows: [minor({ match_id: null, provisional_score: 95 })] });
  assert.match(pending, /análisis pendiente/);
  assert.doesNotMatch(pending, /Provisional|scoreRing/);
});

function assertToolbarPreserves(html, source, objectKey) {
  assert.match(html, /Con análisis/);
  assert.match(html, /name="analysis"[^>]*value="done"/);
  assert.match(html, /name="profile_id"[^>]*value="dinaut-profile"/);
  const links = [...html.matchAll(/href="([^"]*)"/g)].map(match => new URL(match[1].replaceAll("&amp;", "&"), "https://buenapro.invalid"));
  const toggle = links.find(link => !link.searchParams.has("analysis") && link.searchParams.get("q") === "software" && link.searchParams.get(objectKey) === "service");
  assert.ok(toggle);
  assert.equal(toggle.searchParams.get("source"), source);
  assert.equal(toggle.searchParams.get("profile_id"), "dinaut-profile");
  assert.equal(toggle.searchParams.has("page"), false);
  const typeLinks = links.filter(link => link.searchParams.get("analysis") === "done");
  for (const link of typeLinks) assert.equal(link.searchParams.get("profile_id"), "dinaut-profile");
}

test("PROD2 toolbar retains filter on search/object links and offers a profile-preserving toggle", () => {
  const html = render(Prod4OpportunityToolbar, { source: "prod2", defaults: { source: "prod2", profile_id: "dinaut-profile", q: "software", object: "service", analysis: "done", page: "4" } });
  assertToolbarPreserves(html, "prod2", "object");
  assert.match(html, /aria-current="page" class="analysisFilter active"/);
});

test("minor toolbar retains analysis and profile on search, type and quick views", () => {
  const html = render(OpportunityToolbar, { defaults: { source: "prod6", profile_id: "dinaut-profile", q: "software", objeto: "2", analysis: "done", page: "4" } });
  assert.match(html, /name="analysis"[^>]*value="done"/);
  assert.match(html, /name="profile_id"[^>]*value="dinaut-profile"/);
  assert.match(html, /Con análisis/);
  const links = [...html.matchAll(/href="([^"]*)"/g)].map(match => new URL(match[1].replaceAll("&amp;", "&"), "https://buenapro.invalid"));
  const toggle = links.find(link => !link.searchParams.has("analysis") && link.searchParams.get("q") === "software");
  assert.equal(toggle.searchParams.get("profile_id"), "dinaut-profile");
  assert.equal(toggle.searchParams.get("objeto"), "2");
  assert.equal(toggle.searchParams.get("source"), "prod6");
  for (const link of links.filter(link => link.searchParams.get("analysis") === "done")) {
    assert.equal(link.searchParams.get("profile_id"), "dinaut-profile");
    assert.equal(link.searchParams.get("q"), "software");
  }
});
