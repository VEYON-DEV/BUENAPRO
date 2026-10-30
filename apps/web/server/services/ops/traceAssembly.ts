import { redact, scrubText } from "./redact.ts";
import type { OpsSource } from "./types.ts";

/**
 * Armado puro de la «Traza» de una licitación: recibe las filas que devuelven las consultas de trace.ts
 * y produce una línea de tiempo cronológica. Sin acceso a base de datos, para poder probarlo.
 */

export type Raw = Record<string, unknown>;

export type TraceKind = "hallazgo" | "jev" | "trabajo" | "documento" | "lectura" | "evaluacion" | "manual" | "seguimiento" | "pipeline";
export type TraceOutcome = "ok" | "warn" | "error" | "pending" | "info";
export type TraceFact = { label: string; value: string; time?: boolean };

export type TraceEvent = {
  id: string;
  /** ISO 8601 (UTC); la interfaz lo muestra en hora de Lima. */
  ts: string;
  kind: TraceKind;
  outcome: TraceOutcome;
  title: string;
  company: string | null;
  summary: string | null;
  /** Motivo o error, ya saneado. */
  reason: string | null;
  costUsd: number | null;
  facts: TraceFact[];
  /** Registro original, saneado (sin secretos ni textos completos). */
  detail: unknown;
};

export type Trace = {
  source: OpsSource;
  id: string;
  header: {
    code: string;
    entity: string | null;
    description: string | null;
    facts: TraceFact[];
    notes: string[];
  };
  events: TraceEvent[];
  totals: { events: number; costUsd: number; byKind: Partial<Record<TraceKind, number>> };
};

export type MinorTraceRaw = {
  contract: Raw | null;
  decisions: Raw[];
  jobs: Raw[];
  documents: Raw[];
  readings: Raw[];
  evaluations: Raw[];
  manual: Raw[];
  pipeline: Raw[];
};

export type MajorTraceRaw = {
  discovery: Raw | null;
  documents: Raw[];
  downloads: Raw[];
  evaluations: Raw[];
  extractions: Raw[];
  jevCache: Raw[];
  manual: Raw[];
};

/** Orden de desempate cuando dos eventos tienen la misma marca de tiempo. */
const KIND_RANK: Record<TraceKind, number> = {
  hallazgo: 0,
  jev: 1,
  documento: 2,
  trabajo: 3,
  lectura: 4,
  evaluacion: 5,
  manual: 6,
  seguimiento: 7,
  pipeline: 8,
};

const MINOR_AUTO_MIN_DELAY_MS = 5_000;

// --- accesores tolerantes -----------------------------------------------------------------------

function text(row: Raw, key: string): string | null {
  const value = row[key];
  if (value == null) return null;
  const result = String(value).trim();
  return result === "" ? null : result;
}

export function toNumber(row: Raw, key: string): number | null {
  const value = row[key];
  if (value == null || value === "") return null;
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : null;
}

function bool(row: Raw, key: string): boolean {
  return row[key] === true || row[key] === "true" || row[key] === "t";
}

function when(row: Raw, key: string): string | null {
  const value = row[key];
  if (value == null) return null;
  const date = value instanceof Date ? value : new Date(String(value));
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function object(row: Raw, key: string): Raw {
  const value = row[key];
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Raw) : {};
}

function list(row: Raw, key: string): string[] {
  const value = row[key];
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  if (typeof value === "string" && value) return value.split(", ").filter(Boolean);
  return [];
}

function fact(label: string, value: string | number | null | undefined, time = false): TraceFact | null {
  if (value == null || value === "") return null;
  return { label, value: String(value), ...(time ? { time: true } : {}) };
}

function facts(...items: Array<TraceFact | null>): TraceFact[] {
  return items.filter((item): item is TraceFact => item !== null);
}

function scrub(value: string | null, max = 500): string | null {
  return value ? scrubText(value, max) : null;
}

export function formatBytes(bytes: number | null): string | null {
  if (bytes == null || bytes < 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function tokenCount(row: Raw): number | null {
  const input = toNumber(row, "input_tokens");
  const output = toNumber(row, "output_tokens");
  return input == null && output == null ? null : (input ?? 0) + (output ?? 0);
}

function companyOf(row: Raw, labels: ReadonlyMap<string, string>, key = "profile_id"): string | null {
  const id = text(row, key);
  if (!id) return null;
  return labels.get(id.toLowerCase()) ?? text(row, "razon_social") ?? `empresa ${id.slice(0, 8)}`;
}

function event(base: Omit<TraceEvent, "detail" | "facts" | "reason" | "summary" | "company" | "costUsd"> & Partial<TraceEvent>, detail: unknown): TraceEvent {
  return {
    company: null,
    summary: null,
    reason: null,
    costUsd: null,
    facts: [],
    ...base,
    detail: redact(detail),
  };
}

/** Orden cronológico estable: instante, luego etapa del flujo, luego id. */
export function sortEvents(events: TraceEvent[]): TraceEvent[] {
  return [...events].sort(
    (a, b) => a.ts.localeCompare(b.ts) || KIND_RANK[a.kind] - KIND_RANK[b.kind] || a.id.localeCompare(b.id),
  );
}

function totalsOf(events: TraceEvent[]): Trace["totals"] {
  const byKind: Partial<Record<TraceKind, number>> = {};
  let costUsd = 0;
  for (const item of events) {
    byKind[item.kind] = (byKind[item.kind] ?? 0) + 1;
    costUsd += item.costUsd ?? 0;
  }
  return { events: events.length, costUsd, byKind };
}

const FINAL_REQUEST_STATUS = new Set(["done", "failed"]);

function manualRequestEvents(rows: Raw[], labels: ReadonlyMap<string, string>, prefix: string): TraceEvent[] {
  return rows.flatMap((row) => {
    const status = text(row, "status") ?? "pending";
    const created = when(row, "created_at");
    const updated = when(row, "updated_at");
    if (!created) return [];
    const company = companyOf(row, labels);
    // Quién pidió la calificación es un dato personal que el panel no necesita.
    const { requested_by: _requestedBy, ...detail } = row;
    const reason = scrub(text(row, "reason"));
    const events = [
      event(
        {
          id: `${prefix}-manual-${text(row, "id")}`,
          ts: created,
          kind: "manual",
          outcome: status === "failed" ? "error" : status === "done" ? "ok" : "pending",
          title: "Calificación manual solicitada",
          company,
          summary: `Estado ${status} · modo ${text(row, "mode") ?? "—"}`,
          reason,
          facts: facts(fact("Día de Lima", text(row, "lima_day"))),
        },
        detail,
      ),
    ];
    if (FINAL_REQUEST_STATUS.has(status) && updated && Date.parse(updated) - Date.parse(created) >= MINOR_AUTO_MIN_DELAY_MS) {
      events.push(
        event(
          {
            id: `${prefix}-manual-end-${text(row, "id")}`,
            ts: updated,
            kind: "manual",
            outcome: status === "failed" ? "error" : "ok",
            title: status === "failed" ? "Calificación manual fallida" : "Calificación manual terminada",
            company,
            reason,
          },
          { id: row.id, status, updated_at: updated },
        ),
      );
    }
    return events;
  });
}

// --- menores ------------------------------------------------------------------------------------

const STATE_CHANGE_TITLES: Record<string, string> = {
  state_change: "Cambio de estado en seguimiento",
  application_started: "Postulación iniciada",
  application_updated: "Postulación actualizada",
  agent_changes_confirmed: "Cambios del copiloto confirmados",
};

function isAnalysisEvent(type: string): boolean {
  return type === "analysis" || type === "ai_analysis_completed" || type.endsWith("_analysis");
}

/** Automática = la disparó el worker; lo demás (web, pedidos, lotes de operador) es manual. */
export function isAutomaticAnalysis(type: string, source: string | null): boolean {
  return type === "automatic_analysis" || source === "automatic" || source === "worker";
}

/**
 * Costo de un evento de análisis: el del propio evento (web) o, si no lo trae, el de la última evaluación
 * guardada en matches.breakdown_json.meta cuando se escribió dentro de los 15 minutos del evento.
 */
export function evaluationCost(payload: Raw, meta: Raw, eventTs: string): number | null {
  const own = toNumber(payload, "cost_usd");
  if (own != null) return own;
  const analyzed = when(meta, "analyzed_at");
  const metaCost = toNumber(meta, "cost_usd");
  if (analyzed && metaCost != null && Math.abs(Date.parse(analyzed) - Date.parse(eventTs)) <= 900_000) return metaCost;
  return null;
}

export function assembleMinorTrace(id: string, raw: MinorTraceRaw, labels: ReadonlyMap<string, string>): Trace | null {
  const contract = raw.contract;
  const firstDecision = raw.decisions[0];
  if (!contract && !firstDecision) return null;

  const events: TraceEvent[] = [];
  const notes: string[] = [];

  const earliestDecision = raw.decisions.map((row) => when(row, "created_at")).filter((value): value is string => value !== null).sort()[0];
  if (earliestDecision) {
    events.push(
      event(
        {
          id: "sondeo",
          ts: earliestDecision,
          kind: "hallazgo",
          outcome: "info",
          title: "Vista por el sondeo de menores",
          summary: `${raw.decisions.length} decisión(es) por empresa registradas`,
        },
        { first_decision_at: earliestDecision, decisions: raw.decisions.length },
      ),
    );
  }

  if (contract) {
    const first = when(contract, "first_seen_at");
    if (first) {
      events.push(
        event(
          {
            id: "admitida",
            ts: first,
            kind: "hallazgo",
            outcome: "ok",
            title: "Admitida al pipeline de lectura",
            summary: `Estado del pipeline: ${text(contract, "pipeline_state") ?? "—"}`,
            facts: facts(fact("Visto por última vez", when(contract, "last_seen_at"), true)),
          },
          contract,
        ),
      );
    }
    const detailAt = when(contract, "detail_fetched_at");
    if (detailAt) {
      events.push(event({ id: "detalle", ts: detailAt, kind: "hallazgo", outcome: "info", title: "Detalle de SEACE descargado" }, { detail_fetched_at: detailAt }));
    }
  } else {
    notes.push("No está en seace_contracts: fue descartada por el filtro previo, rechazada por JEV o sigue pendiente de JEV.");
  }

  for (const row of raw.decisions) {
    const decision = text(row, "decision") ?? "pending";
    const decidedBy = text(row, "decided_by") ?? "jev";
    const score = toNumber(row, "score");
    const company = companyOf(row, labels);
    const created = when(row, "created_at");
    const updated = when(row, "updated_at") ?? created;
    const base = { company, costUsd: toNumber(row, "cost_usd") };
    const lastError = scrub(text(row, "last_error"));
    const model = text(row, "model");
    const detail = row;

    if (decision === "rejected" && score == null) {
      const reasons = list(row, "prefilter_reasons");
      if (updated) {
        events.push(
          event(
            {
              id: `decision-${text(row, "id")}`,
              ts: updated,
              kind: "jev",
              outcome: "info",
              title: "Filtro previo: descartada",
              ...base,
              reason: reasons.join(", ") || null,
              summary: `Política ${text(row, "policy_version") ?? "—"} · decidió ${decidedBy}`,
              facts: facts(fact("Líneas por palabra clave", list(row, "keyword_lines").join(", "))),
            },
            detail,
          ),
        );
      }
    } else if (decision === "pending") {
      if (created) {
        events.push(
          event(
            {
              id: `decision-${text(row, "id")}`,
              ts: created,
              kind: "jev",
              outcome: lastError ? "error" : "pending",
              title: "JEV pendiente",
              ...base,
              reason: lastError,
              summary: `Intentos: ${toNumber(row, "attempts") ?? 0}`,
              facts: facts(fact("Último intento", lastError ? when(row, "updated_at") : null, true)),
            },
            detail,
          ),
        );
      }
    } else if (decision === "superseded") {
      if (updated) {
        events.push(
          event(
            { id: `decision-${text(row, "id")}`, ts: updated, kind: "jev", outcome: "info", title: "Decisión reemplazada", ...base, summary: "Otra decisión más reciente la sustituyó (cambió la ficha o el perfil)" },
            detail,
          ),
        );
      }
    } else if (updated) {
      const accepted = decision === "accepted";
      events.push(
        event(
          {
            id: `decision-${text(row, "id")}`,
            ts: updated,
            kind: "jev",
            outcome: accepted ? "ok" : "info",
            title: accepted ? "JEV aceptó" : "JEV rechazó",
            ...base,
            summary: `Puntaje ${score ?? "—"} (umbral ${toNumber(row, "threshold") ?? "—"}) · intentos ${toNumber(row, "attempts") ?? 0}`,
            facts: facts(
              fact("Dominio", text(row, "domain")),
              fact("Subcategoría", text(row, "subcategory")),
              fact("Información", text(row, "information")),
              fact("Modelo", model),
              fact("Tokens", tokenCount(row)),
              fact("Entró a JEV", created && Date.parse(updated) - Date.parse(created) >= MINOR_AUTO_MIN_DELAY_MS ? created : null, true),
            ),
          },
          detail,
        ),
      );
    }
  }

  for (const row of raw.jobs) {
    const created = when(row, "created_at");
    if (!created) continue;
    const status = text(row, "status") ?? "pending";
    const held = bool(row, "held");
    const error = scrub(text(row, "last_error"));
    const runtime = object(row, "runtime_result");
    const outcomeText = text(runtime, "outcome");
    const outcome: TraceOutcome =
      status === "done" ? "ok" : status === "dead" || status === "failed" ? "error" : error || held ? "warn" : "pending";
    events.push(
      event(
        {
          id: `job-${text(row, "id")}`,
          ts: created,
          kind: "trabajo",
          outcome,
          title: `Trabajo ${text(row, "job_type") ?? "—"}`,
          company: companyOf(row, labels),
          summary: `Estado ${status}${held ? " (retenido para revisión manual)" : ""} · intento ${toNumber(row, "attempts") ?? 0}/${toNumber(row, "max_attempts") ?? "—"}${
            outcomeText ? ` · resultado ${outcomeText}${text(runtime, "reason") ? `/${text(runtime, "reason")}` : ""}` : ""
          }`,
          reason: error,
          facts: facts(
            fact("Cola", text(row, "queue_name")),
            fact("Tomado", when(row, "claimed_at"), true),
            fact("Terminó", when(row, "finished_at"), true),
          ),
        },
        row,
      ),
    );
  }
  const jobsTotal = raw.jobs.length ? (toNumber(raw.jobs[0], "total") ?? raw.jobs.length) : 0;
  if (jobsTotal > raw.jobs.length) {
    notes.push(`Se muestran ${raw.jobs.length} de ${jobsTotal} trabajos: primero los que no terminaron bien y luego los más recientes.`);
  }

  for (const row of raw.documents) {
    const created = when(row, "created_at");
    if (!created) continue;
    const size = formatBytes(toNumber(row, "size_original_bytes"));
    events.push(
      event(
        {
          id: `documento-${text(row, "id")}`,
          ts: created,
          kind: "documento",
          outcome: "ok",
          title: "Documento descargado",
          summary: `${text(row, "filename") ?? "sin nombre"}${size ? ` · ${size}` : ""}`,
          facts: facts(
            fact("Clase", text(row, "doc_class")),
            fact("Tipo", text(row, "mime")),
            fact("Capa de texto", row.has_text_layer == null ? null : bool(row, "has_text_layer") ? "sí" : "no"),
          ),
        },
        row,
      ),
    );
  }

  for (const row of raw.readings) {
    const created = when(row, "created_at");
    if (!created) continue;
    const failed = text(row, "quality") === "failed";
    const review = bool(row, "requires_human_review");
    const reused = text(row, "reused_from_extraction_id") != null;
    const codes = list(row, "review_codes");
    events.push(
      event(
        {
          id: `lectura-${text(row, "id")}`,
          ts: created,
          kind: "lectura",
          outcome: failed ? "error" : review ? "warn" : "ok",
          title: reused ? "Lectura reutilizada" : "Lectura del documento",
          summary: `${text(row, "filename") ?? "documento"} · ${failed ? "fallida" : review ? "en revisión humana" : "correcta"}${bool(row, "is_current") ? " · vigente" : ""}`,
          reason: codes.length ? codes.join(", ") : failed ? "lectura marcada como fallida" : null,
          costUsd: toNumber(row, "cost_usd"),
          facts: facts(
            fact("Modelo", text(row, "model")),
            fact("Prompt", text(row, "prompt_version")),
            fact("Calidad", text(row, "quality")),
            fact("Tokens", tokenCount(row)),
          ),
        },
        row,
      ),
    );
  }

  for (const row of raw.evaluations) {
    const created = when(row, "created_at");
    if (!created) continue;
    const type = text(row, "event_type") ?? "";
    const payload = object(row, "payload");
    const meta = object(row, "meta");
    const company = companyOf(row, labels);
    if (isAnalysisEvent(type)) {
      const source = text(payload, "source");
      const automatic = isAutomaticAnalysis(type, source);
      const origin = text(meta, "evaluation_origin") ?? text(payload, "execution") ?? source ?? type;
      events.push(
        event(
          {
            id: `evaluacion-${text(row, "id")}`,
            ts: created,
            kind: "evaluacion",
            outcome: "ok",
            title: automatic ? "Evaluación automática" : "Evaluación manual",
            company,
            summary: `Puntaje ${toNumber(payload, "score") ?? "—"} · veredicto ${text(payload, "verdict") ?? "—"} · origen ${origin}`,
            costUsd: evaluationCost(payload, meta, created),
            facts: facts(
              fact("Modelo", text(meta, "model") ?? text(payload, "model")),
              fact("Validado", meta.score_validated == null ? null : bool(meta, "score_validated") ? "sí" : "no"),
              fact("Lectura usada", text(meta, "extraction_id")),
            ),
          },
          row,
        ),
      );
    } else {
      events.push(
        event(
          {
            id: `seguimiento-${text(row, "id")}`,
            ts: created,
            kind: "seguimiento",
            outcome: "info",
            title: STATE_CHANGE_TITLES[type] ?? type,
            company,
            summary: text(row, "user_state") ? `Estado del seguimiento: ${text(row, "user_state")}` : null,
          },
          row,
        ),
      );
    }
  }

  events.push(...manualRequestEvents(raw.manual, labels, "menores"));

  for (const row of raw.pipeline) {
    const created = when(row, "created_at");
    if (!created) continue;
    const status = text(row, "status") ?? "—";
    events.push(
      event(
        {
          id: `pipeline-${text(row, "id")}`,
          ts: created,
          kind: "pipeline",
          outcome: status === "failed" ? "error" : "warn",
          title: `Etapa ${text(row, "stage") ?? "—"}: ${status}`,
          reason: scrub(text(row, "error")),
          facts: facts(fact("Duración (ms)", toNumber(row, "duration_ms")), fact("Trabajo", text(row, "job_id"))),
        },
        row,
      ),
    );
  }

  const headerRow = contract ?? {};
  const fallback = firstDecision ?? {};
  const fields = facts(
    fact("Pipeline", text(headerRow, "pipeline_state")),
    fact("Segmento CUBSO", text(headerRow, "cubso_segmento") ?? text(fallback, "cubso_segmento")),
    fact("Valor estimado", toNumber(headerRow, "valor_estimado")),
    fact("Publicada", when(headerRow, "fec_publica"), true),
    fact("Cierra", when(headerRow, "fec_fin_cotizacion") ?? when(fallback, "fec_fin_cotizacion"), true),
  );
  const sorted = sortEvents(events);
  return {
    source: "menores",
    id,
    header: {
      code: text(headerRow, "codigo") ?? text(fallback, "codigo") ?? `Contrato ${id}`,
      entity: text(headerRow, "entidad_nombre") ?? text(fallback, "entidad"),
      description: text(headerRow, "descripcion") ?? text(fallback, "descripcion"),
      facts: fields,
      notes,
    },
    events: sorted,
    totals: totalsOf(sorted),
  };
}

// --- mayores ------------------------------------------------------------------------------------

function jevOutcome(status: string, affinity: number | null): TraceOutcome {
  if (status === "failed") return "error";
  if (status === "uncertain") return "warn";
  if (status !== "done") return "pending";
  return affinity != null && affinity >= 60 ? "ok" : "info";
}

function statusOutcome(status: string): TraceOutcome {
  if (status === "done") return "ok";
  if (status === "failed") return "error";
  if (status === "uncertain") return "warn";
  return "pending";
}

export function assembleMajorTrace(id: string, raw: MajorTraceRaw, labels: ReadonlyMap<string, string>): Trace | null {
  const discovery = raw.discovery;
  if (!discovery) return null;
  const events: TraceEvent[] = [];
  const notes: string[] = [];

  const first = when(discovery, "first_seen_at");
  if (first) {
    events.push(
      event(
        {
          id: "candidata",
          ts: first,
          kind: "hallazgo",
          outcome: "ok",
          title: "Candidata nueva (prod2_discoveries)",
          summary: `${text(discovery, "object_label") ?? "—"} · publicada en SEACE el ${(when(discovery, "published_at") ?? "—").slice(0, 10)}`,
          facts: facts(fact("Vista por última vez", when(discovery, "last_seen_at"), true)),
        },
        discovery,
      ),
    );
  }
  const detailAt = when(discovery, "detail_fetched_at");
  if (detailAt) {
    events.push(
      event(
        {
          id: "detalle",
          ts: detailAt,
          kind: "hallazgo",
          outcome: "info",
          title: "Detalle de SEACE descargado",
          facts: facts(
            fact("Documentos en el inventario", toNumber(discovery, "document_inventory_total")),
            fact("Inventario completo", discovery.document_inventory_complete == null ? null : bool(discovery, "document_inventory_complete") ? "sí" : "no"),
          ),
        },
        { detail_fetched_at: detailAt },
      ),
    );
  }

  for (const row of raw.documents) {
    const seen = when(row, "first_seen_at");
    if (!seen) continue;
    events.push(
      event(
        {
          id: `documento-${text(row, "codigo_alfresco")}`,
          ts: seen,
          kind: "documento",
          outcome: row.is_listed === false ? "warn" : "info",
          title: "Documento listado",
          summary: `${text(row, "name") ?? "sin nombre"}`,
          facts: facts(fact("Tipo", text(row, "document_type")), fact("Etapa", text(row, "stage_name")), fact("Publicado", when(row, "published_at"), true)),
        },
        row,
      ),
    );
  }
  for (const row of raw.downloads) {
    const at = when(row, "downloaded_at");
    if (!at) continue;
    events.push(
      event(
        {
          id: `descarga-${text(row, "codigo_alfresco")}-${text(row, "sha256_original")?.slice(0, 12)}`,
          ts: at,
          kind: "documento",
          outcome: "ok",
          title: "Documento descargado",
          summary: `${text(row, "name") ?? text(row, "codigo_alfresco") ?? "documento"} · ${text(row, "format") ?? "—"} · ${formatBytes(toNumber(row, "bytes")) ?? "—"}`,
          facts: facts(fact("Tipo de contenido", text(row, "content_type"))),
        },
        { ...row, sha256_original: text(row, "sha256_original")?.slice(0, 16) },
      ),
    );
  }

  const extractionsByEvaluation = new Map<string, Raw>();
  for (const row of raw.extractions) {
    const evaluationId = text(row, "evaluation_id");
    if (evaluationId) extractionsByEvaluation.set(evaluationId, row);
  }
  const mergedEvaluations = new Set<string>();

  for (const row of raw.evaluations) {
    const kind = text(row, "kind") ?? "";
    const status = text(row, "status") ?? "pending";
    const evaluationId = text(row, "id") ?? "";
    // El JEV y la lectura de PROD2 son por licitación (compartidos); solo la evaluación es de una empresa.
    const company = kind === "gemini_match" ? companyOf(row, labels) : null;
    const result = object(row, "result_json");
    const completed = when(row, "completed_at");
    const created = when(row, "created_at");
    const ts = completed ?? when(row, "updated_at") ?? created;
    if (!ts) continue;
    const cost = toNumber(row, "cost_usd");
    const model = text(row, "model");

    if (kind === "jev_affinity") {
      const affinity = toNumber(result, "affinity_index");
      events.push(
        event(
          {
            id: `jev-${evaluationId}`,
            ts,
            kind: "jev",
            outcome: jevOutcome(status, affinity),
            title: `JEV de afinidad (${text(row, "policy_version") ?? "—"})`,
            company,
            summary: `Estado ${status} · afinidad ${affinity ?? "—"}${text(result, "domain") ? ` · dominio ${text(result, "domain")}` : ""}`,
            costUsd: cost,
            facts: facts(fact("Modelo", model), fact("Tokens", tokenCount(row)), fact("Caché", result.cache_hit == null ? null : bool(result, "cache_hit") ? "sí" : "no")),
          },
          row,
        ),
      );
    } else if (kind === "gemini_extraction") {
      const extraction = extractionsByEvaluation.get(evaluationId);
      if (extraction) mergedEvaluations.add(evaluationId);
      const failureReason = scrub(text(result, "failure_reason") ?? text(result, "reason") ?? text(result, "error_type"));
      const review = extraction ? bool(extraction, "requires_human_review") : false;
      const extractionFailed = extraction ? text(extraction, "quality") === "failed" : false;
      const codes = extraction ? list(extraction, "review_codes") : [];
      events.push(
        event(
          {
            id: `lectura-${evaluationId}`,
            ts: extraction ? (when(extraction, "created_at") ?? ts) : ts,
            kind: "lectura",
            outcome: extractionFailed ? "error" : review ? "warn" : statusOutcome(status),
            title: "Lectura del documento",
            company,
            summary: `Estado ${status}${extraction ? ` · ${extractionFailed ? "fallida" : review ? "en revisión humana" : "correcta"}${bool(extraction, "is_current") ? " · vigente" : ""}` : ""}${
              extraction && text(extraction, "filename") ? ` · ${text(extraction, "filename")}` : ""
            }`,
            reason: codes.length ? codes.join(", ") : failureReason,
            costUsd: cost,
            facts: facts(
              fact("Modelo", model),
              fact("Política", text(row, "policy_version")),
              fact("Tokens", tokenCount(row)),
              fact("Hechos extraídos", extraction ? toNumber(extraction, "facet_count") : null),
            ),
          },
          { evaluation: row, extraction: extraction ?? null },
        ),
      );
    } else if (kind === "gemini_match") {
      const origin = text(row, "origin");
      const automatic = origin === "prod2_jev_domain_v1";
      events.push(
        event(
          {
            id: `evaluacion-${evaluationId}`,
            ts,
            kind: "evaluacion",
            outcome: statusOutcome(status),
            title: automatic ? "Evaluación automática" : origin === "prod2_manual_request_v1" ? "Evaluación manual (Calificar)" : "Evaluación manual o de lote",
            company,
            summary: `Estado ${status}${toNumber(result, "score") != null ? ` · puntaje ${toNumber(result, "score")}` : ""}${text(result, "verdict") ? ` · veredicto ${text(result, "verdict")}` : ""} · origen ${origin ?? text(row, "policy_version") ?? "—"}`,
            reason: status === "done" ? null : scrub(text(result, "reason")),
            costUsd: cost,
            facts: facts(fact("Modelo", model), fact("Política", text(row, "policy_version")), fact("Tokens", tokenCount(row))),
          },
          row,
        ),
      );
    }
  }

  // Lecturas guardadas cuya reserva no apareció arriba (por ejemplo, reservas anteriores a este registro).
  for (const row of raw.extractions) {
    const evaluationId = text(row, "evaluation_id") ?? "";
    if (mergedEvaluations.has(evaluationId)) continue;
    const created = when(row, "created_at");
    if (!created) continue;
    const review = bool(row, "requires_human_review");
    const failed = text(row, "quality") === "failed";
    events.push(
      event(
        {
          id: `lectura-guardada-${text(row, "id")}`,
          ts: created,
          kind: "lectura",
          outcome: failed ? "error" : review ? "warn" : "ok",
          title: "Lectura guardada",
          summary: `${text(row, "filename") ?? "documento"} · ${failed ? "fallida" : review ? "en revisión humana" : "correcta"}`,
          reason: list(row, "review_codes").join(", ") || null,
          costUsd: toNumber(row, "cost_usd"),
          facts: facts(fact("Modelo", text(row, "model")), fact("Tokens", tokenCount(row))),
        },
        row,
      ),
    );
  }

  for (const row of raw.jevCache) {
    const at = when(row, "completed_at") ?? when(row, "created_at");
    if (!at) continue;
    const blocked = text(row, "blocked_reason");
    events.push(
      event(
        {
          id: `jev-cache-${text(row, "cache_key")?.slice(0, 12)}`,
          ts: at,
          kind: "jev",
          outcome: blocked ? "warn" : text(row, "status") === "done" ? "ok" : "pending",
          title: "Caché de decisión JEV",
          summary: `Estado ${text(row, "status") ?? "—"}${text(row, "reused_evaluation_id") ? ` · reutiliza la evaluación ${text(row, "reused_evaluation_id")}` : ""}`,
          reason: scrub(blocked),
          facts: facts(fact("Política", text(row, "policy_version")), fact("Modelo", text(row, "model"))),
        },
        { ...row, cache_key: text(row, "cache_key")?.slice(0, 16) },
      ),
    );
  }

  events.push(...manualRequestEvents(raw.manual, labels, "mayores"));

  const currency = text(discovery, "currency");
  const amount = toNumber(discovery, "reference_amount");
  const sorted = sortEvents(events);
  if (!raw.evaluations.length) notes.push("Todavía no tiene evaluaciones registradas (JEV, lectura ni evaluación).");
  return {
    source: "mayores",
    id,
    header: {
      code: text(discovery, "nomenclature") ?? `Convocatoria ${id}`,
      entity: text(discovery, "buyer_name"),
      description: text(discovery, "description"),
      facts: facts(
        fact("Objeto", text(discovery, "object_label")),
        fact("Monto referencial", amount != null ? `${currency ?? ""} ${amount.toLocaleString("es-PE")}`.trim() : null),
        fact("Publicada", when(discovery, "published_at"), true),
        fact("Cierre de propuestas", when(discovery, "proposals_closes_at"), true),
        fact("Procedimiento", text(discovery, "id_procedimiento")),
      ),
      notes,
    },
    events: sorted,
    totals: totalsOf(sorted),
  };
}
