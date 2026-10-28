import test from "node:test";
import assert from "node:assert/strict";
import { decisionSummary, evaluationPresentation, isAdministrativeRequirement } from "./evaluationPresentation.ts";

test("administrative paperwork never reduces capacity coverage or produces substantive gaps", () => {
  const facets = [
    { facet: "rnp", label: "RNP vigente", required: true },
    { facet: "proposal_document", label: "Propuesta económica", required: true },
    { facet: "economic_experience", label: "Experiencia S/40.000", required: true },
    { facet: "penalty_condition", label: "Mora", required: true },
  ];
  const result = evaluationPresentation({ requisitos: [
    { requisito: "RNP vigente", categoria: "identidad", estado: "requiere_revision", critico: true, source_index: 1 },
    { requisito: "Propuesta económica", categoria: "documentacion", estado: "requiere_revision", source_index: 2 },
    { requisito: "Experiencia S/40.000", categoria: "experiencia", estado: "cumple", source_index: 3 },
  ] }, [{ facet: "identidad", label: "RNP vigente", estado: "requiere_revision" }], facets);
  assert.equal(result.substantive.length, 1);
  assert.equal(result.coveredCount, 1);
  assert.equal(result.gaps.length, 0);
  assert.deepEqual(result.administrativeChecklist.map(row => row.requisito), ["RNP vigente", "Propuesta económica"]);
  assert.ok(result.administrativeChecklist.every(row => row.estado === "preparar" && row.score_impact === false));
  assert.deepEqual(result.substantiveFacets.map(row => row.facet), ["economic_experience"]);
});

test("specific licenses and technical certificates remain relevant despite legacy documentation category", () => {
  const facets = [
    { facet: "license", label: "Autorización para vender créditos IA", required: true },
    { facet: "company_certification", label: "Certificación del fabricante", required: true },
  ];
  const result = evaluationPresentation({ requisitos: facets.map((facet, index) => ({
    requisito: facet.label, categoria: "documentacion", source_index: index + 1, estado: "requiere_revision",
  })) }, [], facets);
  assert.equal(result.substantive.length, 2);
  assert.equal(result.gaps.length, 2);
  assert.equal(result.administrativeChecklist.length, 0);
  assert.equal(isAdministrativeRequirement({ source_facet: "license", categoria: "identidad", requisito: "RNP especializado" }), false);
  assert.equal(isAdministrativeRequirement({ facet: "documentacion", label: "Certificación del fabricante" }, facets), false);
});

test("capacity gaps preserve economic review while preparation stays collapsed and neutral", () => {
  const result = evaluationPresentation({
    requisitos: [{ requisito: "Experiencia compatible", source_facet: "economic_experience", estado: "requiere_revision", gap: "Validar rubro y moneda", accion: "Seleccionar comprobantes" }],
    administrative_checklist: [{ tipo: "ruc_status", requisito: "RUC activo y habido", estado: "preparar", score_impact: false }],
  }, [], []);
  assert.equal(result.substantive.length, 1);
  assert.equal(result.coveredCount, 0);
  assert.equal(result.gaps[0].gap, "Validar rubro y moneda");
  assert.equal(result.gaps[0].label, "Experiencia compatible");
  assert.equal(result.administrativeChecklist[0].requisito, "RUC activo y habido");
});

test("nonapplicable and desirable rows do not increase required coverage", () => {
  const result = evaluationPresentation({ requisitos: [
    { requisito: "Deseable", source_facet: "training", required: false, estado: "no_cumple" },
    { requisito: "No aplica", source_facet: "equipment", estado: "no_aplica" },
    { requisito: "Equipo", source_facet: "key_personnel", estado: "cumple_con_accion" },
  ] }, [], []);
  assert.equal(result.substantive.length, 1);
  assert.equal(result.coveredCount, 0);
  assert.equal(result.gaps.length, 1);
});

test("missing legacy evaluation never invents coverage from facet count", () => {
  const result = evaluationPresentation(null, [{ facet: "identidad", label: "RUC", estado: "requiere_revision" }], [
    { facet: "ruc_status", label: "RUC", required: true },
    { facet: "key_personnel", label: "Ingeniero", required: true },
  ]);
  assert.equal(result.substantive.length, 0);
  assert.equal(result.coveredCount, 0);
  assert.equal(result.gaps.length, 0);
  assert.equal(result.administrativeChecklist.length, 1);
});

test("labels alone cannot hide substantive experience as proposal paperwork", () => {
  assert.equal(isAdministrativeRequirement({ source_facet: "specific_experience", requisito: "Declaración de experiencia" }), false);
  assert.equal(isAdministrativeRequirement({ requisito: "Formato especializado del fabricante" }), false);
  const result = evaluationPresentation({ preparacion_documental: [{ requisito: "Carta CCI" }], requisitos: [] }, [], []);
  assert.equal(result.administrativeChecklist.length, 1);
});

test("V4 authoritative technical proposal stays substantive with its own separate checklist", () => {
  const facets = [
    { facet: "legal_capacity", label: "No contar con impedimento", required: true },
    { facet: "penalty_condition", label: "Mora", required: true },
    { facet: "proposal_document", label: "Propuesta técnica con API, términos y SLA", required: true },
    { facet: "proposal_document", label: "Formato de propuesta económica", required: true },
  ];
  const result = evaluationPresentation({
    meta: { prompt_version: "match_analysis_v4_substantive", score_basis: "substantive_requirements_only" },
    requisitos: [{ requisito: facets[2].label, source_index: 3, source_facet: "proposal_document", categoria: "documentacion", estado: "requiere_revision", gap: "Confirmar SLA y alcance API" }],
    administrative_checklist: [{ tipo: "proposal_document", requisito: facets[3].label, estado: "preparar", score_impact: false }],
  }, [], facets);
  assert.equal(result.substantive.length, 1);
  assert.equal(result.gaps.length, 1);
  assert.equal(result.gaps[0].gap, "Confirmar SLA y alcance API");
  assert.deepEqual(result.substantiveFacets, [facets[2]]);
  assert.deepEqual(result.administrativeChecklist.map(row => row.requisito), [facets[3].label]);
});

test("legacy technical proposal and manufacturer certification stay relevant alongside ordinary forms", () => {
  const facets = [
    { facet: "proposal_document", label: "Propuesta técnica", required: true, details_json: { text: "API y SLA de servicio" } },
    { facet: "proposal_document", label: "Certificado de fabricante", required: true },
    { facet: "proposal_document", label: "Formato de propuesta económica", required: true },
  ];
  const result = evaluationPresentation({ requisitos: facets.map((facet, index) => ({ requisito: facet.label, source_index: index + 1, categoria: "documentacion", estado: "requiere_revision" })) }, [], facets);
  assert.equal(result.substantive.length, 2);
  assert.equal(result.gaps.length, 2);
  assert.deepEqual(result.administrativeChecklist.map(row => row.requisito), [facets[2].label]);
});

test("new policy never reconstructs administrative or capacity items from unrelated source inventory", () => {
  const result = evaluationPresentation({ requisitos: [], administrative_checklist: [] }, [
    { facet: "economic_experience", label: "Resultado anterior", estado: "requiere_revision" },
  ], [{ facet: "proposal_document", label: "Formato obligatorio", required: true }]);
  assert.equal(result.substantive.length, 0);
  assert.equal(result.gaps.length, 0);
  assert.equal(result.substantiveFacets.length, 0);
  assert.equal(result.administrativeChecklist.length, 0);
});

test("an evaluation with a withheld economic score still exposes substantive coverage", () => {
  const result = evaluationPresentation({
    meta: { score_validated: false, raw_score: 95, score_basis: "substantive_requirements_only" },
    requisitos: [
      { requisito: "Personal técnico", source_facet: "key_personnel", estado: "cumple" },
      { requisito: "Experiencia económica compatible", source_facet: "economic_experience", estado: "requiere_revision", gap: "Separar PEN y USD y bienes de contrato mixto" },
    ],
    administrative_checklist: [{ tipo: "rnp", requisito: "RNP vigente", estado: "preparar", score_impact: false }],
  }, [], []);
  assert.equal(result.hasEvaluation, true);
  assert.equal(result.scoreUnderReview, true);
  assert.equal(result.substantive.length, 2);
  assert.equal(result.coveredCount, 1);
  assert.equal(result.gaps.length, 1);
  assert.equal(result.administrativeChecklist.length, 1);
  assert.equal("raw_score" in result, false);
});

test("pending score is explicit and does not convert a missing analysis into an evaluation", () => {
  assert.equal(evaluationPresentation({ meta: { score_validated: true }, requisitos: [{ requisito: "Experiencia", source_facet: "specific_experience", estado: "cumple" }] }, [], []).scoreUnderReview, false);
  const pending = evaluationPresentation(null, [], []);
  assert.equal(pending.hasEvaluation, false);
  assert.equal(pending.scoreUnderReview, false);
});


const GENERIC = "La evaluación requiere contrastar experiencia y respaldos antes de publicar el puntaje. Revisa las condiciones pendientes indicadas abajo.";

test("a score under review shows the model's own reading, marked as provisional", () => {
  const reading = "La empresa no acredita el monto facturado mínimo exigido de S/ 140,000.00.";
  assert.deepEqual(decisionSummary({ resumen: GENERIC, meta: { score_validated: false, raw_model_summary: ` ${reading} ` } }),
    { text: reading, provisional: true });
  assert.deepEqual(evaluationPresentation({ resumen: GENERIC, meta: { score_validated: false, raw_model_summary: reading } }, [], []).summary,
    { text: reading, provisional: true });
});

test("a validated score, or one without the model's reading, keeps its summary", () => {
  assert.deepEqual(decisionSummary({ resumen: "Cumple lo esencial.", meta: { score_validated: true, raw_model_summary: "Otro texto." } }),
    { text: "Cumple lo esencial.", provisional: false });
  assert.deepEqual(decisionSummary({ resumen: GENERIC, meta: { score_validated: false } }), { text: GENERIC, provisional: false });
  assert.deepEqual(decisionSummary({ resumen: GENERIC, meta: { score_validated: false, raw_model_summary: "   " } }),
    { text: GENERIC, provisional: false });
  assert.deepEqual(decisionSummary({}), { text: "", provisional: false });
});

test("a long model reading is cut at a word boundary", () => {
  const long = Array.from({ length: 120 }, (_, index) => `palabra${index}`).join(" ");
  const { text } = decisionSummary({ meta: { score_validated: false, raw_model_summary: long } });
  const kept = text.slice(0, -1);
  assert.ok(text.endsWith("…") && kept.length <= 420);
  assert.ok(long.startsWith(kept) && long[kept.length] === " ");  // Never half a word.
});
