import taxonomy from "../../../workers/seace/src/buenapro_worker/extraction/commercial_taxonomy.v1.json";
import categories from "../../../workers/seace/src/buenapro_worker/extraction/commercial_categories.v1.json";

/** Shared with LLM1. Commercial tags are independent of legal modality and score. */
export const commercialTaxonomy = taxonomy;
export const commercialCategories = categories;
export const ADVANCED_COMMERCIAL_FILTER_KEYS = ["commercial_area", "commercial_work", "commercial_specialty", "commercial_status"] as const;
export const COMMERCIAL_FILTER_KEYS = ["commercial_category", ...ADVANCED_COMMERCIAL_FILTER_KEYS] as const;
export type CommercialStatus = "evidence_backed" | "preliminary" | "needs_review" | "unclassified";
export type CommercialFilters = { categories?: string[]; areas: string[]; workTypes: string[]; specialties: string[]; statuses: CommercialStatus[] };
export type CommercialEvidence = { axis: "area" | "work_type" | "specialty"; value: string; citation: string; page: number };
export type CommercialClassification = {
  taxonomy_version: string;
  primary_area: string;
  secondary_areas: string[];
  work_types: string[];
  specialties: string[];
  support_status: CommercialStatus;
  evidence: CommercialEvidence[];
};
export type CommercialClassificationSource = "document" | "title" | null;
export type CommercialClassificationState = "classified" | "other" | "unclear" | "unclassified";
export type CommercialCategoryPresentation = {
  commercial_categories: string[];
  commercial_classification_source: CommercialClassificationSource;
  commercial_classification_state: CommercialClassificationState;
};
export const PROD2_TITLE_CATEGORY_POLICY = "title_commercial_classification_v1";
export const PROD2_TITLE_CATEGORY_MODEL = "jev-1.13.0";

export class CommercialFilterError extends Error {
  readonly status = 400;
}

const areaIds = new Set(commercialTaxonomy.areas.map(item => item.id));
const workIds = new Set(commercialTaxonomy.work_types.map(item => item.id));
const specialtyIds = new Set(commercialTaxonomy.specialties.map(item => item.id));
const statusIds = new Set(commercialTaxonomy.support_statuses.map(item => item.id));
const categoryIds = new Set(commercialCategories.categories.map(item => item.id));

function csv(params: URLSearchParams, key: string, allowed: ReadonlySet<string>): string[] {
  const supplied = params.getAll(key);
  if (!supplied.length || supplied.every(value => value === "")) return [];
  const values = supplied.flatMap(value => value.split(",").map(item => item.trim()));
  if (values.some(value => !allowed.has(value))) throw new CommercialFilterError(`Invalid ${key}`);
  return [...new Set(values)];
}

/** OR within an axis; services combine populated axes with AND before pagination. */
export function parseCommercialFilters(params: URLSearchParams): CommercialFilters {
  return {
    categories: csv(params, "commercial_category", categoryIds),
    areas: csv(params, "commercial_area", areaIds),
    workTypes: csv(params, "commercial_work", workIds),
    specialties: csv(params, "commercial_specialty", specialtyIds),
    statuses: csv(params, "commercial_status", new Set([...statusIds, "unclassified"])) as CommercialStatus[],
  };
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function validAxis(value: unknown, allowed: ReadonlySet<string>): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string" && allowed.has(item)) && new Set(value).size === value.length;
}

export function unclassifiedCommercialClassification(): CommercialClassification {
  return { taxonomy_version: commercialTaxonomy.version, primary_area: "sin_clasificar", secondary_areas: [], work_types: [], specialties: [],
    support_status: "unclassified", evidence: [] };
}

/** Historical or unknown tags never get inferred from title, keyword, or score. */
export function normalizeCommercialClassification(value: unknown, retrievalEvidenceAudit?: unknown): CommercialClassification {
  const data = object(value);
  if (!data || data.taxonomy_version !== commercialTaxonomy.version || typeof data.primary_area !== "string"
    || !areaIds.has(data.primary_area) || data.primary_area === "sin_clasificar"
    || typeof data.support_status !== "string" || !statusIds.has(data.support_status)
    || !validAxis(data.secondary_areas, areaIds) || data.secondary_areas.includes("sin_clasificar") || data.secondary_areas.includes(data.primary_area)
    || !validAxis(data.work_types, workIds) || !validAxis(data.specialties, specialtyIds) || !Array.isArray(data.evidence)) {
    return unclassifiedCommercialClassification();
  }
  const secondaryAreas = [...data.secondary_areas];
  const workTypes = [...data.work_types], specialties = [...data.specialties];
  const selected: Record<CommercialEvidence["axis"], Set<string>> = {
    area: new Set([data.primary_area, ...secondaryAreas]), work_type: new Set(workTypes), specialty: new Set(specialties),
  };
  const evidence: CommercialEvidence[] = [];
  for (const item of data.evidence) {
    const row = object(item);
    if (!row || (row.axis !== "area" && row.axis !== "work_type" && row.axis !== "specialty")
      || typeof row.value !== "string" || !selected[row.axis].has(row.value)
      || typeof row.citation !== "string" || !row.citation.trim()
      || typeof row.page !== "number" || !Number.isSafeInteger(row.page) || row.page < 1) return unclassifiedCommercialClassification();
    evidence.push({ axis: row.axis, value: row.value, citation: row.citation.trim(), page: row.page });
  }
  for (const axis of ["area", "work_type", "specialty"] as const) {
    for (const value of selected[axis]) {
      if (!evidence.some(item => item.axis === axis && item.value === value)) return unclassifiedCommercialClassification();
    }
  }
  const errors = object(retrievalEvidenceAudit)?.errors;
  const unverifiedCitation = Array.isArray(errors) && errors.some(item => object(item)?.kind === "unverified_commercial_citation");
  return { taxonomy_version: commercialTaxonomy.version, primary_area: data.primary_area, secondary_areas: secondaryAreas,
    work_types: workTypes, specialties, support_status: unverifiedCitation ? "needs_review" : data.support_status as CommercialStatus, evidence };
}

/** An invalid raw block is authoritative and cannot be replaced by an older summary. */
export function commercialClassificationFromExtraction(rawExtraction: unknown, summary: unknown): CommercialClassification {
  const raw = object(rawExtraction), compact = object(summary);
  return normalizeCommercialClassification(raw?.commercial_classification ?? compact?.commercial_classification, raw?.retrieval_evidence_audit);
}

/** Categories are a deterministic view of valid LLM1 tags, never another paid read. */
export function deriveCommercialCategories(classification: unknown): string[] {
  const current = normalizeCommercialClassification(classification);
  if (current.support_status === "unclassified" || current.taxonomy_version !== commercialCategories.classification_taxonomy_version) return [];
  const areas = new Set([current.primary_area, ...current.secondary_areas]);
  return commercialCategories.categories.filter(category => category.match_any.areas.some(id => areas.has(id))
    || category.match_any.work_types.some(id => current.work_types.includes(id))
    || category.match_any.specialties.some(id => current.specialties.includes(id))).map(category => category.id);
}

/** Title labels are preliminary navigation metadata, never documentary evidence. */
export function prod2CommercialCategoryPresentation(classification: unknown, retained: {
  commercial_categories?: unknown; commercial_classification_source?: unknown; commercial_classification_state?: unknown;
}): CommercialCategoryPresentation {
  const document = normalizeCommercialClassification(classification);
  if (document.support_status !== "unclassified") {
    const categories = deriveCommercialCategories(document);
    return { commercial_categories: categories, commercial_classification_source: "document",
      commercial_classification_state: categories.length ? "classified" : "other" };
  }
  const categories = retained.commercial_categories;
  const state = retained.commercial_classification_state;
  if (retained.commercial_classification_source === "title" && validAxis(categories, categoryIds)
    && ((state === "classified" && categories.length > 0) || ((state === "other" || state === "unclear") && categories.length === 0))) {
    return { commercial_categories: commercialCategories.categories.filter(category => categories.includes(category.id)).map(category => category.id),
      commercial_classification_source: "title", commercial_classification_state: state };
  }
  return { commercial_categories: [], commercial_classification_source: null, commercial_classification_state: "unclassified" };
}
