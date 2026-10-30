import type { OpsRowKind, OpsSource } from "./types.ts";

/**
 * Catálogo de pasos del embudo. Cada paso es UNA consulta de «eventos» (una fila por hecho) y de ella
 * salen el conteo por día, la lista de elementos y las fallas. Así el número de la tabla y la lista a la
 * que enlaza usan exactamente el mismo predicado.
 *
 * Todas las consultas arrancan con `WITH ${PARAMS_CTE}` y reciben los mismos cuatro parámetros:
 *   $1 = día inicial (Lima), $2 = día final (Lima, inclusive), $3 = perfil de empresa o NULL, $4 = límite.
 * Solo SELECT. El filtro de tiempo compara la columna cruda contra límites calculados una sola vez
 * (sargable si algún día existe un índice); el día se agrupa con `(col AT TIME ZONE 'America/Lima')::date`.
 */

/** Columnas que devuelve toda consulta de eventos, en este orden. */
export const EVENT_COLUMNS = ["item_id", "ts", "day", "profile_id", "score", "state", "reason", "cost", "ref", "title"] as const;

export const PARAMS_CTE = `p AS (
  SELECT (($1::date)::timestamp AT TIME ZONE 'America/Lima') AS ts_from,
         ((($2::date) + 1)::timestamp AT TIME ZONE 'America/Lima') AS ts_to,
         $1::date AS d_from, $2::date AS d_to, $3::uuid AS profile_id, $4::int AS row_limit
)`;

const FROM_TS = "(SELECT ts_from FROM p)";
const TO_TS = "(SELECT ts_to FROM p)";
const PROFILE = "(SELECT profile_id FROM p)";

/** Condición de ventana sobre una columna timestamptz. */
export const inWindow = (column: string) => `${column} >= ${FROM_TS} AND ${column} < ${TO_TS}`;
/** Día de Lima de una columna timestamptz. */
export const limaDayOf = (column: string) => `(${column} AT TIME ZONE 'America/Lima')`;
const forCompany = (column: string) => `(${PROFILE} IS NULL OR ${column} = ${PROFILE})`;
const safeNumber = (expression: string) =>
  `(CASE WHEN (${expression}) ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (${expression})::numeric END)`;
const safeTimestamp = (expression: string) =>
  `(CASE WHEN (${expression}) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' THEN (${expression})::timestamptz END)`;

/** Primera línea del error, hashes largos como <id>, números como N, acotada: agrupa errores iguales. */
export const normalizedError = (column: string) =>
  `left(regexp_replace(regexp_replace(split_part(${column}, chr(10), 1), '[0-9a-fA-F]{12,}', '<id>', 'g'), '[0-9]+', 'N', 'g'), 180)`;

type EventParts = {
  item?: string;
  ts: string;
  day?: string;
  profile?: string;
  score?: string;
  state?: string;
  reason?: string;
  cost?: string;
  ref?: string;
  title?: string;
  from: string;
  where: string;
};

function event(parts: EventParts): string {
  const column = (expression: string | undefined, type: string) => `(${expression ?? "NULL"})::${type}`;
  return `SELECT ${column(parts.item, "bigint")} AS item_id,
       ${column(parts.ts, "timestamptz")} AS ts,
       ${column(parts.day ?? limaDayOf(parts.ts), "date")} AS day,
       ${column(parts.profile, "uuid")} AS profile_id,
       ${column(parts.score, "numeric")} AS score,
       ${column(parts.state, "text")} AS state,
       ${column(parts.reason, "text")} AS reason,
       ${column(parts.cost, "numeric")} AS cost,
       ${column(parts.ref, "text")} AS ref,
       ${column(parts.title, "text")} AS title
FROM ${parts.from}
WHERE ${parts.where}`;
}

const union = (...selects: string[]) => selects.join("\nUNION ALL\n");

/** Códigos de las razones de revisión de una lectura (summary_json.review_reasons), separados por coma. */
export const reviewCodes = (alias: string) => `(
  SELECT string_agg(code, ', ' ORDER BY code) FROM (
    SELECT DISTINCT COALESCE(r->>'code', CASE WHEN jsonb_typeof(r) = 'string' THEN r #>> '{}' END) AS code
    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(${alias}.summary_json->'review_reasons') = 'array'
                                   THEN ${alias}.summary_json->'review_reasons' ELSE '[]'::jsonb END) r
  ) codes WHERE code IS NOT NULL)`;

/** Una fila por razón: separa «a, b» en dos eventos para contar cada razón por separado. */
function splitReasons(events: string): string {
  return `SELECT e.item_id, e.ts, e.day, e.profile_id, e.score, e.state,
       COALESCE(NULLIF(rc.code, ''), 'sin código registrado') AS reason, e.cost, e.ref, e.title
FROM (${events}) e
LEFT JOIN LATERAL unnest(string_to_array(e.reason, ', ')) AS rc(code) ON true`;
}

export type StepTone = "neutral" | "good" | "bad";

export type StepDef = {
  key: string;
  source: OpsSource;
  /** Encabezado de columna. */
  label: string;
  /** Nombre completo, para títulos de listas y fallas. */
  title: string;
  hint: string;
  group: string;
  /** Es por licitación y no por empresa: ignora el filtro de empresa. */
  shared: boolean;
  tone: StepTone;
  /** Qué representa cada fila. */
  rowKind: OpsRowKind;
  /** Aparece como columna de la tabla diaria. */
  column: boolean;
  /** Aparece en «Fallas». */
  failure: boolean;
  events: string;
  /** Variante para «Fallas» cuando una fila debe contarse por cada razón. */
  failureEvents?: string;
};

// ---------------------------------------------------------------------------------------------
// Menores (PROD6)
// ---------------------------------------------------------------------------------------------

const DECISIONS = "prod6_profile_decisions d";
const JEV_DECIDED = "d.decided_by = 'jev' AND d.score IS NOT NULL";

function jevDecision(decision: "accepted" | "rejected") {
  return event({
    item: "d.id_contrato",
    ts: "d.updated_at",
    profile: "d.profile_id",
    score: "d.score",
    state: "d.decision",
    reason: "NULLIF(concat_ws(' · ', d.domain, d.subcategory), '')",
    cost: "d.cost_usd",
    from: DECISIONS,
    where: `${JEV_DECIDED} AND d.decision = '${decision}' AND ${inWindow("d.updated_at")} AND ${forCompany("d.profile_id")}`,
  });
}

function minorReading(kind: "ok" | "review" | "failed") {
  const predicate = {
    ok: "te.quality <> 'failed' AND NOT te.requires_human_review",
    review: "te.quality <> 'failed' AND te.requires_human_review",
    failed: "te.quality = 'failed'",
  }[kind];
  const reason = {
    ok: "CASE WHEN te.reused_from_extraction_id IS NOT NULL THEN 'lectura reutilizada' END",
    review: reviewCodes("te"),
    failed: "'lectura marcada como fallida'",
  }[kind];
  return event({
    item: "cd.id_contrato",
    ts: "te.created_at",
    state: "te.quality",
    reason,
    cost: "te.cost_usd",
    ref: "cd.filename",
    from: "tdr_extractions te JOIN contract_documents cd ON cd.id = te.contract_document_id",
    where: `${inWindow("te.created_at")} AND ${predicate}`,
  });
}

const EVALUATION_EVENTS = `(e.event_type IN ('analysis', 'ai_analysis_completed') OR e.event_type LIKE '%\\_analysis')`;
/** Automática = la disparó el worker; todo lo demás (web, pedidos manuales, lotes de operador) cuenta como manual. */
const AUTOMATIC_EVENT = `(e.event_type = 'automatic_analysis' OR COALESCE(e.payload->>'source', '') IN ('automatic', 'worker'))`;

function minorEvaluation(automatic: boolean) {
  return event({
    item: "m.id_contrato",
    ts: "e.created_at",
    profile: "m.profile_id",
    state: "concat_ws(' · ', e.payload->>'verdict', e.payload->>'score')",
    reason: "COALESCE(e.payload->>'source', e.payload->>'execution', e.event_type)",
    // El costo exacto solo está en el evento web; el resto lo guarda matches.breakdown_json.meta de la
    // última evaluación, que sirve si se escribió en los 15 minutos del evento (si no, queda sin costo).
    cost: `COALESCE(${safeNumber("e.payload->>'cost_usd'")}, CASE WHEN abs(extract(epoch FROM e.created_at - ${safeTimestamp(
      "m.breakdown_json->'meta'->>'analyzed_at'",
    )})) <= 900 THEN ${safeNumber("m.breakdown_json->'meta'->>'cost_usd'")} END)`,
    from: "match_events e JOIN matches m ON m.id = e.match_id",
    where: `${EVALUATION_EVENTS} AND ${automatic ? "" : "NOT "}${AUTOMATIC_EVENT} AND ${inWindow("e.created_at")} AND ${forCompany("m.profile_id")}`,
  });
}

function manualRequests(sourceKey: "seace_prod6" | "seace_prod2", onlyFailed: boolean) {
  return event({
    item: "r.source_id",
    ts: "r.created_at",
    day: "r.lima_day",
    profile: "r.profile_id",
    state: "r.status",
    reason: onlyFailed ? "COALESCE(r.reason, 'sin motivo registrado')" : "r.reason",
    ref: "r.mode",
    from: "manual_grading_requests r",
    where: `r.source_key = '${sourceKey}' AND r.lima_day BETWEEN (SELECT d_from FROM p) AND (SELECT d_to FROM p) AND ${forCompany("r.profile_id")}${
      onlyFailed ? " AND r.status = 'failed'" : ""
    }`,
  });
}

/** payload->>'id_contrato' solo si es un entero (los trabajos globales, como los sondeos, no tienen contrato). */
const JOB_CONTRACT = `CASE WHEN j.payload->>'id_contrato' ~ '^[0-9]{1,18}$' THEN (j.payload->>'id_contrato')::bigint END`;

const failedJobs = event({
  item: JOB_CONTRACT,
  // Muertos: el día en que terminaron. Pendientes con error (reintento o retenidos para revisión): el día en
  // que se crearon, porque la fecha del último fallo no se guarda.
  ts: "CASE WHEN j.status IN ('dead', 'failed') THEN COALESCE(j.finished_at, j.created_at) ELSE j.created_at END",
  state: "j.job_type || ' · ' || j.status || CASE WHEN j.run_after = 'infinity'::timestamptz THEN ' (retenido)' ELSE '' END",
  reason: `j.job_type || ' · ' || COALESCE(NULLIF(${normalizedError("j.last_error")}, ''), 'sin mensaje de error')`,
  ref: "'#' || j.id::text",
  from: "worker_jobs j",
  where: `((j.status IN ('dead', 'failed') AND j.finished_at >= ${FROM_TS} AND j.finished_at < ${TO_TS})
    OR (j.status = 'pending' AND j.last_error IS NOT NULL AND ${inWindow("j.created_at")}))`,
});

const jevPending = (onlyWithError: boolean) =>
  event({
    item: "d.id_contrato",
    ts: onlyWithError ? "d.updated_at" : "d.created_at",
    profile: "d.profile_id",
    state: "'pendiente · ' || d.attempts::text || ' intentos'",
    reason: `NULLIF(${normalizedError("d.last_error")}, '')`,
    from: DECISIONS,
    where: `d.decided_by = 'jev' AND d.decision = 'pending' AND ${inWindow(onlyWithError ? "d.updated_at" : "d.created_at")} AND ${forCompany(
      "d.profile_id",
    )}${onlyWithError ? " AND d.last_error IS NOT NULL" : ""}`,
  });

const MINOR_STEPS: StepDef[] = [
  {
    key: "found",
    source: "menores",
    label: "Encontradas",
    title: "Contratos encontrados",
    hint: "Contratos nuevos en seace_contracts (first_seen_at): los admitidos al pipeline de lectura. Las descartadas y pendientes de JEV no se guardan ahí.",
    group: "Entrada",
    shared: true,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: event({
      item: "c.id_contrato",
      ts: "c.first_seen_at",
      state: "c.pipeline_state",
      from: "seace_contracts c",
      where: inWindow("c.first_seen_at"),
    }),
  },
  {
    key: "prefilter_rejected",
    source: "menores",
    label: "Descartadas",
    title: "Descartadas por el filtro previo",
    hint: "prod6_profile_decisions rechazadas sin puntaje (filtro previo duro, sin pasar por JEV). Una fila por empresa y contrato.",
    group: "Entrada",
    shared: false,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: event({
      item: "d.id_contrato",
      ts: "d.updated_at",
      profile: "d.profile_id",
      state: "d.decided_by",
      reason: "NULLIF(array_to_string(d.prefilter_reasons, ', '), '')",
      from: DECISIONS,
      where: `d.decision = 'rejected' AND d.score IS NULL AND ${inWindow("d.updated_at")} AND ${forCompany("d.profile_id")}`,
    }),
  },
  {
    key: "jev_accepted",
    source: "menores",
    label: "Aceptadas",
    title: "JEV aceptadas (≥ 60)",
    hint: "Decisiones JEV aceptadas (puntaje ≥ 60), por el día de updated_at. Una fila por empresa y contrato; sin las reemplazadas.",
    group: "JEV",
    shared: false,
    tone: "good",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: jevDecision("accepted"),
  },
  {
    key: "jev_rejected",
    source: "menores",
    label: "Rechazadas",
    title: "JEV rechazadas (< 60)",
    hint: "Decisiones JEV rechazadas (puntaje < 60), por el día de updated_at. Una fila por empresa y contrato.",
    group: "JEV",
    shared: false,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: jevDecision("rejected"),
  },
  {
    key: "jev_pending",
    source: "menores",
    label: "Pendientes",
    title: "JEV pendientes",
    hint: "Decisiones que siguen pendientes de JEV, por el día en que entraron (created_at). El total actual va arriba, en el resumen.",
    group: "JEV",
    shared: false,
    tone: "bad",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: jevPending(false),
  },
  {
    key: "jev_errors",
    source: "menores",
    label: "JEV con error",
    title: "JEV pendientes con error",
    hint: "Decisiones pendientes cuyo último intento de JEV falló (last_error).",
    group: "JEV",
    shared: false,
    tone: "bad",
    rowKind: "licitacion",
    column: false,
    failure: true,
    events: jevPending(true),
  },
  {
    key: "read_ok",
    source: "menores",
    label: "Ok",
    title: "Lecturas correctas",
    hint: "Lecturas (tdr_extractions) creadas ese día, no fallidas y sin revisión humana pendiente. Una fila por documento leído.",
    group: "Lectura",
    shared: true,
    tone: "good",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: minorReading("ok"),
  },
  {
    key: "read_review",
    source: "menores",
    label: "En revisión",
    title: "Lecturas en revisión",
    hint: "Lecturas con requires_human_review. El motivo son los códigos de summary_json.review_reasons.",
    group: "Lectura",
    shared: true,
    tone: "bad",
    rowKind: "licitacion",
    column: true,
    failure: true,
    events: minorReading("review"),
    failureEvents: splitReasons(minorReading("review")),
  },
  {
    key: "read_failed",
    source: "menores",
    label: "Fallidas",
    title: "Lecturas fallidas",
    hint: "Lecturas con quality = failed.",
    group: "Lectura",
    shared: true,
    tone: "bad",
    rowKind: "licitacion",
    column: true,
    failure: true,
    events: minorReading("failed"),
  },
  {
    key: "eval_auto",
    source: "menores",
    label: "Auto",
    title: "Evaluaciones automáticas",
    hint: "Eventos de análisis de match_events disparados por el worker (automatic_analysis o source worker).",
    group: "Evaluación",
    shared: false,
    tone: "good",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: minorEvaluation(true),
  },
  {
    key: "eval_manual",
    source: "menores",
    label: "Manual",
    title: "Evaluaciones manuales",
    hint: "Análisis que no disparó el worker automático: «Calificar» de la web, pedidos en cola y lotes de operador.",
    group: "Evaluación",
    shared: false,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: minorEvaluation(false),
  },
  {
    key: "jobs_failed",
    source: "menores",
    label: "Trabajos fallidos",
    title: "Trabajos fallidos del worker",
    hint: "worker_jobs muertos (por finished_at) o pendientes con error de reintento o retenidos (por created_at).",
    group: "Operación",
    shared: true,
    tone: "bad",
    rowKind: "trabajo",
    column: true,
    failure: true,
    events: failedJobs,
  },
  {
    key: "manual_requests",
    source: "menores",
    label: "Calif. manuales",
    title: "Calificaciones manuales solicitadas",
    hint: "manual_grading_requests de seace_prod6 por lima_day, con su estado.",
    group: "Operación",
    shared: false,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: manualRequests("seace_prod6", false),
  },
  {
    key: "manual_failed",
    source: "menores",
    label: "Calif. manuales fallidas",
    title: "Calificaciones manuales fallidas",
    hint: "manual_grading_requests de seace_prod6 con status = failed.",
    group: "Operación",
    shared: false,
    tone: "bad",
    rowKind: "licitacion",
    column: false,
    failure: true,
    events: manualRequests("seace_prod6", true),
  },
];

// ---------------------------------------------------------------------------------------------
// Mayores (PROD2)
// ---------------------------------------------------------------------------------------------

const JEV_UNION = "ev.kind = 'jev_affinity' AND ev.status = 'done' AND ev.policy_version = 'prod2_commercial_union_v1'";
const AFFINITY = safeNumber("ev.result_json->>'affinity_index'");
export const EVALUATION_ORIGIN = "COALESCE(ev.result_json->>'evaluation_origin', ev.result_json->'breakdown'->'meta'->>'evaluation_origin')";
const AUTO_ORIGIN = "'prod2_jev_domain_v1'";

function majorJev(condition: string) {
  return event({
    item: "ev.id_convocatoria",
    ts: "ev.completed_at",
    score: AFFINITY,
    state: "ev.result_json->>'domain'",
    cost: "ev.cost_usd",
    ref: "ev.model",
    from: "prod2_evaluations ev",
    where: `${JEV_UNION} AND ${inWindow("ev.completed_at")} AND ${AFFINITY} ${condition}`,
  });
}

function majorExtraction(kind: "ok" | "review" | "failed") {
  const predicate = {
    ok: "x.quality <> 'failed' AND NOT x.requires_human_review",
    review: "x.quality <> 'failed' AND x.requires_human_review",
    failed: "x.quality = 'failed'",
  }[kind];
  return event({
    item: "x.id_convocatoria",
    ts: "x.created_at",
    state: "x.quality",
    reason: kind === "review" ? reviewCodes("x") : kind === "failed" ? "'lectura marcada como fallida'" : undefined,
    cost: "x.cost_usd",
    ref: "x.model",
    from: "prod2_document_extractions x",
    where: `${inWindow("x.created_at")} AND ${predicate}`,
  });
}

const FAILED_EXTRACTION_EVALUATION = event({
  item: "ev.id_convocatoria",
  ts: "ev.updated_at",
  state: "ev.status",
  reason: "COALESCE(ev.result_json->>'failure_reason', ev.result_json->>'reason', ev.result_json->>'error_type', 'sin motivo registrado')",
  cost: "ev.cost_usd",
  ref: "ev.model",
  from: "prod2_evaluations ev",
  where: `ev.kind = 'gemini_extraction' AND ev.status = 'failed' AND ${inWindow("ev.updated_at")}`,
});

const UNCERTAIN_EXTRACTION = event({
  item: "ev.id_convocatoria",
  ts: "ev.updated_at",
  state: "ev.status",
  reason: "COALESCE(ev.result_json->>'reason', 'sin resultado registrado')",
  cost: "ev.cost_usd",
  ref: "ev.model",
  from: "prod2_evaluations ev",
  where: `ev.kind = 'gemini_extraction' AND ev.status IN ('uncertain', 'pending', 'running') AND ${inWindow("ev.updated_at")}`,
});

function majorEvaluation(condition: string, status: "done" | "problem") {
  const done = status === "done";
  return event({
    item: "ev.id_convocatoria",
    ts: done ? "ev.completed_at" : "ev.updated_at",
    profile: "ev.profile_id",
    state: done
      ? "concat_ws(' · ', ev.result_json->>'verdict', ev.result_json->>'score')"
      : "ev.status",
    reason: done ? `COALESCE(${EVALUATION_ORIGIN}, ev.policy_version)` : "COALESCE(ev.result_json->>'reason', 'sin motivo registrado')",
    cost: "ev.cost_usd",
    ref: "ev.model",
    from: "prod2_evaluations ev",
    where: `ev.kind = 'gemini_match' AND ${condition} AND ${inWindow(done ? "ev.completed_at" : "ev.updated_at")} AND ${forCompany("ev.profile_id")}`,
  });
}

const searchLabel = "concat_ws(' · ', t.query_manifest->>'keyword', t.query_manifest->>'object_type', t.query_manifest->>'year')";

const MAJOR_STEPS: StepDef[] = [
  {
    key: "searches_ok",
    source: "mayores",
    label: "Completas",
    title: "Búsquedas completas",
    hint: "prod2_keyword_tasks con status complete, por el día de last_completed_at. Una fila por palabra clave × objeto × año.",
    group: "Búsquedas",
    shared: true,
    tone: "good",
    rowKind: "busqueda",
    column: true,
    failure: false,
    events: event({
      ts: "t.last_completed_at",
      state: "t.status",
      reason: "CASE WHEN t.attempts > 1 THEN 'reintentada (' || t.attempts::text || ' intentos)' END",
      ref: searchLabel,
      title: "t.sweep_id",
      from: "prod2_keyword_tasks t",
      where: `t.status = 'complete' AND ${inWindow("t.last_completed_at")}`,
    }),
  },
  {
    key: "searches_incomplete",
    source: "mayores",
    label: "Incompletas",
    title: "Búsquedas incompletas",
    hint: "prod2_keyword_tasks parciales o bloqueadas, por el día de updated_at. El motivo es el primer código de error.",
    group: "Búsquedas",
    shared: true,
    tone: "bad",
    rowKind: "busqueda",
    column: true,
    failure: true,
    events: event({
      ts: "t.updated_at",
      state: "t.status",
      reason: "t.status || ' · ' || COALESCE(t.result_json->'error_codes'->>0, 'sin código de error')",
      ref: searchLabel,
      title: "t.sweep_id",
      from: "prod2_keyword_tasks t",
      where: `t.status IN ('partial', 'blocked') AND ${inWindow("t.updated_at")}`,
    }),
  },
  {
    key: "found",
    source: "mayores",
    label: "Nuevas",
    title: "Candidatas nuevas",
    hint: "prod2_discoveries por first_seen_at: convocatorias vistas por primera vez.",
    group: "Candidatas",
    shared: true,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: event({
      item: "p.id_convocatoria",
      ts: "p.first_seen_at",
      state: "p.object_label",
      from: "prod2_discoveries p",
      where: inWindow("p.first_seen_at"),
    }),
  },
  {
    key: "jev_ge85",
    source: "mayores",
    label: "≥ 85",
    title: "JEV ≥ 85 (se leen)",
    hint: "prod2_evaluations jev_affinity (política prod2_commercial_union_v1), por completed_at, con affinity_index ≥ 85: se leen.",
    group: "JEV",
    shared: true,
    tone: "good",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: majorJev(">= 85"),
  },
  {
    key: "jev_60_84",
    source: "mayores",
    label: "60–84",
    title: "JEV 60–84 (por calificar)",
    hint: "affinity_index entre 60 y 84: quedan por calificar.",
    group: "JEV",
    shared: true,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: majorJev(">= 60 AND " + AFFINITY + " < 85"),
  },
  {
    key: "jev_lt60",
    source: "mayores",
    label: "< 60",
    title: "JEV < 60",
    hint: "affinity_index menor que 60: descartadas.",
    group: "JEV",
    shared: true,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: majorJev("< 60"),
  },
  {
    key: "read_ok",
    source: "mayores",
    label: "Ok",
    title: "Lecturas correctas",
    hint: "prod2_document_extractions creadas ese día, no fallidas y sin revisión humana pendiente.",
    group: "Lectura",
    shared: true,
    tone: "good",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: majorExtraction("ok"),
  },
  {
    key: "read_review",
    source: "mayores",
    label: "En revisión",
    title: "Lecturas en revisión",
    hint: "prod2_document_extractions con requires_human_review (la mayoría de PROD2 sale como provisional).",
    group: "Lectura",
    shared: true,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: majorExtraction("review"),
  },
  {
    key: "read_failed",
    source: "mayores",
    label: "Fallidas",
    title: "Lecturas fallidas",
    hint: "Reservas gemini_extraction en estado failed (con su motivo) más extracciones con quality = failed.",
    group: "Lectura",
    shared: true,
    tone: "bad",
    rowKind: "licitacion",
    column: true,
    failure: true,
    events: union(FAILED_EXTRACTION_EVALUATION, majorExtraction("failed")),
  },
  {
    key: "read_uncertain",
    source: "mayores",
    label: "Inciertas",
    title: "Lecturas inciertas o en curso",
    hint: "Reservas gemini_extraction en uncertain, pending o running, por updated_at: un pago o una respuesta sin confirmar.",
    group: "Lectura",
    shared: true,
    tone: "bad",
    rowKind: "licitacion",
    column: true,
    failure: true,
    events: UNCERTAIN_EXTRACTION,
  },
  {
    key: "eval_auto",
    source: "mayores",
    label: "Auto",
    title: "Evaluaciones automáticas",
    hint: "gemini_match terminadas con evaluation_origin prod2_jev_domain_v1.",
    group: "Evaluación",
    shared: false,
    tone: "good",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: majorEvaluation(`ev.status = 'done' AND ${EVALUATION_ORIGIN} = ${AUTO_ORIGIN}`, "done"),
  },
  {
    key: "eval_manual",
    source: "mayores",
    label: "Manual",
    title: "Evaluaciones manuales",
    hint: "gemini_match terminadas con otro origen: pedidos de «Calificar» (prod2_manual_request_v1) y lotes de operador.",
    group: "Evaluación",
    shared: false,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: majorEvaluation(`ev.status = 'done' AND ${EVALUATION_ORIGIN} IS DISTINCT FROM ${AUTO_ORIGIN}`, "done"),
  },
  {
    key: "eval_failed",
    source: "mayores",
    label: "Fallidas",
    title: "Evaluaciones fallidas o inciertas",
    hint: "gemini_match en failed o uncertain, por updated_at.",
    group: "Evaluación",
    shared: false,
    tone: "bad",
    rowKind: "licitacion",
    column: true,
    failure: true,
    events: majorEvaluation("ev.status IN ('failed', 'uncertain')", "problem"),
  },
  {
    key: "manual_requests",
    source: "mayores",
    label: "Calif. manuales",
    title: "Calificaciones manuales solicitadas",
    hint: "manual_grading_requests de seace_prod2 por lima_day, con su estado.",
    group: "Operación",
    shared: false,
    tone: "neutral",
    rowKind: "licitacion",
    column: true,
    failure: false,
    events: manualRequests("seace_prod2", false),
  },
  {
    key: "manual_failed",
    source: "mayores",
    label: "Calif. manuales fallidas",
    title: "Calificaciones manuales fallidas",
    hint: "manual_grading_requests de seace_prod2 con status = failed.",
    group: "Operación",
    shared: false,
    tone: "bad",
    rowKind: "licitacion",
    column: false,
    failure: true,
    events: manualRequests("seace_prod2", true),
  },
  {
    key: "embeddings_ok",
    source: "mayores",
    label: "Embeddings",
    title: "Embeddings generados",
    hint: "prod2_embedding_attempts en done o partial_known, por completed_at.",
    group: "Operación",
    shared: true,
    tone: "neutral",
    rowKind: "embedding",
    column: true,
    failure: false,
    events: event({
      ts: "a.completed_at",
      state: "a.status",
      cost: "a.cost_usd",
      ref: "a.model",
      title: "a.attempt_id::text",
      from: "prod2_embedding_attempts a",
      where: `a.status IN ('done', 'partial_known') AND ${inWindow("a.completed_at")}`,
    }),
  },
  {
    key: "embeddings_failed",
    source: "mayores",
    label: "Embeddings fallidos",
    title: "Embeddings fallidos o inciertos",
    hint: "prod2_embedding_attempts en failed_pre_send o uncertain, por claimed_at.",
    group: "Operación",
    shared: true,
    tone: "bad",
    rowKind: "embedding",
    column: false,
    failure: true,
    events: event({
      ts: "a.claimed_at",
      state: "a.status",
      reason: "a.status || ' · ' || COALESCE(a.error_code, 'sin código de error')",
      cost: "a.cost_usd",
      ref: "a.model",
      title: "a.attempt_id::text",
      from: "prod2_embedding_attempts a",
      where: `a.status IN ('failed_pre_send', 'uncertain') AND ${inWindow("a.claimed_at")}`,
    }),
  },
];

export const STEPS: readonly StepDef[] = [...MINOR_STEPS, ...MAJOR_STEPS];

const STEP_KEY = /^[a-z0-9_]+$/;
for (const step of STEPS) {
  if (!STEP_KEY.test(step.key)) throw new Error(`Invalid ops step key: ${step.key}`);
}

export function stepsFor(source: OpsSource): StepDef[] {
  return STEPS.filter((step) => step.source === source);
}

export function findStep(source: OpsSource, key: string): StepDef | null {
  return STEPS.find((step) => step.source === source && step.key === key) ?? null;
}

export function columnSteps(source: OpsSource): StepDef[] {
  return stepsFor(source).filter((step) => step.column);
}

export function failureSteps(source: OpsSource): StepDef[] {
  return stepsFor(source).filter((step) => step.failure);
}

/** Pasos cuyo costo se suma en cada rubro de «Costos por día». */
export const COST_BUCKETS: Record<string, { source: OpsSource; stage: "jev" | "lectura" | "evaluacion" | "embeddings"; steps: string[] }> = {
  menores_jev: { source: "menores", stage: "jev", steps: ["jev_accepted", "jev_rejected"] },
  menores_lectura: { source: "menores", stage: "lectura", steps: ["read_ok", "read_review", "read_failed"] },
  menores_evaluacion: { source: "menores", stage: "evaluacion", steps: ["eval_auto", "eval_manual"] },
  mayores_jev: { source: "mayores", stage: "jev", steps: ["jev_ge85", "jev_60_84", "jev_lt60"] },
  mayores_lectura: { source: "mayores", stage: "lectura", steps: ["read_ok", "read_review", "read_failed"] },
  mayores_evaluacion: { source: "mayores", stage: "evaluacion", steps: ["eval_auto", "eval_manual", "eval_failed"] },
  mayores_embeddings: { source: "mayores", stage: "embeddings", steps: ["embeddings_ok", "embeddings_failed"] },
};

// ---------------------------------------------------------------------------------------------
// Constructores de consultas completas
// ---------------------------------------------------------------------------------------------

/** Parámetros de toda consulta del panel: [desde, hasta, perfil|null, límite]. */
export type OpsParams = [from: string, to: string, profileId: string | null, limit: number];

/** Conteo de eventos por paso y día de Lima. */
export function countsSql(steps: readonly StepDef[]): string {
  const branches = steps.map(
    (step) => `SELECT '${step.key}'::text AS step, e.day::text AS day, count(*)::int AS n
FROM (${step.events}) e GROUP BY e.day`,
  );
  return `WITH ${PARAMS_CTE}\n${branches.join("\nUNION ALL\n")}`;
}

/** Costo (USD) y conteo por rubro y día; `priced` cuenta los eventos con costo conocido. */
export function costsSql(): string {
  const branches = Object.entries(COST_BUCKETS).flatMap(([bucket, definition]) =>
    definition.steps.map((key) => {
      const step = findStep(definition.source, key);
      if (!step) throw new Error(`Unknown cost step: ${definition.source}/${key}`);
      return `SELECT '${bucket}'::text AS bucket, e.day, e.cost FROM (${step.events}) e`;
    }),
  );
  return `WITH ${PARAMS_CTE}
SELECT c.bucket, c.day::text AS day, COALESCE(sum(c.cost), 0)::text AS usd, count(*)::int AS n, count(c.cost)::int AS priced
FROM (
${branches.join("\nUNION ALL\n")}
) c GROUP BY c.bucket, c.day`;
}

/** Filas de un paso en una ventana (normalmente un solo día), las más nuevas primero. */
export function itemsSql(step: StepDef): string {
  return `WITH ${PARAMS_CTE}
SELECT e.item_id::text AS item_id, e.ts, e.day::text AS day, e.profile_id::text AS profile_id, e.score::text AS score,
       e.state, e.reason, e.cost::text AS cost, e.ref, e.title, count(*) OVER ()::int AS total
FROM (${step.events}) e
ORDER BY e.ts DESC, e.item_id DESC NULLS LAST
LIMIT (SELECT row_limit FROM p)`;
}

/** Fallas de un día: por paso y razón, con el total y las `row_limit` filas más nuevas de cada grupo. */
export function failuresSql(steps: readonly StepDef[]): string {
  const branches = steps.map(
    (step) => `SELECT '${step.key}'::text AS step, x.item_id, x.ts, x.profile_id, x.state, x.ref, x.title,
       COALESCE(NULLIF(x.reason, ''), 'sin motivo registrado') AS reason
FROM (${step.failureEvents ?? step.events}) x`,
  );
  return `WITH ${PARAMS_CTE},
f AS (
${branches.join("\nUNION ALL\n")}
), ranked AS (
  SELECT f.*, row_number() OVER (PARTITION BY f.step, f.reason ORDER BY f.ts DESC, f.item_id DESC NULLS LAST) AS rn,
         count(*) OVER (PARTITION BY f.step, f.reason)::int AS total
  FROM f
)
SELECT r.step, r.reason, r.total, r.item_id::text AS item_id, r.ts, r.profile_id::text AS profile_id, r.state, r.ref, r.title
FROM ranked r
WHERE r.rn <= (SELECT row_limit FROM p)
ORDER BY r.total DESC, r.step, r.reason, r.rn`;
}
