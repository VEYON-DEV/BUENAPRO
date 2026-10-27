type JsonRecord = Record<string, unknown>;

export const MATCH_POLICY_VERSION = "match_analysis_v5_concrete_evidence";
export const COMPACT_PROFILE_VERSION = "compact_match_context_v1";
export const SEMANTIC_GUARD_VERSION = "economic_comparison_guard_v1";

export const SOURCE_CATEGORIES: Record<string, string> = {
  economic_experience: "experiencia_economica", general_experience: "experiencia",
  specific_experience: "experiencia", key_personnel: "personal_clave", education: "formacion",
  training: "formacion", equipment: "equipamiento", insurance: "seguro", license: "licencia",
  company_certification: "certificacion", professional_registration: "certificacion",
  proposal_document: "documentacion", legal_capacity: "identidad", other: "otro",
};

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function normalized(value: unknown): string {
  return String(value ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[._]/g, " ").replace(/\s+/g, " ").trim();
}

/** Submission paperwork is retained as a checklist, without scoring capacity. */
export function isAdministrativeRequirement(value: unknown): boolean {
  const row = record(value);
  const type = String(row.tipo ?? row.facet ?? row.categoria ?? "");
  const label = normalized(row.requisito ?? row.label);
  if (/^persona natural(?: y\/o| o| y)? juridica[ .]*$|^carta de presentacion\b|\bpacto de integridad\b|\bno incurrir en nepotismo\b/.test(label)) return true;
  if (["rnp", "ruc_status", "not_disqualified", "cci"].includes(type)) return true;
  if (type === "legal_capacity" && /^(?:empresa|persona juridica) dedicada al rubro[ .]*$/.test(label)) return true;
  if (/\b(?:rnp|ruc|cci)\b|registro (?:nacional de proveedores|unico de contribuyente)|impedimento para contratar|no (?:tener|contar con) impedimento|declaracion jurada|declaraciones juradas/.test(label)) return true;
  if (/^(?:formato|anexo|cotizacion|propuesta economica|oferta economica)\b/.test(label)) return true;
  if (/copia simple.*(?:constancia|documento de identidad)|documentos? (?:que )?acredit(?:en|e).*|documentacion que acredite/.test(label)) return true;
  // Legal capacity can describe a real business authorization or the ability to
  // supply the requested service. Do not drop it merely because of its category.
  return false;
}

export function splitMatchRequirements(facets: JsonRecord[]) {
  const substantive: JsonRecord[] = [];
  const administrativeChecklist: JsonRecord[] = [];
  for (const [index, row] of facets.entries()) {
    if (row.facet === "penalty_condition") continue;
    if (isAdministrativeRequirement(row)) {
      administrativeChecklist.push({
        tipo: row.facet,
        requisito: row.label,
        estado: "preparar",
        score_impact: false,
      });
    } else {
      substantive.push({
        source_index: index + 1,
        tipo: row.facet,
        requisito: row.label,
        obligatorio: row.required,
        detalle: row.details_json,
      });
    }
  }
  return { substantive, administrativeChecklist };
}

/**
 * Proposal documents the model did not judge become paperwork to prepare.
 * They usually say how another requirement is proven (contract copies, SCTR policy, degrees)
 * and the model leaves them out. Those, and only those, move to `checklist`; the requirements
 * still expected are returned. A proposal document the model did judge (an SLA, technical
 * documentation) stays a requirement, and any other omission still fails the binding.
 */
export function checklistOmittedProposalDocuments(results: JsonRecord[], expected: JsonRecord[], checklist: JsonRecord[]): JsonRecord[] {
  const judged = new Set(results.map(result => result.source_index));
  const kept: JsonRecord[] = [];
  for (const requirement of expected) {
    if (requirement.tipo === "proposal_document" && !judged.has(requirement.source_index)) {
      checklist.push({ tipo: requirement.tipo, requisito: requirement.requisito, estado: "preparar", score_impact: false });
    } else {
      kept.push(requirement);
    }
  }
  return kept;
}

/** Mirror the worker: a hypothetical/demo capability never becomes evidence. */
export function evidenceOnlyProfile(value: unknown): JsonRecord {
  function hypothetical(item: unknown): boolean {
    if (typeof item === "string") return /demo|por acreditar/i.test(item);
    const row = record(item);
    if (row.verified === false || row.is_demo === true) return true;
    if (["demo", "unverified", "hypothetical", "template", "public_record_not_accredited"].includes(normalized(row.verification_status ?? row.status))) return true;
    return ["nombre", "name", "role", "descripcion"].some(key => typeof row[key] === "string" && hypothetical(row[key]));
  }
  function clean(item: unknown): unknown {
    if (Array.isArray(item)) return item.filter(child => !hypothetical(child)).map(clean);
    if (item && typeof item === "object") return Object.fromEntries(Object.entries(record(item)).filter(([key]) => !key.startsWith("demo_")).map(([key, child]) => [key, clean(child)]));
    return item;
  }
  return record(clean(value));
}

function pick(row: JsonRecord, keys: string[]): JsonRecord {
  return Object.fromEntries(keys.filter(key => Object.hasOwn(row, key)).map(key => [key, row[key]]));
}

const EXPERIENCE_KEYS = ["numero_contrato", "objeto", "tipo_objeto", "rubro", "especialidad", "actividades", "entidad", "fecha_inicio", "fecha_fin", "fecha_conformidad", "anio", "monto", "moneda", "conformidad", "modalidad_participacion", "porcentaje_participacion", "alcance_participacion"];
const AMOUNT_KEYS = ["amount_decimal", "literal", "currency", "label", "tax_basis", "source_type", "quote", "page"];
const FACT_KEYS = ["field", "value", "quote", "page"];
const AREA_KEYS = ["monto", "moneda", "rubro", "especialidad", "tipo_objeto", "descripcion", "documents", "status", "no_global_sum", "source_record_index"];

/** Remove duplicate audit/raw material, preserving every documentary decision source. */
export function buildCompactMatchProfile(value: unknown): JsonRecord {
  const profile = evidenceOnlyProfile(value);
  const backedIds = new Set<string>();
  const experiences = list(profile.experience_json).map(value => {
    const experience = record(value), summary = record(experience.documentary_summary);
    const sources = list(summary.sources).map(value => {
      const source = record(value), extraction = record(source.extraction);
      if (typeof source.document_id === "string") backedIds.add(source.document_id);
      const amounts = list(extraction.amounts).map(value => pick(record(value), AMOUNT_KEYS));
      const uniqueAmounts = amounts.filter((amount, index) => amounts.findIndex(candidate => JSON.stringify(candidate) === JSON.stringify(amount)) === index);
      // All monetary evidence survives, including mixed currency, tax and invoice
      // amounts. Removing an exact duplicate requires remapping the principal.
      const principal = extraction.principal_amount_index;
      const principalAmount = typeof principal === "number" && Number.isInteger(principal) ? amounts[principal] : null;
      const principalIndex = principalAmount ? uniqueAmounts.findIndex(amount => JSON.stringify(amount) === JSON.stringify(principalAmount)) : null;
      const projectedExtraction = {
        ...pick(extraction, ["summary", "conformity", "warnings", "unknowns", "document_types"]),
        classification: pick(record(extraction.classification), ["rubro", "specialty", "inferred", "quote", "page"]),
        amounts: uniqueAmounts,
        principal_amount_index: principalIndex,
        facts: list(extraction.facts).map(fact => pick(record(fact), FACT_KEYS)),
      };
      return { ...pick(source, ["document_id", "sha256", "status"]), extraction: projectedExtraction };
    });
    return {
      ...pick(experience, sources.length ? EXPERIENCE_KEYS.filter(key => !["actividades", "alcance_participacion"].includes(key)) : EXPERIENCE_KEYS),
      ...(sources.length ? { documentary_summary: {
        ...pick(summary, ["status", "no_legal_accreditation", "amount_review_required", "missing_document_ids"]), sources,
      } } : {}),
    };
  });
  const economic = record(profile.econ_experience_json);
  const areas = list(economic.areas).filter(value => {
    const area = record(value), ids = list(area.documents).map(String);
    const index = area.source_record_index;
    const indexedExperience = typeof index === "number" && Number.isInteger(index) ? record(experiences[index]) : {};
    const hasIndexedBacking = list(record(indexedExperience.documentary_summary).sources).length > 0;
    return !hasIndexedBacking && !(ids.length && ids.every(id => backedIds.has(id)));
  }).map(area => pick(record(area), AREA_KEYS));
  return {
    ...pick(profile, ["razon_social", "ruc", "team_json", "equipment_json", "certifications_json", "hireable_roles_json"]),
    identity_json: pick(record(profile.identity_json), ["rnp", "cci", "website", "commercial_name", "ruc_source", "services_declared_by_user", "capabilities_source"]),
    experience_json: experiences,
    econ_experience_json: { areas, no_global_sum: true, currency_conversion: false },
    context_projection: { version: COMPACT_PROFILE_VERSION, declared_fields_omitted: true, omission_meaning: "Document-backed records use extracted summaries and citations; omitted declarations are not evidence of incapacity." },
  };
}

export function guardParticipationActions(requirements: JsonRecord[], actions: unknown[], summary: unknown): string[] {
  const participation = record(record(summary).participation);
  function guard(value: unknown): string | null {
    if (typeof value !== "string" || !value.trim()) return null;
    for (const [type, pattern] of [["consorcio", "consor"], ["subcontratacion", "subcontr"]]) {
      if (!normalized(value).includes(pattern)) continue;
      const status = record(participation[type]).status ?? "not_identified";
      if (status === "prohibited") return "Buscar alternativa permitida por las bases";
      if (status !== "permitted") return "Verificar autorización y condiciones en las bases";
    }
    return value;
  }
  for (const requirement of requirements) {
    const action = guard(requirement.accion);
    if (requirement.accion !== action) {
      requirement.accion = action;
      if (requirement.estado === "cumple_con_accion") requirement.estado = "requiere_revision";
    }
  }
  return actions.map(guard).filter((action): action is string => action != null);
}

function decimalUnits(value: unknown): bigint | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const text = String(value).trim();
  if (!/^\d+(?:\.\d{1,6})?$/.test(text)) return null;
  const [whole, fraction = ""] = text.split(".");
  return BigInt(whole) * BigInt(1_000_000) + BigInt(fraction.padEnd(6, "0"));
}

function evidenceDate(value: unknown): Date | null {
  const text = String(value ?? "");
  const iso = text.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
  const latin = text.match(/\b(\d{1,2})\/(\d{1,2})\/(\d{4})\b/);
  const parts = iso ? [iso[1], iso[2], iso[3]] : latin ? [latin[3], latin[2].padStart(2, "0"), latin[1].padStart(2, "0")] : null;
  if (!parts) return null;
  const normalizedDate = parts.join("-");
  const date = new Date(`${normalizedDate}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().startsWith(normalizedDate) ? date : null;
}

function requirementAmount(value: JsonRecord): unknown {
  const detail = record(value.detalle ?? value.details_json);
  return detail.monto ?? detail.amount ?? detail.required_amount ?? null;
}

/** Validate the documentary arithmetic before a model's economic approval can be published. */
export function guardEconomicComparisons(requirements: JsonRecord[], profileValue: unknown, substantive: JsonRecord[], at = new Date()) {
  const profile = record(profileValue);
  const sources = new Map<string, { source: JsonRecord; experience: JsonRecord }>();
  for (const value of list(profile.experience_json)) {
    const experience = record(value);
    for (const item of list(record(experience.documentary_summary).sources)) {
      const source = record(item);
      if (typeof source.document_id === "string") sources.set(source.document_id, { source, experience });
    }
  }
  const invalid: JsonRecord[] = [];
  for (const requirement of requirements) {
    const original = substantive.find(row => row.source_index === requirement.source_index);
    if (!original || requirement.estado !== "cumple") continue;
    if (!["economic_experience", "specific_experience", "general_experience", "experience"].includes(String(original.tipo))) continue;
    const required = decimalUnits(requirementAmount(original));
    if (required == null || required <= BigInt(0)) continue;
    const comparisons = list(requirement.economic_comparison).map(record);
    const requiredCurrency = String(record(original.detalle).moneda ?? record(original.detalle).currency ?? "PEN").trim().toUpperCase();
    const reasons: string[] = [];
    if (!comparisons.length) reasons.push("Falta comparación económica estructurada contra partidas documentadas.");
    const seen = new Set<string>();
    const contracts = new Set<string>();
    let eligible = BigInt(0);
    const requirementText = normalized(`${original.requisito ?? ""} ${record(original.detalle).descripcion ?? ""}`);
    const yearMatch = requirementText.match(/\b(\d{1,2})\)?\s*anos\b/);
    const periodWords = requirementText.match(/\b(uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|quince|veinte)\s+anos\b/);
    const yearWords: Record<string, number> = { uno: 1, dos: 2, tres: 3, cuatro: 4, cinco: 5, seis: 6, siete: 7, ocho: 8, nueve: 9, diez: 10, quince: 15, veinte: 20 };
    const years = yearMatch ? Number(yearMatch[1]) : periodWords ? yearWords[periodWords[1]] : null;
    const periodStart = years == null ? null : new Date(at);
    if (periodStart && years != null) periodStart.setUTCFullYear(periodStart.getUTCFullYear() - years);
    for (const comparison of comparisons) {
      const id = String(comparison.document_id ?? ""), binding = sources.get(id);
      if (!binding) { reasons.push("La comparación usa una fuente documental inexistente."); continue; }
      if (!list(requirement.evidence_refs).includes(id)) reasons.push("El documento económico no se atribuyó en evidence_refs.");
      if (seen.has(id)) { reasons.push("Una misma fuente se repite en el monto computado."); continue; }
      seen.add(id);
      const contract = normalized(binding.experience.numero_contrato);
      if (contract && contracts.has(contract)) { reasons.push("Se repite el mismo contrato en documentos diferentes."); continue; }
      if (contract) contracts.add(contract);
      const extraction = record(binding.source.extraction);
      const index = comparison.amount_index;
      const amount = typeof index === "number" && Number.isInteger(index) && index >= 0 ? record(list(extraction.amounts)[index]) : {};
      const amountUnits = decimalUnits(amount.amount_decimal), eligibleUnits = decimalUnits(comparison.eligible_amount_decimal);
      if (amountUnits == null || eligibleUnits == null || eligibleUnits <= BigInt(0) || eligibleUnits !== amountUnits) reasons.push("El monto computable no coincide con una partida monetaria documentada.");
      if (comparison.currency !== requiredCurrency || amount.currency !== requiredCurrency) reasons.push("La moneda de la partida no coincide con el mínimo; no se permite conversión implícita.");
      if (comparison.full_amount_comparable !== true) reasons.push("No se acreditó que toda la partida seleccionada corresponda al objeto compatible.");
      const quote = normalized(comparison.compatible_scope_quote);
      const sourceText = [extraction.summary, record(extraction.classification).quote, ...list(extraction.facts).flatMap(value => [record(value).value, record(value).quote])].map(normalized);
      if (quote.length < 12 || !sourceText.some(text => text.includes(quote))) reasons.push("La compatibilidad no cita un alcance literal de la fuente seleccionada.");
      const dates = list(extraction.facts).filter(value => /fecha|date|vigencia|period|aceptacion|recepcion|conformidad|culminacion/.test(normalized(record(value).field))).flatMap(value => [record(value).value, record(value).quote])
        .map(evidenceDate).filter((date): date is Date => date != null);
      const withinPeriod = dates.some(date => date <= at && (!periodStart || date >= periodStart));
      if (comparison.period_supported !== true || !withinPeriod) reasons.push("La vigencia o el período computable no tiene una fecha documental compatible.");
      if (eligibleUnits != null) eligible += eligibleUnits;
    }
    if (comparisons.length && eligible < required) reasons.push("Las partidas documentadas no cubren el mínimo económico exigido.");
    if (reasons.length) {
      const unique = Array.from(new Set(reasons));
      requirement.estado = "requiere_revision";
      requirement.gap = "El monto compatible todavía no está validado por partida, moneda y período.";
      requirement.accion = "Verificar las partidas compatibles y sus respaldos antes de computarlas.";
      requirement.economic_validation_reasons = unique;
      invalid.push({ source_index: requirement.source_index, reasons: unique });
    }
  }
  return { scoreValidated: invalid.length === 0, invalidEconomicComparisons: invalid };
}
