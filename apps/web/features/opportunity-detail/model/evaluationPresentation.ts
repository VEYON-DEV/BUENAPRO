type RecordValue = Record<string, any>;

const ADMINISTRATIVE_FACETS = new Set([
  "legal_capacity", "ruc_status", "rnp", "proposal_document",
]);
const SUBSTANTIVE_FACETS = new Set([
  "economic_experience", "general_experience", "specific_experience", "key_personnel",
  "education", "professional_registration", "training", "license", "equipment",
  "insurance", "company_certification",
]);
const ADMINISTRATIVE_CATEGORIES = new Set(["identidad", "documentacion"]);

function records(value: unknown): RecordValue[] {
  return Array.isArray(value)
    ? value.filter(row => row && typeof row === "object" && !Array.isArray(row))
    : [];
}

function label(row: RecordValue): string {
  return String(row.requisito ?? row.label ?? "").trim();
}

function sourceFacet(row: RecordValue, facets: RecordValue[]): RecordValue | undefined {
  const index = Number(row.source_index);
  if (Number.isInteger(index) && index > 0 && index <= facets.length) {
    const indexed = facets[index - 1];
    if (!label(row) || label(indexed) === label(row)) return indexed;
  }
  return facets.find(facet => label(facet) === label(row));
}

function facetType(row: RecordValue, facets: RecordValue[]): string {
  if (row.source_facet) return String(row.source_facet);
  const type = String(row.facet ?? "");
  if (type && !ADMINISTRATIVE_CATEGORIES.has(type)) return type;
  return String(sourceFacet(row, facets)?.facet ?? type);
}

function hasTechnicalScope(row: RecordValue, facets: RecordValue[]): boolean {
  const source = sourceFacet(row, facets);
  const value = [label(row), source?.label, JSON.stringify(source?.details_json ?? row.details_json ?? row.details ?? {})]
    .filter(Boolean).join(" ").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("es-PE");
  return /\b(?:api|sla|propuesta tecnica|especificaciones tecnicas|terminos de servicio|autorizacion del fabricante|certificaci[oó]n|certificados?|licencias?)\b/.test(value);
}

export function isAdministrativeRequirement(row: RecordValue, facets: RecordValue[] = []): boolean {
  const type = facetType(row, facets);
  // A specific license, qualification or technical certificate remains substantive even if
  // a legacy model classified its evidence as generic documentation.
  if (SUBSTANTIVE_FACETS.has(type)) return false;
  if ((type === "proposal_document" || type === "documentacion" || !type)
    && hasTechnicalScope(row, facets)) return false;
  if (ADMINISTRATIVE_FACETS.has(type)) return true;
  if (ADMINISTRATIVE_CATEGORIES.has(type)) return true;
  if (type && type !== "other") return false;
  return ADMINISTRATIVE_CATEGORIES.has(String(row.categoria ?? ""));
}

function uniqueChecklist(items: RecordValue[]): RecordValue[] {
  const unique = new Map<string, RecordValue>();
  for (const row of items) {
    const title = label(row);
    if (!title) continue;
    const key = title.toLocaleLowerCase("es-PE");
    if (!unique.has(key)) unique.set(key, { ...row, requisito: title, estado: "preparar", score_impact: false });
  }
  return [...unique.values()];
}

function clip(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, limit).replace(/\s+\S*$/, "")}…`;
}

/**
 * When a rule could not back the model's judgements the worker stores a generic notice as the
 * summary and keeps the model's own summary in meta. A score still under review shows that
 * reading; validated or older evaluations keep their summary. The summary is provisional whenever
 * the score is: unvalidated, or resting on a source reading that still needs human review.
 */
export function decisionSummary(analysis: RecordValue, reviewRequired = false): { text: string; provisional: boolean } {
  const summary = typeof analysis.resumen === "string" ? analysis.resumen.trim() : "";
  const reading = typeof analysis.meta?.raw_model_summary === "string" ? analysis.meta.raw_model_summary.trim() : "";
  const unvalidated = analysis.meta?.score_validated === false;
  const text = unvalidated && reading ? clip(reading, 420) : summary;
  return { text, provisional: Boolean(text) && (unvalidated || reviewRequired) };
}

export function evaluationPresentation(
  breakdown: unknown,
  missingActions: unknown,
  sourceFacets: unknown,
  reviewRequired = false,
) {
  const analysis = breakdown && typeof breakdown === "object" && !Array.isArray(breakdown)
    ? breakdown as RecordValue
    : {};
  // V4 source indexes belong to the complete original inventory, including penalties.
  const facets = records(sourceFacets);
  const evaluated = records(analysis.requisitos);
  const explicitChecklist = Array.isArray(analysis.administrative_checklist) || Array.isArray(analysis.preparacion_documental);
  const preparation = records(analysis.administrative_checklist ?? analysis.preparacion_documental);
  const administrative = explicitChecklist ? [] : evaluated.filter(row => isAdministrativeRequirement(row, facets));
  // The policy has already classified V4 evaluations. In particular, a technical proposal
  // can retain proposal_document as its source facet without becoming ordinary paperwork.
  const substantive = evaluated.filter(row => (explicitChecklist || !isAdministrativeRequirement(row, facets))
    && facetType(row, facets) !== "penalty_condition"
    && row.score_impact !== false
    && row.estado !== "no_aplica"
    && (row.required ?? sourceFacet(row, facets)?.required) !== false);
  const gaps: RecordValue[] = evaluated.length || explicitChecklist
    ? substantive.filter(row => row.estado !== "cumple").map(row => ({ ...row, label: label(row), facet: facetType(row, facets) || row.categoria }))
    : records(missingActions).filter(row => !isAdministrativeRequirement(row, facets) && facetType(row, facets) !== "penalty_condition");
  const checklist = uniqueChecklist(explicitChecklist ? preparation : [
    ...administrative,
    ...records(missingActions).filter(row => isAdministrativeRequirement(row, facets)),
    ...facets.filter(row => isAdministrativeRequirement(row)),
  ]);
  const substantiveFacets = explicitChecklist
    ? substantive.map(row => sourceFacet(row, facets) ?? {
      facet: facetType(row, facets) || "other", label: label(row), required: row.required,
    })
    : facets.filter(row => row.facet !== "penalty_condition" && !isAdministrativeRequirement(row));

  return {
    hasEvaluation: evaluated.length > 0,
    scoreUnderReview: analysis.meta?.score_validated === false || reviewRequired,
    summary: decisionSummary(analysis, reviewRequired),
    substantive,
    substantiveFacets,
    coveredCount: substantive.filter(row => row.estado === "cumple").length,
    gaps,
    administrativeChecklist: checklist,
  };
}
