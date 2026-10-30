import { pool, query } from "@/server/db/client";
import { analyzeMatchDirect, type DirectMatchAnalysisResult } from "./matchAnalysis";

/**
 * Calificación manual de licitaciones menores (PROD6).
 *
 * Cada calificación manual la paga la empresa: el tope diario es por tenant y por día de Lima,
 * suma todas las fuentes (seace_prod6 ahora, seace_prod2 después) y también cuenta la re-calificación.
 * El conteo y el alta de la solicitud ocurren en una sola transacción bajo un lock consultivo por
 * tenant, de modo que dos clics simultáneos no pueden pasar el tope.
 */

export const MANUAL_GRADING_SOURCE_PROD6 = "seace_prod6";
export const DEFAULT_MANUAL_GRADING_DAILY_LIMIT = 100;
const ACTIVE_STATUSES = ["pending", "reading", "evaluating"];
/** Una solicitud directa que sigue activa pasado este tiempo quedó huérfana (reinicio del proceso). */
const DIRECT_STALE_SECONDS = 300;
const LOCK_NAMESPACE = "manual_grading_requests";

export type ManualGradingStatus = "pending" | "reading" | "evaluating" | "done" | "failed";
export type ManualGradingMode = "direct" | "queued";

export type ManualGradingRequest = {
  id: string;
  status: ManualGradingStatus;
  mode: ManualGradingMode;
  reason: string | null;
  created_at: string | Date;
  updated_at: string | Date;
};

export type ManualGradingStatusView = {
  status: ManualGradingStatus;
  mode: ManualGradingMode;
  reason: string | null;
  created_at: string | Date;
  updated_at: string | Date;
};

export type ManualGradingUsage = { used: number; limit: number };

export type MinorGradingOutcome =
  | { status: "done"; data: DirectMatchAnalysisResult }
  | { status: "queued"; request: ManualGradingRequest };

export type ManualGradingErrorCode =
  | "daily_limit_reached"
  | "profile_not_found"
  | "contract_not_found";

/** Error tipado: la ruta lo traduce a 429, 409 o 404 sin inspeccionar el mensaje. */
export class ManualGradingError extends Error {
  readonly code: ManualGradingErrorCode;
  readonly limit?: number;
  readonly used?: number;
  constructor(code: ManualGradingErrorCode, message: string, details: { limit?: number; used?: number } = {}) {
    super(message);
    this.name = "ManualGradingError";
    this.code = code;
    this.limit = details.limit;
    this.used = details.used;
  }
}

export function manualGradingDailyLimit(): number {
  const raw = Number(process.env.MANUAL_GRADING_DAILY_LIMIT);
  return Number.isSafeInteger(raw) && raw > 0 ? raw : DEFAULT_MANUAL_GRADING_DAILY_LIMIT;
}

export function dailyLimitMessage(limit: number): string {
  return `Se alcanzó el límite diario de ${limit} calificaciones manuales de tu empresa.`;
}

/** Misma resolución de perfil que analyzeMatchDirect (matchAnalysis.ts); mantener alineadas. */
async function resolveActiveProfileId(tenantId: string, profileId?: string | null): Promise<string> {
  const result = await query<{ id: string }>(
    `SELECT id FROM company_profiles
     WHERE tenant_id=$1 AND is_active=true AND ($2::uuid IS NULL OR id=$2::uuid)
     ORDER BY updated_at DESC LIMIT 1`,
    [tenantId, profileId ?? null],
  );
  const id = result.rows[0]?.id;
  if (!id) {
    throw new ManualGradingError("profile_not_found", "No se encontró un perfil activo para esta empresa.");
  }
  return id;
}

/** La misma condición de lectura válida que exige analyzeMatchDirect. */
async function contractReadingState(idContrato: number): Promise<{ exists: boolean; validReading: boolean }> {
  const result = await query<{
    id_contrato: string;
    extraction_id: string | number | null;
    quality: string | null;
    requires_human_review: boolean | null;
  }>(
    `SELECT c.id_contrato, te.id AS extraction_id, te.quality, te.requires_human_review
     FROM seace_contracts c
     LEFT JOIN LATERAL (
       SELECT tx.id, tx.quality, tx.requires_human_review FROM contract_documents d
       JOIN tdr_extractions tx ON tx.contract_document_id=d.id AND tx.is_current=true
       WHERE d.id_contrato=c.id_contrato ORDER BY tx.created_at DESC, tx.id DESC LIMIT 1
     ) te ON true WHERE c.id_contrato=$1`,
    [idContrato],
  );
  const row = result.rows[0];
  if (!row) return { exists: false, validReading: false };
  return {
    exists: true,
    validReading: Boolean(row.extraction_id) && row.quality !== "failed" && !row.requires_human_review,
  };
}

function publicRequest(row: Record<string, unknown>): ManualGradingRequest {
  return {
    id: String(row.id),
    status: row.status as ManualGradingStatus,
    mode: row.mode as ManualGradingMode,
    reason: (row.reason as string | null) ?? null,
    created_at: row.created_at as string | Date,
    updated_at: row.updated_at as string | Date,
  };
}

type SqlRunner = (text: string, values: unknown[]) => Promise<{ rows: Record<string, unknown>[] }>;

/** Cuenta todas las solicitudes del día de Lima del tenant, de cualquier fuente, modo, estado o re-calificación. */
async function countTodayUsage(run: SqlRunner, tenantId: string): Promise<number> {
  const result = await run(
    `SELECT count(*)::int AS used FROM manual_grading_requests
     WHERE tenant_id=$1::uuid AND lima_day=(now() AT TIME ZONE 'America/Lima')::date`,
    [tenantId],
  );
  return Number(result.rows[0]?.used ?? 0);
}

export async function manualGradingUsage(tenantId: string): Promise<ManualGradingUsage> {
  return { used: await countTodayUsage((text, values) => query(text, values), tenantId), limit: manualGradingDailyLimit() };
}

type Reservation = { request: ManualGradingRequest; reused: boolean };

/**
 * Reserva atómica: lock por tenant -> solicitud activa existente -> tope diario -> INSERT.
 * Una solicitud activa devuelta no consume cupo.
 */
async function reserveRequest(input: {
  tenantId: string;
  idContrato: number;
  profileId: string;
  actorId?: string | null;
  mode: ManualGradingMode;
  status: "pending" | "evaluating";
}): Promise<Reservation> {
  const limit = manualGradingDailyLimit();
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2))", [LOCK_NAMESPACE, input.tenantId]);
    await client.query(
      `UPDATE manual_grading_requests
       SET status='failed', reason='evaluation_interrupted', updated_at=now()
       WHERE tenant_id=$1::uuid AND source_key=$2 AND source_id=$3 AND profile_id=$4::uuid
         AND mode='direct' AND status = ANY($5::text[])
         AND updated_at < now() - ($6::int * interval '1 second')`,
      [input.tenantId, MANUAL_GRADING_SOURCE_PROD6, input.idContrato, input.profileId, ACTIVE_STATUSES, DIRECT_STALE_SECONDS],
    );
    const active = await client.query(
      `SELECT id, status, mode, reason, created_at, updated_at FROM manual_grading_requests
       WHERE tenant_id=$1::uuid AND source_key=$2 AND source_id=$3 AND profile_id=$4::uuid
         AND status = ANY($5::text[])
       ORDER BY created_at DESC, id DESC LIMIT 1`,
      [input.tenantId, MANUAL_GRADING_SOURCE_PROD6, input.idContrato, input.profileId, ACTIVE_STATUSES],
    );
    if (active.rows[0]) {
      await client.query("COMMIT");
      return { request: publicRequest(active.rows[0]), reused: true };
    }
    const used = await countTodayUsage((text, values) => client.query(text, values), input.tenantId);
    if (used >= limit) {
      throw new ManualGradingError("daily_limit_reached", dailyLimitMessage(limit), { limit, used });
    }
    const inserted = await client.query(
      `INSERT INTO manual_grading_requests (tenant_id, profile_id, source_key, source_id, requested_by, mode, status)
       VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
       RETURNING id, status, mode, reason, created_at, updated_at`,
      [
        input.tenantId,
        input.profileId,
        MANUAL_GRADING_SOURCE_PROD6,
        input.idContrato,
        input.actorId ?? null,
        input.mode,
        input.status,
      ],
    );
    await client.query("COMMIT");
    return { request: publicRequest(inserted.rows[0]), reused: false };
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw error;
  } finally {
    client.release();
  }
}

/** Códigos de falla de una evaluación directa; el worker usa los mismos para las solicitudes en cola. */
export function directFailureReason(error: unknown): string {
  const message = error instanceof Error ? error.message : "";
  if (/perfil activo/i.test(message)) return "profile_inactive";
  if (/no se encontró la licitación/i.test(message)) return "contract_not_found";
  if (/lectura documental válida/i.test(message)) return "reading_requires_review";
  if (/requisitos sustantivos para evaluar capacidad/i.test(message)) return "no_requirements";
  if (/respuesta|truncado|referencias documentales|SyntaxError|JSON/i.test(`${message} ${error instanceof Error ? error.name : ""}`)) {
    return "invalid_model_output";
  }
  return "evaluation_failed";
}

function logCloseError(error: unknown) {
  console.error("manual_grading: no se pudo cerrar la solicitud", error);
}

async function markRequest(id: string, status: "done" | "failed", reason: string | null) {
  await query(
    `UPDATE manual_grading_requests SET status=$2, reason=$3, updated_at=now() WHERE id=$1::bigint`,
    [id, status, reason],
  );
}

/**
 * Califica manualmente una licitación menor. Con lectura válida evalúa en el momento (modo directo);
 * sin ella deja una solicitud en cola para que el worker lea los documentos y evalúe.
 */
export async function requestMinorGrading(input: {
  tenantId: string;
  idContrato: number;
  profileId?: string | null;
  actorId?: string | null;
  force?: boolean;
}): Promise<MinorGradingOutcome> {
  const profileId = await resolveActiveProfileId(input.tenantId, input.profileId);
  const contract = await contractReadingState(input.idContrato);
  if (!contract.exists) {
    throw new ManualGradingError("contract_not_found", "No se encontró la licitación.");
  }

  const direct = contract.validReading;
  const reservation = await reserveRequest({
    tenantId: input.tenantId,
    idContrato: input.idContrato,
    profileId,
    actorId: input.actorId,
    mode: direct ? "direct" : "queued",
    status: direct ? "evaluating" : "pending",
  });
  if (reservation.reused) return { status: "queued", request: reservation.request };
  if (!direct) return { status: "queued", request: reservation.request };

  try {
    const data = await analyzeMatchDirect({
      tenantId: input.tenantId,
      idContrato: input.idContrato,
      profileId,
      actorId: input.actorId,
      force: Boolean(input.force),
    });
    // El análisis ya quedó guardado: si falla el cierre de la solicitud no se pierde el resultado.
    // Una solicitud directa que no se cierra caduca sola (DIRECT_STALE_SECONDS).
    if ("skipped" in data) {
      // Sin cambios: no hubo costo de IA, así que la solicitud no consume cupo.
      await query(`DELETE FROM manual_grading_requests WHERE id=$1::bigint`, [reservation.request.id]).catch(logCloseError);
    } else {
      await markRequest(reservation.request.id, "done", null).catch(logCloseError);
    }
    return { status: "done", data };
  } catch (error) {
    await markRequest(reservation.request.id, "failed", directFailureReason(error)).catch(logCloseError);
    throw error;
  }
}

/** Última solicitud de esta licitación y perfil, más el uso de hoy del tenant. */
export async function minorGradingStatus(input: {
  tenantId: string;
  idContrato: number;
  profileId?: string | null;
}): Promise<{ request: ManualGradingStatusView | null; usage: ManualGradingUsage }> {
  const usage = await manualGradingUsage(input.tenantId);
  let profileId: string;
  try {
    profileId = await resolveActiveProfileId(input.tenantId, input.profileId);
  } catch (error) {
    if (error instanceof ManualGradingError && error.code === "profile_not_found") return { request: null, usage };
    throw error;
  }
  const result = await query(
    `SELECT status, mode, reason, created_at, updated_at,
       (mode='direct' AND status = ANY($4::text[])
         AND updated_at < now() - ($5::int * interval '1 second')) AS stale
     FROM manual_grading_requests
     WHERE tenant_id=$1::uuid AND source_key=$2 AND source_id=$3 AND profile_id=$6::uuid
     ORDER BY created_at DESC, id DESC LIMIT 1`,
    [input.tenantId, MANUAL_GRADING_SOURCE_PROD6, input.idContrato, ACTIVE_STATUSES, DIRECT_STALE_SECONDS, profileId],
  );
  const row = result.rows[0] as Record<string, unknown> | undefined;
  if (!row) return { request: null, usage };
  const stale = Boolean(row.stale);
  return {
    request: {
      status: stale ? "failed" : (row.status as ManualGradingStatus),
      mode: row.mode as ManualGradingMode,
      reason: stale ? "evaluation_interrupted" : ((row.reason as string | null) ?? null),
      created_at: row.created_at as string | Date,
      updated_at: row.updated_at as string | Date,
    },
    usage,
  };
}
