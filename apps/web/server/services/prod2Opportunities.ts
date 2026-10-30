import { query } from "@/server/db/client";
import { buildProd2Analysis, normalizeProd2Schedule, type Prod2Extraction } from "@/lib/prod2Analysis";
import type { Prod4DocumentMatch, Prod4RequirementFacet } from "@/lib/prod4Analysis";
import { prod2StorageAvailable, signedProd2DocumentUrl, validProd2PreviewKey } from "./prod2DocumentStorage";
import { prod2OfficialDocumentReference, resolveProd2OfficialDocument } from "./prod2OfficialDocument";
import { keywordTextFitLateralSql } from "./keywordTextScoring";
import { savedOpportunityIds } from "./savedOpportunities";
import { prod2CommercialAffinityJoin, prod2CommercialContext } from "./prod2CommercialAffinity";

export class Prod2RequestError extends Error {
  constructor(message: string, public status = 400) { super(message); }
}

export type Prod2Deadline = "open" | "closed" | "all";
export type Prod2ListParams = { object: "good" | "service" | null; q: string; page: number; pageSize: number; profileId: string | null; analysis?: "done" | null; withScore?: boolean; deadline?: Prod2Deadline | null };
type Prod2Row = Record<string, any> & {
  id_convocatoria: string; id_procedimiento: string; opportunity_id: string; source_key: "seace_prod2";
  nomenclatura: string; title: string; description: string; object_type: "good" | "service";
  buyer_name: string; published_at: string; reference_amount: string | number | null; currency: string;
  last_seen_at: string; source_url: string; detail_fetched_at: string; schedule_fetched_at: string;
  registration_closes_at: string | null; proposals_start_at: string | null; proposals_closes_at: string | null;
  procedure_type: string | null; schedule_json: unknown; item_states: string[];
  document_inventory_total: number | null; document_inventory_complete: boolean; documents_count: number;
  document_publication_events: unknown; document_publication_events_count: number;
  match_id: string | null; match_review_required: boolean; provisional_score: number | null; match_score: number | null; match_verdict: string | null;
  participation_extraction: unknown;
  has_official_detail?: boolean; publication_status?: string | null; jev_score?: number | null;
};
type Prod2DocumentRow = {
  codigo_alfresco: string; name: string; document_type: string; stage_name: string; published_at: string | null;
  published_at_text: string; extension: string | null; sha256_original: string | null; bytes: string | null; storage_key: string | null;
  official_download_url: string;
};
type Prod2PublicationEvent = {
  stage_name: string; document_type: string; published_at: string | null; published_at_text: string;
};
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function officialDocumentsEnabled() { return process.env.PROD2_OFFICIAL_DOCUMENTS_ENABLED === "true"; }
export function parseProd2ListParams(params: URLSearchParams): Prod2ListParams {
  const object = params.get("object");
  if (object && object !== "good" && object !== "service") throw new Prod2RequestError("object must be good or service");
  const q = (params.get("q") ?? "").trim();
  if (q.length > 120) throw new Prod2RequestError("q must be 120 characters or fewer");
  const page = Number(params.get("page") ?? 1);
  const pageSize = Number(params.get("page_size") ?? 20);
  if (!Number.isSafeInteger(page) || page < 1) throw new Prod2RequestError("page must be a positive integer");
  if (!Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 100) throw new Prod2RequestError("page_size must be between 1 and 100");
  const profileId = params.get("profile_id");
  if (profileId && !uuidPattern.test(profileId)) throw new Prod2RequestError("profile_id must be a UUID");
  const analysis = params.get("analysis");
  if (analysis && analysis !== "done") throw new Prod2RequestError("analysis must be done");
  const withScore = params.get("with_score");
  if (withScore && withScore !== "true" && withScore !== "false") throw new Prod2RequestError("with_score must be true or false");
  // Without a deadline the API keeps its full, unordered-by-closing listing; the feed page asks for one.
  const deadline = params.get("deadline");
  if (deadline && deadline !== "open" && deadline !== "closed" && deadline !== "all") throw new Prod2RequestError("deadline must be open, closed or all");
  // Los parámetros commercial_* de enlaces antiguos se ignoran: el buscador ya no filtra por categoría.
  return { object: object as Prod2ListParams["object"], q, page, pageSize, profileId, analysis: analysis ? "done" : null, withScore: withScore === "true",
    deadline: (deadline || null) as Prod2Deadline | null };
}

async function resolveProfile(tenantId: string, profileId: string | null) {
  const result = await query<{ id: string; razon_social: string }>(
    `SELECT id::text, razon_social FROM company_profiles
     WHERE tenant_id = $1 AND is_active = true AND ($2::uuid IS NULL OR id = $2::uuid)
     ORDER BY created_at, id LIMIT 1`, [tenantId, profileId]);
  if (profileId && !result.rows[0]) throw new Prod2RequestError("Perfil no disponible en tu empresa.", 404);
  return result.rows[0] ?? null;
}

async function currentCommercialContext(tenantId: string, profileId: string | null) {
  if (!profileId) return null;
  const result = await query<{ profile_hash: string | null; commercial_description: string; active_business_lines: string[] }>(
    `SELECT cp.profile_hash, COALESCE(cp.identity_json->>'descripcion', '') AS commercial_description,
      ARRAY(SELECT bl.nombre FROM business_lines bl WHERE bl.profile_id = cp.id AND bl.is_active = true) AS active_business_lines
     FROM company_profiles cp WHERE cp.id = $1::uuid AND cp.tenant_id = $2::uuid AND cp.is_active = true`, [profileId, tenantId]);
  return result.rows[0] ? prod2CommercialContext(result.rows[0]) : null;
}

const BASE = `p.id_convocatoria::text, p.id_procedimiento::text, p.opportunity_id::text,
  'seace_prod2'::text AS source_key, p.nomenclature AS nomenclatura,
  p.description AS title, p.description, p.object_type, p.buyer_name,
  p.published_at::text, p.reference_amount, p.currency, p.last_seen_at::text,
  COALESCE(d.source_url, 'https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml') AS source_url,
  (d.id_convocatoria IS NOT NULL) AS has_official_detail, pf.reader_status AS publication_status,
  jev_union.affinity_index AS jev_score,
  d.detail_fetched_at::text, d.detail_fetched_at::text AS schedule_fetched_at,
  d.registration_closes_at::text, d.proposals_start_at::text, d.proposals_closes_at::text,
  d.raw_detail->>'selection_type' AS procedure_type,
  d.schedule_json, d.item_states, d.document_inventory_total, d.document_inventory_complete,
  d.raw_detail->'document_events' AS document_publication_events,
  CASE WHEN jsonb_typeof(d.raw_detail->'document_events') = 'array'
    THEN jsonb_array_length(d.raw_detail->'document_events')
    ELSE (SELECT count(*)::int FROM prod2_documents pd WHERE pd.id_convocatoria = p.id_convocatoria AND pd.is_listed)
  END AS document_publication_events_count,
  (SELECT count(*)::int FROM prod2_documents pd WHERE pd.id_convocatoria = p.id_convocatoria AND pd.is_listed) AS documents_count,
  (SELECT jsonb_build_object('id', ex.id::text, 'quality', ex.quality,
    'requires_human_review', ex.requires_human_review,
    'participation', COALESCE(NULLIF(ex.summary_json->'participation', 'null'::jsonb), ex.raw_extraction_json->'participation'),
    'page_scope', 'combined')
   FROM prod2_document_extractions ex
   WHERE ex.id_convocatoria = p.id_convocatoria AND ex.is_current
   ORDER BY ex.created_at DESC, ex.id DESC LIMIT 1) AS participation_extraction`;

// Publishing selected listing metadata does not create a ficha. Legacy rows with
// neither a real detail nor this explicit, current-hash publication stay hidden.
const PUBLISHED_FROM = `prod2_discoveries p LEFT JOIN prod2_details d USING(id_convocatoria)
  LEFT JOIN prod2_feed_publications pf ON pf.id_convocatoria = p.id_convocatoria AND pf.source_row_hash = p.row_hash`;
const PUBLISHED_VISIBILITY = "(d.id_convocatoria IS NOT NULL OR pf.id_convocatoria IS NOT NULL)";
const publicationLabels: Record<string, string> = {
  closed_without_reading: "Cerrada al revisar", rejected_below_60: "Descartada en la revisión previa",
  opening_not_confirmed: "Apertura por confirmar", needs_review: "Lectura por revisar",
  selection_needs_review: "Documento por seleccionar", llm1_complete: "Lectura previa conservada",
  accepted_unread: "Por calificar",
};

// Closing follows the date the list shows: proposals, else registration. A day-only official
// proposals stage closes once its Lima day has passed (compared as YYYYMMDD text, so a malformed
// date can never fail the query), and a row the reader already saw closed stays closed.
// An unknown closing date is not closed.
const DAY_ONLY_PROPOSALS_END = `(SELECT max(substring(stage->>'ends_at_text' from 7 for 4) || substring(stage->>'ends_at_text' from 4 for 2) || substring(stage->>'ends_at_text' from 1 for 2))
  FROM jsonb_array_elements(CASE WHEN jsonb_typeof(d.schedule_json) = 'array' THEN d.schedule_json ELSE '[]'::jsonb END) stage
  WHERE stage->>'ends_at_text' ~ '^\\d{2}/\\d{2}/\\d{4}'
    AND stage->>'stage' ~* '(cotizaci|presentaci[oó]n.*(oferta|propuesta)|recepci[oó]n.*(oferta|propuesta))'
    AND stage->>'stage' !~* '(absoluci|respuesta|contestaci)')`;
const CLOSED = `COALESCE(COALESCE(d.proposals_closes_at, d.registration_closes_at) < now()
  OR (d.proposals_closes_at IS NULL AND d.registration_closes_at IS NULL
    AND ${DAY_ONLY_PROPOSALS_END} < to_char(now() AT TIME ZONE 'America/Lima', 'YYYYMMDD'))
  OR pf.reader_status = 'closed_without_reading', false)`;
// Afinidad JEV por título de la convocatoria (0–100): una sola fila canónica por convocatoria, igual para todas las
// empresas, por eso no se filtra por perfil. Manda la última fila terminada. Es el puntaje que decide la lectura:
// desde JEV_ACCEPTANCE_THRESHOLD se acepta. Un valor que no sea número se trata como sin puntaje.
const JEV_ACCEPTANCE_THRESHOLD = 60;
const JEV_UNION_POLICY = "prod2_commercial_union_v1";
const JEV_UNION_JOIN = `LEFT JOIN LATERAL (
  SELECT CASE WHEN jsonb_typeof(ev.result_json->'affinity_index') = 'number'
    THEN (ev.result_json->>'affinity_index')::double precision END AS affinity_index
  FROM prod2_evaluations ev
  WHERE ev.id_convocatoria = p.id_convocatoria AND ev.kind = 'jev_affinity' AND ev.status = 'done'
    AND ev.policy_version = '${JEV_UNION_POLICY}'
  ORDER BY ev.completed_at DESC, ev.id DESC LIMIT 1
) jev_union ON true`;
// Mayores lista solo lo aceptado por JEV, en toda vista y en todo conteo: no salen las descartadas en la revisión
// previa (nunca se leen, así que jamás tendrán fecha de cierre) ni las de afinidad vigente menor a 60. Sin fila JEV
// (filas previas: cerrada al revisar, apertura por confirmar...) siguen visibles. Nada se borra.
const NOT_DISCARDED = "pf.reader_status IS DISTINCT FROM 'rejected_below_60'";
const JEV_ACCEPTED = `(jev_union.affinity_index IS NULL OR jev_union.affinity_index >= ${JEV_ACCEPTANCE_THRESHOLD})`;

// Canonical matches must be tied to this source, this document revision and the durable evaluation.
const MATCH_JOIN = `LEFT JOIN LATERAL (
  SELECT m.*, provenance.profile_hash AS evaluated_profile_hash,
    ex.requires_human_review AS evaluated_source_review_required
  FROM opportunity_matches m
  JOIN prod2_match_provenance provenance ON provenance.match_id = m.id
  JOIN prod2_document_extractions ex ON ex.id = provenance.extraction_id AND ex.is_current = true AND ex.quality <> 'failed'
  JOIN prod2_evaluations ev ON ev.id = provenance.evaluation_id AND ev.status = 'done' AND ev.kind = 'gemini_match'
  WHERE m.profile_id = $1::uuid AND m.opportunity_id = p.opportunity_id
    AND provenance.id_convocatoria = p.id_convocatoria AND ex.id_convocatoria = p.id_convocatoria
    AND ev.id_convocatoria = p.id_convocatoria AND ev.profile_id = m.profile_id
    AND m.breakdown_json->'meta'->>'source_key' = 'seace_prod2'
    AND m.breakdown_json->'meta'->>'id_convocatoria' = p.id_convocatoria::text
    AND m.breakdown_json->'meta'->>'extraction_id' = ex.id::text
    AND m.breakdown_json->'meta'->>'document_sha256' = ex.sha256_original
    AND m.breakdown_json->'meta'->>'profile_hash' = provenance.profile_hash
  ORDER BY m.updated_at DESC LIMIT 1
) matched ON true`;

// Keep the durable match and its evidence visible while its model score is
// under review. Missing metadata preserves already published legacy scores.
const PUBLISHED_MATCH_SCORE = "CASE WHEN matched.breakdown_json->'meta'->>'score_validated' = 'false' THEN NULL ELSE matched.score END";
const PUBLISHED_MATCH_VERDICT = "CASE WHEN matched.breakdown_json->'meta'->>'score_validated' = 'false' THEN 'gris' ELSE matched.verdict END";
const MATCH_REVIEW_REQUIRED = "(COALESCE(matched.breakdown_json->'meta'->>'score_validated' = 'false', false) OR COALESCE(matched.evaluated_source_review_required, false))";
const PROVISIONAL_MATCH_SCORE = `CASE WHEN ${MATCH_REVIEW_REQUIRED} AND matched.score >= 0 AND matched.score <= 100 THEN matched.score ELSE NULL END`;
// The same retained 0–100 number shown by the UI, whether final or provisional.
// This is never a keyword-affinity score and never substitutes zero for null.
const AVAILABLE_MATCH_SCORE = "CASE WHEN matched.id IS NOT NULL AND matched.score BETWEEN 0 AND 100 THEN matched.score ELSE NULL END";
const MATCH_STATUS_SELECT = `matched.id::text AS match_id, ${MATCH_REVIEW_REQUIRED} AS match_review_required, ${PROVISIONAL_MATCH_SCORE} AS provisional_score`;

const KEYWORD_FIT_JOIN = keywordTextFitLateralSql(1, "p.description");
const KEYWORD_FIT_SELECT = `fit.keyword_points, fit.fit_points, fit.fit_level,
  fit.business_line_id AS fit_business_line_id, fit.business_line_name,
  COALESCE(fit.keyword_hits, '[]'::jsonb) AS keyword_hits`;

function provisionalScore(row: Prod2Row) {
  return row.match_id && row.match_review_required === true && typeof row.provisional_score === "number"
    && Number.isFinite(row.provisional_score) && row.provisional_score >= 0 && row.provisional_score <= 100
    ? row.provisional_score : null;
}

// Afinidad JEV de la convocatoria, o null cuando no hay puntaje válido.
function jevScore(row: Prod2Row) {
  return typeof row.jev_score === "number" && Number.isFinite(row.jev_score) && row.jev_score >= 0 && row.jev_score <= 100 ? row.jev_score : null;
}

// Accepted by JEV but neither read nor evaluated (below the automatic reading threshold): waits for a manual grading.
function publicationStatus(row: Prod2Row) {
  if (row.publication_status) return row.publication_status in publicationLabels ? row.publication_status : null;
  const score = jevScore(row);
  return score != null && score >= JEV_ACCEPTANCE_THRESHOLD && row.participation_extraction == null && !row.match_id
    ? "accepted_unread" : null;
}

function processRow(row: Prod2Row) {
  const { schedule_json, document_publication_events: _publicationEvents, ...process } = row;
  const status = publicationStatus(row);
  return { ...process,
    has_official_detail: row.has_official_detail !== false,
    publication_status: status,
    publication_status_label: status ? publicationLabels[status] : null,
    provisional_score: provisionalScore(row), jev_score: jevScore(row), region: null, technology_segments: [], technology_match_reason: [], items_count: 0,
    fit_points: row.fit_points ?? null, keyword_points: row.keyword_points ?? null,
    fit_score: null, fit_level: row.fit_level ?? null, business_line_name: row.business_line_name ?? null,
    keyword_hits: row.keyword_hits ?? [], fit_basis: "profile_keywords" as const,
    source_window_status: "observed" as const, source_presence_label: row.has_official_detail === false ? "Datos del listado PROD2" : "Consultada en PROD2",
    official_schedule: normalizeProd2Schedule(schedule_json) };
}

function publicationHistory(events: unknown, document: Prod2DocumentRow): Prod2PublicationEvent[] {
  // Old captures predate the additive history array and preserve their one-row
  // representation. New captures expose every stage/date for the same UUID.
  if (events == null) return [{ stage_name: document.stage_name, document_type: document.document_type,
    published_at: document.published_at, published_at_text: document.published_at_text }];
  if (!Array.isArray(events)) return [];
  return events.flatMap((event) => {
    if (!event || typeof event !== "object" || event.uuid !== document.codigo_alfresco
      || event.filename !== document.name || typeof event.stage !== "string" || typeof event.type !== "string"
      || typeof event.published_at_text !== "string" || (event.published_at !== null && typeof event.published_at !== "string")) return [];
    return [{ stage_name: event.stage, document_type: event.type,
      published_at: event.published_at, published_at_text: event.published_at_text }];
  });
}

export async function listProd2OpportunitiesForTenant(tenantId: string, params: Prod2ListParams) {
  const profile = await resolveProfile(tenantId, params.profileId);
  const commercialContext = await currentCommercialContext(tenantId, profile?.id ?? null);
  const values: unknown[] = [profile?.id ?? null];
  // Include explicitly published selections without pretending a ficha exists.
  const where = [PUBLISHED_VISIBILITY, NOT_DISCARDED, JEV_ACCEPTED];
  if (params.object) { values.push(params.object); where.push(`p.object_type = $${values.length}`); }
  if (params.q) { values.push(`%${params.q}%`); where.push(`(p.nomenclature ILIKE $${values.length} OR p.description ILIKE $${values.length} OR p.buyer_name ILIKE $${values.length})`); }
  // A saved analysis exists even when its score is unpublished. Match provenance
  // and the chosen tenant-owned profile establish availability, never score > 0.
  if (params.analysis === "done") where.push("matched.id IS NOT NULL");
  if (params.withScore) where.push(`${AVAILABLE_MATCH_SCORE} IS NOT NULL`);
  if (params.deadline === "open") where.push(`NOT ${CLOSED}`);
  if (params.deadline === "closed") where.push(CLOSED);
  const predicate = where.join(" AND ");
  const count = await query<{ total: number; open_total?: number }>(`SELECT count(*) FILTER (WHERE NOT ${CLOSED})::int AS open_total,
    count(*)::int AS total FROM ${PUBLISHED_FROM}
    ${JEV_UNION_JOIN}
    ${params.analysis === "done" || params.withScore ? MATCH_JOIN : ""}
    WHERE ($1::uuid IS NULL OR $1::uuid IS NOT NULL) AND ${predicate}`, values);
  const pageValues = [...values];
  // Tras el puntaje de requisitos desempata primero la afinidad JEV de la convocatoria (la que se muestra) y luego la
  // afinidad DIN por título de la empresa, que solo ordena.
  const commercialAffinityJoin = prod2CommercialAffinityJoin(pageValues, commercialContext, tenantId);
  const rows = await query<Prod2Row>(`SELECT ${BASE}, ${KEYWORD_FIT_SELECT},
    ${MATCH_STATUS_SELECT}, ${PUBLISHED_MATCH_SCORE} AS match_score, ${PUBLISHED_MATCH_VERDICT} AS match_verdict
    FROM ${PUBLISHED_FROM}
    ${MATCH_JOIN} ${JEV_UNION_JOIN} ${commercialAffinityJoin} ${KEYWORD_FIT_JOIN}
    WHERE ${predicate}
    ORDER BY ${params.deadline === "all" ? `${CLOSED}, CASE WHEN ${CLOSED} THEN p.published_at END DESC NULLS LAST, ` : ""}${AVAILABLE_MATCH_SCORE} DESC NULLS LAST, jev_union.affinity_index DESC NULLS LAST, commercial_jev.affinity_index DESC NULLS LAST, fit.keyword_points DESC NULLS LAST,
      ${PUBLISHED_MATCH_SCORE} DESC NULLS LAST,
      p.published_at DESC NULLS LAST, p.id_convocatoria DESC
    LIMIT $${pageValues.length + 1} OFFSET $${pageValues.length + 2}`, [...pageValues, params.pageSize, (params.page - 1) * params.pageSize]);
  const saved = await savedOpportunityIds(tenantId, rows.rows.map(row => row.opportunity_id));
  return { data: rows.rows.map(row => ({ ...processRow(row), is_saved: saved.has(row.opportunity_id) })), total: count.rows[0]?.total ?? 0,
    open_total: count.rows[0]?.open_total ?? 0, page: params.page, page_size: params.pageSize,
    profile: profile ? { id: profile.id, name: profile.razon_social } : null };
}

export async function getProd2OpportunityForTenant(tenantId: string, id: string, profileId: string | null = null) {
  if (profileId && !uuidPattern.test(profileId)) throw new Prod2RequestError("profile_id must be a UUID");
  const profile = await resolveProfile(tenantId, profileId);
  const values: unknown[] = [profile?.id ?? null, id];
  const result = await query<Prod2Row>(`SELECT ${BASE}, ${KEYWORD_FIT_SELECT},
    ${MATCH_STATUS_SELECT}, matched.profile_id::text AS match_profile_id,
    ${PUBLISHED_MATCH_SCORE} AS match_score, ${PUBLISHED_MATCH_VERDICT} AS match_verdict,
    matched.breakdown_json, matched.missing_actions_json,
    matched.matched_at::text, matched.updated_at::text AS match_updated_at, matched.evaluated_profile_hash
    FROM ${PUBLISHED_FROM}
    ${MATCH_JOIN} ${JEV_UNION_JOIN} ${KEYWORD_FIT_JOIN}
    WHERE ${PUBLISHED_VISIBILITY} AND p.id_convocatoria = $2::bigint`, values);
  const row = result.rows[0];
  if (!row) return null;
  const [documents, extraction] = await Promise.all([
    query<Prod2DocumentRow>(`SELECT pd.codigo_alfresco::text, pd.name, pd.document_type, pd.stage_name,
      pd.published_at::text, pd.published_at_text, pd.source_url AS official_download_url,
      dl.format AS extension, dl.sha256_original, dl.bytes::text, dl.storage_key
      FROM prod2_documents pd LEFT JOIN LATERAL (
        SELECT format, sha256_original, bytes, storage_key FROM prod2_document_downloads
        WHERE id_convocatoria = pd.id_convocatoria AND codigo_alfresco = pd.codigo_alfresco
        ORDER BY downloaded_at DESC LIMIT 1
      ) dl ON true WHERE pd.id_convocatoria = $1::bigint AND pd.is_listed ORDER BY pd.published_at DESC NULLS LAST, pd.name`, [id]),
    query<Prod2Extraction>(`SELECT id::text, codigo_alfresco::text, sha256_original, summary_json, raw_extraction_json,
      model, prompt_version, schema_version, quality, requires_human_review, facet_count, created_at::text
      FROM prod2_document_extractions WHERE id_convocatoria = $1::bigint AND is_current
      ORDER BY created_at DESC, id DESC LIMIT 1`, [id]),
  ]);
  const currentExtraction = extraction.rows[0] ?? null;
  const facets = currentExtraction ? await query<Prod4RequirementFacet>(`SELECT f.id::text, f.facet, f.label, f.required, f.details_json, f.evidence_json
      FROM prod2_requirement_facets f JOIN prod2_document_extractions ex ON ex.id = f.extraction_id AND ex.is_current
      WHERE f.id_convocatoria = $1::bigint AND f.extraction_id = $2::bigint AND f.is_current ORDER BY f.id`, [id, currentExtraction.id]) : { rows: [] };
  const match: Prod4DocumentMatch | null = row.match_id && row.match_verdict != null ? { id: row.match_id, profile_id: row.match_profile_id,
    score: row.match_score, provisional_score: provisionalScore(row), verdict: row.match_verdict, breakdown_json: row.breakdown_json,
    missing_actions_json: row.missing_actions_json, matched_at: row.matched_at, updated_at: row.match_updated_at } : null;
  const analysis = buildProd2Analysis({ idConvocatoria: id, profileHash: row.evaluated_profile_hash ?? null,
    extraction: currentExtraction, facets: facets.rows, match });
  const { breakdown_json: _breakdown, missing_actions_json: _actions, evaluated_profile_hash: _hash, ...publicRow } = row;
  const saved = await savedOpportunityIds(tenantId, [row.opportunity_id]);
  const process = { ...processRow(publicRow), is_saved: saved.has(row.opportunity_id) };
  const officialEnabled = officialDocumentsEnabled();
  const canDownload = prod2StorageAvailable();
  return { process, items: [], documents: documents.rows.map(({ storage_key, ...document }) => {
    const publication_events = publicationHistory(row.document_publication_events, { ...document, storage_key });
    return { ...document, publication_events, publication_events_count: publication_events.length,
      download_url: (officialEnabled ? prod2OfficialDocumentReference(document.codigo_alfresco)
        : canDownload && storage_key && validProd2PreviewKey(storage_key, id, document.codigo_alfresco))
        ? `/api/prod2/opportunities/${id}/documents/${document.codigo_alfresco}` : null };
  }),
    official_schedule: process.official_schedule, analysis,
    profile: profile ? { id: profile.id, name: profile.razon_social } : null };
}

export async function getProd2DocumentUrl(id: string, documentCode: string) {
  if (!/^[1-9]\d{0,18}$/.test(id) || !uuidPattern.test(documentCode)) throw new Prod2RequestError("Invalid document identity");
  // Default preserves the deployed, verified historical preview path. Official
  // ticket resolution remains an explicit opt-in pending live availability QA.
  if (!officialDocumentsEnabled()) {
    const result = await query<{ storage_key: string; sha256_original: string }>(`SELECT dl.storage_key, dl.sha256_original
      FROM prod2_document_downloads dl JOIN prod2_documents pd USING(id_convocatoria, codigo_alfresco)
      WHERE dl.id_convocatoria = $1::bigint AND dl.codigo_alfresco = $2::uuid AND pd.is_listed
        AND dl.storage_key IS NOT NULL ORDER BY dl.downloaded_at DESC LIMIT 1`, [id, documentCode]);
    const document = result.rows[0];
    if (!document || !validProd2PreviewKey(document.storage_key, id, documentCode)) return null;
    return signedProd2DocumentUrl(document.storage_key, id, documentCode);
  }
  const result = await query<{ storage_key: string | null }>(`SELECT dl.storage_key
    FROM prod2_documents pd LEFT JOIN LATERAL (
      SELECT storage_key FROM prod2_document_downloads
      WHERE id_convocatoria = pd.id_convocatoria AND codigo_alfresco = pd.codigo_alfresco AND storage_key IS NOT NULL
      ORDER BY downloaded_at DESC LIMIT 1
    ) dl ON true WHERE pd.id_convocatoria = $1::bigint AND pd.codigo_alfresco = $2::uuid AND pd.is_listed`, [id, documentCode]);
  const document = result.rows[0];
  if (!document) return null;
  try { return await resolveProd2OfficialDocument(documentCode); }
  catch {
    // Keep already verified historical previews usable when the official host is unavailable.
    if (document.storage_key && validProd2PreviewKey(document.storage_key, id, documentCode)) {
      const historical = signedProd2DocumentUrl(document.storage_key, id, documentCode);
      if (historical) return historical;
    }
    throw new Prod2RequestError("SEACE no pudo proporcionar el enlace de descarga. Inténtalo nuevamente o abre la ficha oficial.", 503);
  }
}
