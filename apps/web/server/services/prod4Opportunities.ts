import { query } from "@/server/db/client";

type SourceWindowState = "current" | "exited" | "all";

export type Prod4ListParams = {
  object: "good" | "service" | null;
  q: string;
  state: SourceWindowState;
  page: number;
  pageSize: number;
};

export function parseProd4ListParams(params: URLSearchParams): Prod4ListParams {
  const object = params.get("object");
  if (object && object !== "good" && object !== "service") {
    throw new Error("object must be good or service");
  }
  const state = params.get("state") || "current";
  if (state !== "current" && state !== "exited" && state !== "all") {
    throw new Error("state must be current, exited or all");
  }
  const q = (params.get("q") || "").trim();
  if (q.length > 120) throw new Error("q must be 120 characters or fewer");
  const page = Number(params.get("page") || "1");
  const pageSize = Number(params.get("page_size") || "20");
  if (!Number.isSafeInteger(page) || page < 1) throw new Error("page must be a positive integer");
  if (!Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 100) {
    throw new Error("page_size must be between 1 and 100");
  }
  return { object: object as Prod4ListParams["object"], q, state, page, pageSize };
}

const BASE_SELECT = `
  p.id_procedimiento::text AS id_procedimiento,
  p.opportunity_id,
  'seace_prod4'::text AS source_key,
  p.nomenclatura,
  p.title,
  p.description,
  p.object_type,
  p.procedure_type,
  p.buyer_name,
  p.region,
  p.published_at,
  p.registration_closes_at,
  p.proposals_start_at,
  p.proposals_closes_at,
  p.reference_amount,
  p.currency,
  p.source_url,
  p.technology_match_reason,
  p.last_seen_at,
  p.missing_since,
  CASE WHEN p.missing_since IS NULL THEN 'current' ELSE 'exited' END AS source_window_status,
  o.record_kind,
  o.procurement_method,
  o.lifecycle_stage,
  o.participation_access,
  o.actionability,
  (SELECT count(*)::int FROM prod4_items i WHERE i.id_procedimiento = p.id_procedimiento) AS items_count,
  (SELECT count(*)::int FROM prod4_documents d WHERE d.id_procedimiento = p.id_procedimiento) AS documents_count,
  COALESCE((
    SELECT array_agg(DISTINCT left(i.cubso_code, 2))
    FROM prod4_items i
    WHERE i.id_procedimiento = p.id_procedimiento
      AND i.cubso_code ~ '^[0-9]{2}'
  ), ARRAY[]::text[]) AS technology_segments
`;

export async function listProd4Opportunities(params: Prod4ListParams) {
  const values: unknown[] = [];
  const where = ["p.technology_relevant = true"];
  if (params.object) {
    values.push(params.object);
    where.push(`p.object_type = $${values.length}`);
  }
  if (params.state !== "all") {
    where.push(params.state === "current" ? "p.missing_since IS NULL" : "p.missing_since IS NOT NULL");
  }
  if (params.q) {
    values.push(`%${params.q}%`);
    where.push(`(p.nomenclatura ILIKE $${values.length} OR p.title ILIKE $${values.length} OR p.description ILIKE $${values.length} OR p.buyer_name ILIKE $${values.length})`);
  }
  const whereSql = where.join(" AND ");
  const count = await query<{ total: number }>(
    `SELECT count(*)::int AS total FROM prod4_processes p WHERE ${whereSql}`,
    values,
  );
  const offset = (params.page - 1) * params.pageSize;
  const rows = await query(
    `SELECT ${BASE_SELECT}
     FROM prod4_processes p
     JOIN opportunities o ON o.id = p.opportunity_id
     WHERE ${whereSql}
     ORDER BY p.proposals_closes_at ASC NULLS LAST, p.published_at DESC NULLS LAST, p.id_procedimiento DESC
     LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
    [...values, params.pageSize, offset],
  );
  return {
    data: rows.rows,
    total: count.rows[0]?.total ?? 0,
    page: params.page,
    page_size: params.pageSize,
  };
}

// PROD4 is public procurement data, but the web surface remains tenant-authenticated.
// The tenant parameter is deliberately kept in the service contract for future
// tenant-specific relevance/matching without changing callers.
export async function listProd4OpportunitiesForTenant(_tenantId: string, params: Prod4ListParams) {
  return listProd4Opportunities(params);
}

export async function getProd4Opportunity(id: string) {
  const process = await query(
    `SELECT ${BASE_SELECT}, p.id_convocatoria_pub::text AS id_convocatoria_pub,
            p.numero_procedimiento, p.buyer_id
     FROM prod4_processes p
     JOIN opportunities o ON o.id = p.opportunity_id
     WHERE p.id_procedimiento = $1 AND p.technology_relevant = true`,
    [id],
  );
  if (!process.rows[0]) return null;
  const [items, documents, schedule] = await Promise.all([
    query(
      `SELECT nro_item, cubso_code, description, quantity, unit
       FROM prod4_items WHERE id_procedimiento = $1 ORDER BY nro_item`,
      [id],
    ),
    query(
      `SELECT codigo_alfresco, name, document_type, extension, published_at, source_url
       FROM prod4_documents WHERE id_procedimiento = $1 ORDER BY published_at DESC NULLS LAST, name`,
      [id],
    ),
    query(
      `SELECT position, stage_key, stage_name, starts_at, ends_at
       FROM prod4_schedule WHERE id_procedimiento = $1 ORDER BY position`,
      [id],
    ),
  ]);
  return {
    process: process.rows[0],
    items: items.rows,
    documents: documents.rows,
    schedule: schedule.rows,
  };
}

export async function getProd4OpportunityForTenant(_tenantId: string, id: string) {
  return getProd4Opportunity(id);
}
