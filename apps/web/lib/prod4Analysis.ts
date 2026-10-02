export type Prod4Extraction = {
  id: string;
  codigo_alfresco: string;
  summary_json: Record<string, unknown>;
  raw_extraction_json: Record<string, unknown>;
  model: string;
  prompt_version: string;
  schema_version: string;
  quality: string;
  requires_human_review: boolean;
  facet_count: number;
  created_at: string;
};

export type Prod4RequirementFacet = {
  id: string;
  facet: string;
  label: string;
  required: boolean;
  details_json: Record<string, unknown>;
  evidence_json: unknown[];
};

export type Prod4DocumentMatch = {
  id: string;
  profile_id: string;
  score: number;
  verdict: string;
  breakdown_json: Record<string, unknown>;
  missing_actions_json: unknown[];
  matched_at: string;
  updated_at: string;
};

export type Prod4Analysis = {
  status: "pending" | "skipped" | "failed" | "extracted" | "matched";
  reason: string | null;
  checked_at: string | null;
  extraction: Prod4Extraction | null;
  facets: Prod4RequirementFacet[];
  match: Prod4DocumentMatch | null;
};

/** Preliminary affinity never substitutes for a document-backed analysis. */
export function buildProd4Analysis(input: {
  sourceStatus: string | null;
  reason: string | null;
  checkedAt: string | null;
  extraction: Prod4Extraction | null;
  facets: Prod4RequirementFacet[];
  match: Prod4DocumentMatch | null;
}): Prod4Analysis {
  const { extraction } = input;
  const usableExtraction = extraction && extraction.quality !== "failed";
  const meta = input.match?.breakdown_json.meta;
  const linkedExtractionId = meta && typeof meta === "object" && "extraction_id" in meta
    ? (meta as Record<string, unknown>).extraction_id : null;
  const sameExtraction = extraction && input.match && (linkedExtractionId != null
    ? String(linkedExtractionId) === extraction.id
    : Date.parse(input.match.matched_at) >= Date.parse(extraction.created_at));
  // An older match must not imply that the current document has been evaluated.
  const currentMatch = usableExtraction && sameExtraction ? input.match : null;
  const status = extraction?.quality === "failed" ? "failed"
    : currentMatch ? "matched"
    : usableExtraction ? "extracted"
    : input.sourceStatus === "skipped" ? "skipped" : "pending";
  return {
    status,
    reason: input.reason,
    checked_at: input.checkedAt,
    extraction,
    facets: usableExtraction ? input.facets : [],
    match: currentMatch,
  };
}
