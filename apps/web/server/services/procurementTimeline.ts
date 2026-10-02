import { query } from "@/server/db/client";
import { normalizeProd4Schedule, normalizeProd6Schedule, parseOfficialScheduleDate } from "@/lib/procurementSchedule";
import type { TimelineOpportunity, TimelineResponse, TimelineSource } from "@/lib/procurementTimeline";
export type ProcurementTimelineResult = TimelineResponse;
export type { TimelineOpportunity, TimelineResponse, TimelineSource } from "@/lib/procurementTimeline";

export function parseTimelineParams(params: URLSearchParams): { source: TimelineSource; q: string } {
  const source = params.get("source") || "prod6";
  const q = (params.get("q") || "").trim();
  if (source !== "prod6" && source !== "prod4") throw new Error("source must be prod6 or prod4");
  if (q.length > 120) throw new Error("q must be 120 characters or fewer");
  return { source, q };
}

type TimelineRow = {
  id: string; source_id: string; code: string; title: string; entity: string; object_type: string;
  fit_level: number | null; fit_points: number | null; score: number | null; verdict: string | null;
  official_schedule: unknown; schedule_fetched_at: string | null; published_raw?: string | null;
};

/** Single bulk query per source: active tenant profiles only, no PDF/LLM calls. */
export function timelineSql(source: TimelineSource): string {
  const prod4 = source === "prod4";
  const alias = prod4 ? "p" : "c";
  const sourceId = prod4 ? "id_procedimiento" : "id_contrato";
  const fitFunction = prod4 ? "profile_prod4_fit" : "profile_contract_fit";
  const extractionSql = prod4
    ? `SELECT e.id, e.created_at FROM prod4_document_extractions e
       WHERE e.id_procedimiento = p.id_procedimiento AND e.is_current = true AND e.quality <> 'failed'
       ORDER BY e.created_at DESC, e.id DESC LIMIT 1`
    : `SELECT e.id, e.created_at FROM tdr_extractions e
       JOIN contract_documents d ON d.id = e.contract_document_id
       WHERE d.id_contrato = c.id_contrato AND e.is_current = true AND e.quality <> 'failed'
       ORDER BY e.created_at DESC, e.id DESC LIMIT 1`;
  const matchTable = prod4 ? "opportunity_matches" : "matches";
  const matchKey = prod4 ? "m.opportunity_id = p.opportunity_id" : "m.id_contrato = c.id_contrato";
  // PROD4 matching records link the extraction explicitly; older records use their timestamp.
  const freshness = prod4
    ? `(CASE WHEN m.breakdown_json->'meta'->>'extraction_id' IS NOT NULL
        THEN m.breakdown_json->'meta'->>'extraction_id' = extraction.id::text
        ELSE m.matched_at >= extraction.created_at END)`
    : "m.matched_at >= extraction.created_at";
  return `SELECT ${alias}.opportunity_id::text AS id, ${alias}.${sourceId}::text AS source_id,
    ${prod4 ? "p.nomenclatura" : "c.codigo"} AS code,
    ${prod4 ? "COALESCE(p.title, p.description, '')" : "COALESCE(c.descripcion, '')"} AS title,
    ${prod4 ? "p.buyer_name" : "c.entidad_nombre"} AS entity,
    o.object_type, fit.fit_level, fit.fit_points, matched.score, matched.verdict,
    ${prod4 ? "p.raw_detail->'listaCronograma'" : "COALESCE(c.raw_detail_json->'uitContratoEtapaProjectionList', c.cronograma->'etapas')"} AS official_schedule,
    ${prod4 ? "NULL::text" : "COALESCE(c.raw_detail_json->'uitContratoCompletoProjection'->>'fecPublica', c.raw_search_json->>'fecPublica')"} AS published_raw,
    ${alias}.schedule_fetched_at::text AS schedule_fetched_at
  FROM ${prod4 ? "prod4_processes p" : "seace_contracts c"}
  JOIN opportunities o ON o.id = ${alias}.opportunity_id
  LEFT JOIN LATERAL (
    SELECT f.* FROM company_profiles cp
    CROSS JOIN LATERAL ${fitFunction}(cp.id, ${alias}.${sourceId}) f
    WHERE cp.tenant_id = $1 AND cp.is_active = true
    ORDER BY f.fit_score DESC, f.business_line_id LIMIT 1
  ) fit ON true
  LEFT JOIN LATERAL (${extractionSql}) extraction ON true
  LEFT JOIN LATERAL (
    SELECT m.score, m.verdict FROM ${matchTable} m
    JOIN company_profiles cp ON cp.id = m.profile_id
    WHERE cp.tenant_id = $1 AND cp.is_active = true AND ${matchKey}
      AND extraction.id IS NOT NULL AND ${freshness}
    ORDER BY m.score DESC, m.updated_at DESC, m.id DESC LIMIT 1
  ) matched ON true
  WHERE ${prod4 ? "p.technology_relevant = true AND p.missing_since IS NULL" : "c.estado_codigo = 2"}
    AND (matched.verdict IN ('verde', 'ambar') OR (matched.verdict IS NULL AND fit.fit_level >= 2))
    AND ($2 = '' OR ${prod4 ? "p.nomenclatura" : "c.codigo"} ILIKE '%' || $2 || '%'
      OR ${prod4 ? "p.title" : "c.descripcion"} ILIKE '%' || $2 || '%'
      OR ${prod4 ? "p.buyer_name" : "c.entidad_nombre"} ILIKE '%' || $2 || '%')
  ORDER BY ${alias}.${sourceId}`;
}

function limaDate(now: Date): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Lima", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const part = (type: string) => parts.find(p => p.type === type)?.value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function normalizeTimelineRows(rows: TimelineRow[], source: TimelineSource, now = new Date()): TimelineOpportunity[] {
  const today = limaDate(now);
  const mapped: TimelineOpportunity[] = rows.filter(row => row.verdict === "verde" || row.verdict === "ambar"
    || (row.verdict == null && Number(row.fit_level) >= 2)).map(row => {
    const schedule = source === "prod4" ? normalizeProd4Schedule(row.official_schedule) : normalizeProd6Schedule(row.official_schedule);
    const publication = source === "prod6" ? parseOfficialScheduleDate(row.published_raw) : null;
    if (publication && !schedule.some(stage => stage.kinds.includes("publication"))) {
      schedule.unshift({ id: `prod6-publication-${row.source_id}`, name: "Publicación", kind: "publication", kinds: ["publication"],
        startsAt: publication.value, endsAt: publication.value, startPrecision: publication.precision,
        endPrecision: publication.precision, timezone: "America/Lima" });
    }
    const deadlineStage = schedule.filter(s => s.kinds.includes("proposals") && s.endsAt && s.endPrecision)
      .sort((a, b) => a.endsAt!.localeCompare(b.endsAt!)).at(-1);
    const deadline = deadlineStage ? { at: deadlineStage.endsAt!, precision: deadlineStage.endPrecision!, name: deadlineStage.name } : null;
    const verdict = row.verdict === "verde" || row.verdict === "ambar" ? row.verdict : null;
    return {
      id: row.id, source, sourceId: row.source_id, code: row.code, title: row.title, entity: row.entity,
      objectType: row.object_type, detailHref: source === "prod4" ? `/oportunidad/seace/${row.source_id}` : `/oportunidad/${row.source_id}`,
      fitLevel: Number(row.fit_level ?? 0), fitPoints: Number(row.fit_points ?? 0),
      score: verdict ? Number(row.score) : null, verdict, status: verdict ? "evaluated" : "affinity",
      schedule, scheduleFetchedAt: row.schedule_fetched_at, deadline,
    };
  });
  return mapped.filter(row => {
    if (!row.deadline) return true;
    // A day-only deadline remains visible throughout that Lima calendar day;
    // no synthetic 23:59 deadline is exposed to the client.
    if (row.deadline.precision === "day") return row.deadline.at >= today;
    return new Date(row.deadline.at).getTime() >= now.getTime();
  }).sort((a, b) => {
    const order = (row: TimelineOpportunity) => row.deadline ? Date.parse(row.deadline.precision === "day" ? `${row.deadline.at}T00:00:00-05:00` : row.deadline.at) : Infinity;
    return order(a) - order(b) || b.fitLevel - a.fitLevel || a.code.localeCompare(b.code);
  });
}

export async function getProcurementTimeline(tenantId: string, params: ReturnType<typeof parseTimelineParams>): Promise<TimelineResponse> {
  const now = new Date();
  const result = await query<TimelineRow>(timelineSql(params.source), [tenantId, params.q]);
  const data = normalizeTimelineRows(result.rows, params.source, now);
  return { data, meta: { source: params.source, count: data.length, generatedAt: now.toISOString(), timezone: "America/Lima" } };
}
