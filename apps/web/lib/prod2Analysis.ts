import { classifyScheduleStage, parseOfficialScheduleDate, type ProcurementScheduleStage } from "@/lib/procurementSchedule";
import type { Prod4Extraction, Prod4RequirementFacet, Prod4DocumentMatch } from "@/lib/prod4Analysis";

export type Prod2Extraction = Prod4Extraction & { sha256_original: string };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/** Use the published date precision; a date-only stage never gets an invented hour. */
export function normalizeProd2Schedule(value: unknown): ProcurementScheduleStage[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((value, index) => {
    const row = record(value);
    const name = typeof row.stage === "string" ? row.stage.trim() : "";
    if (!name) return [];
    const kinds = classifyScheduleStage(name);
    const start = parseOfficialScheduleDate(row.starts_at_text) ?? parseOfficialScheduleDate(row.starts_at);
    const end = parseOfficialScheduleDate(row.ends_at_text) ?? parseOfficialScheduleDate(row.ends_at);
    return [{ id: `prod2-${index}`, name, kind: kinds[0], kinds, startsAt: start?.value ?? null,
      endsAt: end?.value ?? null, startPrecision: start?.precision ?? null, endPrecision: end?.precision ?? null,
      timezone: "America/Lima" as const }];
  });
}

/** Match identity is source + document + extraction + company version, not a numeric ID alone. */
export function buildProd2Analysis(input: {
  idConvocatoria: string;
  profileHash: string | null;
  extraction: Prod2Extraction | null;
  facets: Prod4RequirementFacet[];
  match: Prod4DocumentMatch | null;
}) {
  const extraction = input.extraction;
  const usable = extraction && extraction.quality !== "failed";
  const meta = record(input.match?.breakdown_json.meta);
  const linked = usable && input.match && meta.source_key === "seace_prod2"
    && String(meta.id_convocatoria) === input.idConvocatoria
    && String(meta.extraction_id) === String(extraction.id)
    && meta.document_sha256 === extraction.sha256_original
    && input.profileHash != null && meta.profile_hash === input.profileHash;
  const match = linked ? input.match : null;
  return { status: extraction?.quality === "failed" ? "failed" : match ? "matched" : usable ? "extracted" : "pending",
    extraction, facets: usable ? input.facets : [], match };
}
