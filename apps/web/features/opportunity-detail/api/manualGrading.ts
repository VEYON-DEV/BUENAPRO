import { ApiError, apiFetch } from "@/lib/api/client";
import {
  normalizeGradingRequest,
  normalizeGradingUsage,
  type GradingProblem,
  type GradingRequest,
  type GradingUsage,
} from "../model/manualGrading";

/** Códigos de rechazo del servidor que se muestran tal cual como motivo de la falla. */
const PRECONDITION_CODES: ReadonlySet<string> = new Set([
  "contract_not_found",
  "jev_below_60",
  "convocatoria_cerrada",
  "already_graded",
]);

export type GradingSnapshot = { request: GradingRequest | null; usage: GradingUsage | null };

export type GradingSubmission =
  | { kind: "done"; usage: GradingUsage | null }
  | { kind: "queued"; request: GradingRequest | null; usage: GradingUsage | null }
  | { kind: "problem"; problem: GradingProblem; usage: GradingUsage | null }
  | { kind: "failed"; reason: string | null; usage: GradingUsage | null };

/**
 * Última calificación manual de la licitación o convocatoria y uso de hoy de la empresa.
 * `endpoint` es la ruta de calificación de la oportunidad (ver gradingEndpoint en el modelo).
 */
export async function fetchGradingStatus(
  endpoint: string,
  profileId: string | null | undefined,
  signal?: AbortSignal,
): Promise<GradingSnapshot> {
  const query = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : "";
  const result = await apiFetch<{ data?: { request?: unknown; usage?: unknown } }>(`${endpoint}${query}`, {
    cache: "no-store",
    signal,
  });
  return {
    request: normalizeGradingRequest(result.data?.request),
    usage: normalizeGradingUsage(result.data?.usage),
  };
}

/** Pide calificar. Los fallos esperados (tope, perfil, evaluación) vuelven como resultado, no como excepción. */
export async function submitGrading(
  endpoint: string,
  options: { profileId?: string | null; force: boolean },
): Promise<GradingSubmission> {
  try {
    const result = await apiFetch<{ data?: { status?: string; request?: unknown; usage?: unknown } }>(endpoint, {
      method: "POST",
      json: { force: options.force, ...(options.profileId ? { profile_id: options.profileId } : {}) },
    });
    const usage = normalizeGradingUsage(result.data?.usage);
    if (result.data?.status === "done") return { kind: "done", usage };
    return { kind: "queued", request: normalizeGradingRequest(result.data?.request), usage };
  } catch (error) {
    if (!(error instanceof ApiError)) return { kind: "problem", problem: { kind: "network" }, usage: null };
    const payload = (error.payload ?? {}) as { code?: unknown; reason?: unknown; usage?: unknown };
    const usage = normalizeGradingUsage(payload.usage);
    if (error.status === 429) return { kind: "problem", problem: { kind: "limit", message: error.message }, usage };
    // Un 409 sin código (o profile_not_found) es la falta de perfil activo; los demás 409 de mayores
    // (jev_below_60, convocatoria_cerrada, already_graded) viajan como motivo de la falla.
    if (error.status === 409 && (payload.code == null || payload.code === "profile_not_found")) {
      return { kind: "problem", problem: { kind: "no_profile" }, usage };
    }
    const reason = typeof payload.reason === "string"
      ? payload.reason
      : typeof payload.code === "string" && PRECONDITION_CODES.has(payload.code) ? payload.code : null;
    return { kind: "failed", reason, usage };
  }
}
