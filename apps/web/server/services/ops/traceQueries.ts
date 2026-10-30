import { EVALUATION_ORIGIN, reviewCodes } from "./steps.ts";

/**
 * Consultas SQL de la «Traza» (solo texto, sin acceso a base de datos). Todas piden columnas concretas y, para los JSON, solo claves conocidas
 * (`pick`): ni texto completo de documentos ni respuestas crudas del proveedor salen de la base.
 */

const pick = (expression: string, keys: readonly string[]) =>
  `(SELECT COALESCE(jsonb_object_agg(t.k, t.v), '{}'::jsonb)
    FROM jsonb_each(CASE WHEN jsonb_typeof(${expression}) = 'object' THEN ${expression} ELSE '{}'::jsonb END) AS t(k, v)
    WHERE t.k IN (${keys.map((key) => `'${key}'`).join(", ")}))`;

const JEV_KEYS = ["alias", "score", "confidence", "domain", "information", "subcategory", "companies_in_query", "query_cost_usd", "query_input_tokens", "query_output_tokens", "inherited_from"] as const;
const JOB_PAYLOAD_KEYS = ["id_contrato", "id_contrato_archivo", "profile_id", "scope_profile_id", "source", "origin", "segment", "batch_id", "selection_reason", "source_runtime", "manual_request_id", "fit_score", "fit_level", "fit_points", "business_line_id", "extraction_id"] as const;
const PROD2_RESULT_KEYS = ["affinity_index", "affinity_confidence", "domain", "information", "needs_detail", "cache_hit", "score", "verdict", "status", "reason", "failure_reason", "error_type", "finish_reason", "finish_reasons", "model", "policy_version", "evaluation_origin", "manual_request_id", "extraction_id", "document_scope", "coverage_valid", "score_validated", "cost_is_estimate", "provider_usage_known", "input_tokens", "output_tokens", "cost_usd", "incurred_cost_usd", "attempt_id"] as const;
const MATCH_META_KEYS = ["model", "cost_usd", "input_tokens", "output_tokens", "analyzed_at", "evaluation_origin", "execution", "extraction_id", "score_validated", "prompt_version", "manual_request_id"] as const;

export const MINOR_QUERIES = {
  contract: `
SELECT c.id_contrato::text AS id_contrato, c.codigo, c.entidad_nombre, c.descripcion, c.estado_codigo, c.pipeline_state,
       c.valor_estimado::text AS valor_estimado, c.fec_publica, c.fec_fin_cotizacion, c.first_seen_at, c.last_seen_at,
       c.detail_fetched_at, c.cubso_segmento
FROM seace_contracts c WHERE c.id_contrato = $1::bigint`,
  decisions: `
SELECT d.id::text AS id, d.profile_id::text AS profile_id, cp.razon_social, d.codigo, d.cubso_segmento, d.fec_fin_cotizacion,
       d.policy_version, d.prefilter_passed, d.prefilter_reasons, d.keyword_lines, d.decision, d.decided_by,
       d.score::text AS score, d.threshold::text AS threshold, d.domain, d.subcategory, d.information, d.model,
       d.input_tokens, d.output_tokens, d.cost_usd::text AS cost_usd, d.attempts, d.last_error, d.created_at, d.updated_at,
       d.listing_json->>'nomEntidad' AS entidad, d.listing_json->>'desContratacion' AS descripcion,
       ${pick("d.result_json", JEV_KEYS)} AS result_json
FROM prod6_profile_decisions d LEFT JOIN company_profiles cp ON cp.id = d.profile_id
WHERE d.id_contrato = $1::bigint ORDER BY d.id`,
  // El contrato vive en el payload (sin índice): una pasada por la tabla por consulta de traza, no por fila.
  jobs: `
SELECT j.id::text AS id, j.job_type, j.queue_name, j.status, j.attempts, j.max_attempts, j.priority, j.created_at,
       j.claimed_at, j.finished_at, (j.run_after = 'infinity'::timestamptz) AS held, j.last_error,
       j.payload->>'profile_id' AS profile_id, ${pick("j.payload", JOB_PAYLOAD_KEYS)} AS payload,
       jsonb_build_object('outcome', j.payload->'runtime_result'->'outcome', 'reason', j.payload->'runtime_result'->'reason',
                          'extraction_id', j.payload->'runtime_result'->'extraction_id', 'facets', j.payload->'runtime_result'->'facets') AS runtime_result,
       count(*) OVER ()::int AS total
FROM worker_jobs j
WHERE j.payload->>'id_contrato' = $1::text
ORDER BY (j.status = 'done'), j.id DESC
LIMIT 100`,
  documents: `
SELECT cd.id::text AS id, cd.id_contrato_archivo::text AS id_contrato_archivo, cd.categoria, cd.filename, cd.mime, cd.doc_class,
       cd.has_text_layer, cd.size_original_bytes::text AS size_original_bytes, left(cd.sha256_original, 16) AS sha256_original, cd.created_at
FROM contract_documents cd WHERE cd.id_contrato = $1::bigint ORDER BY cd.created_at, cd.id`,
  readings: `
SELECT te.id::text AS id, te.contract_document_id::text AS contract_document_id, cd.filename, te.model, te.prompt_version,
       te.schema_version, te.input_tokens, te.output_tokens, te.cost_usd::text AS cost_usd, te.requires_human_review, te.quality,
       te.is_current, te.created_at, te.reused_from_extraction_id::text AS reused_from_extraction_id,
       ${reviewCodes("te")} AS review_codes
FROM tdr_extractions te JOIN contract_documents cd ON cd.id = te.contract_document_id
WHERE cd.id_contrato = $1::bigint ORDER BY te.created_at, te.id`,
  evaluations: `
SELECT e.id::text AS id, e.match_id::text AS match_id, e.event_type, e.payload, e.created_at, m.profile_id::text AS profile_id,
       cp.razon_social, m.score, m.verdict, m.user_state, ${pick("m.breakdown_json->'meta'", MATCH_META_KEYS)} AS meta
FROM match_events e JOIN matches m ON m.id = e.match_id LEFT JOIN company_profiles cp ON cp.id = m.profile_id
WHERE m.id_contrato = $1::bigint ORDER BY e.created_at, e.id`,
  manual: `
SELECT r.id::text AS id, r.profile_id::text AS profile_id, cp.razon_social, r.mode, r.status, r.reason, r.lima_day::text AS lima_day,
       r.created_at, r.updated_at
FROM manual_grading_requests r LEFT JOIN company_profiles cp ON cp.id = r.profile_id
WHERE r.source_key = 'seace_prod6' AND r.source_id = $1::bigint ORDER BY r.created_at`,
  // Solo fallas y omisiones: cada paso correcto de cada sondeo llena la tabla sin aportar nada.
  pipeline: `
SELECT pe.id::text AS id, pe.job_id::text AS job_id, pe.stage, pe.status, pe.duration_ms, pe.error, pe.created_at
FROM pipeline_events pe WHERE pe.id_contrato = $1::bigint AND pe.status IN ('failed', 'skipped')
ORDER BY pe.created_at DESC LIMIT 50`,
} as const;

export const MAJOR_QUERIES = {
  discovery: `
SELECT p.id_convocatoria::text AS id_convocatoria, p.id_procedimiento::text AS id_procedimiento, p.nomenclature, p.buyer_name,
       p.description, p.object_label, p.published_at, p.reference_amount::text AS reference_amount, p.currency, p.first_seen_at,
       p.last_seen_at, d.detail_fetched_at, d.proposals_closes_at, d.registration_closes_at, d.document_inventory_total,
       d.document_inventory_complete
FROM prod2_discoveries p LEFT JOIN prod2_details d ON d.id_convocatoria = p.id_convocatoria
WHERE p.id_convocatoria = $1::bigint`,
  documents: `
SELECT doc.codigo_alfresco::text AS codigo_alfresco, doc.name, doc.document_type, doc.stage_name, doc.published_at, doc.is_listed,
       doc.first_seen_at, doc.last_seen_at
FROM prod2_documents doc WHERE doc.id_convocatoria = $1::bigint ORDER BY doc.first_seen_at, doc.name LIMIT 80`,
  downloads: `
SELECT dl.codigo_alfresco::text AS codigo_alfresco, dl.sha256_original, dl.bytes::text AS bytes, dl.content_type, dl.format,
       dl.downloaded_at, doc.name
FROM prod2_document_downloads dl
LEFT JOIN prod2_documents doc ON doc.id_convocatoria = dl.id_convocatoria AND doc.codigo_alfresco = dl.codigo_alfresco
WHERE dl.id_convocatoria = $1::bigint ORDER BY dl.downloaded_at LIMIT 80`,
  evaluations: `
SELECT ev.id::text AS id, ev.kind, ev.status, ev.policy_version, ev.model, ev.profile_id::text AS profile_id, cp.razon_social,
       ev.created_at, ev.claimed_at, ev.completed_at, ev.updated_at, ev.input_tokens, ev.output_tokens, ev.cost_usd::text AS cost_usd,
       ${EVALUATION_ORIGIN} AS origin, ${pick("ev.result_json", PROD2_RESULT_KEYS)} AS result_json
FROM prod2_evaluations ev LEFT JOIN company_profiles cp ON cp.id = ev.profile_id
WHERE ev.id_convocatoria = $1::bigint ORDER BY ev.created_at, ev.id`,
  extractions: `
SELECT x.id::text AS id, x.evaluation_id::text AS evaluation_id, x.codigo_alfresco::text AS codigo_alfresco, x.model,
       x.prompt_version, x.schema_version, x.input_tokens, x.output_tokens, x.cost_usd::text AS cost_usd, x.requires_human_review,
       x.quality, x.facet_count, x.is_current, x.created_at, doc.name AS filename, ${reviewCodes("x")} AS review_codes
FROM prod2_document_extractions x
LEFT JOIN prod2_documents doc ON doc.id_convocatoria = x.id_convocatoria AND doc.codigo_alfresco = x.codigo_alfresco
WHERE x.id_convocatoria = $1::bigint ORDER BY x.created_at, x.id`,
  jevCache: `
SELECT c.cache_key, c.model, c.policy_version, c.status, c.completed_at, c.created_at, c.reused_evaluation_id::text AS reused_evaluation_id,
       c.blocked_reason
FROM prod2_jev_decision_cache c WHERE c.convocation_id = $1::bigint ORDER BY c.created_at`,
  manual: `
SELECT r.id::text AS id, r.profile_id::text AS profile_id, cp.razon_social, r.mode, r.status, r.reason, r.lima_day::text AS lima_day,
       r.created_at, r.updated_at
FROM manual_grading_requests r LEFT JOIN company_profiles cp ON cp.id = r.profile_id
WHERE r.source_key = 'seace_prod2' AND r.source_id = $1::bigint ORDER BY r.created_at`,
} as const;
