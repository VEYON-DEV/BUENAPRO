import { ApiError, apiFetch } from "@/lib/api/client";
import {
  normalizeGradingRequest,
  normalizeGradingUsage,
  type GradingProblem,
  type GradingRequest,
  type GradingUsage,
} from "../model/manualGrading";

export type GradingSnapshot = { request: GradingRequest | null; usage: GradingUsage | null };

export type GradingSubmission =
  | { kind: "done"; usage: GradingUsage | null }
  | { kind: "queued"; request: GradingRequest | null; usage: GradingUsage | null }
  | { kind: "problem"; problem: GradingProblem; usage: GradingUsage | null }
  | { kind: "failed"; reason: string | null; usage: GradingUsage | null };

function endpoint(idContrato: string | number) {
  return `/api/contracts/${encodeURIComponent(String(idContrato))}/grade`;
}

/** Última calificación manual de la licitación y uso de hoy de la empresa. */
export async function fetchGradingStatus(
  idContrato: string | number,
  profileId: string | null | undefined,
  signal?: AbortSignal,
): Promise<GradingSnapshot> {
  const query = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : "";
  const result = await apiFetch<{ data?: { request?: unknown; usage?: unknown } }>(`${endpoint(idContrato)}${query}`, {
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
  idContrato: string | number,
  options: { profileId?: string | null; force: boolean },
): Promise<GradingSubmission> {
  try {
    const result = await apiFetch<{ data?: { status?: string; request?: unknown; usage?: unknown } }>(endpoint(idContrato), {
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
    if (error.status === 409) return { kind: "problem", problem: { kind: "no_profile" }, usage };
    const reason = typeof payload.reason === "string"
      ? payload.reason
      : payload.code === "contract_not_found" ? "contract_not_found" : null;
    return { kind: "failed", reason, usage };
  }
}
