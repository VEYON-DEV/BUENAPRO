export type GradingStatus = "pending" | "reading" | "evaluating" | "done" | "failed";
export type GradingMode = "direct" | "queued";

export type GradingRequest = {
  status: GradingStatus;
  mode: GradingMode;
  reason: string | null;
  created_at: string;
  updated_at: string;
};

export type GradingUsage = { used: number; limit: number };

/** Problemas que no vienen de una solicitud guardada: se muestran hasta el siguiente intento. */
export type GradingProblem =
  | { kind: "limit"; message?: string }
  | { kind: "no_profile" }
  | { kind: "network" };

export type GradingTone = "idle" | "active" | "failed" | "limit" | "no_profile";

export type GradingPresentation = {
  tone: GradingTone;
  label: string;
  disabled: boolean;
  /** Texto bajo el botón: pista de progreso, motivo de la falla o aviso de tope. */
  message: string | null;
  usageText: string | null;
};

/** Frecuencia con la que la página consulta una calificación en curso. */
export const GRADING_POLL_INTERVAL_MS = 20_000;

const ACTIVE_STATUSES: ReadonlySet<string> = new Set(["pending", "reading", "evaluating"]);
const STATUSES: ReadonlySet<string> = new Set(["pending", "reading", "evaluating", "done", "failed"]);

const FAILURE_MESSAGES: Record<string, string> = {
  reading_requires_review: "La lectura de los documentos necesita revisión manual antes de calificar.",
  reading_failed: "No se pudieron leer los documentos de la licitación.",
  no_documents: "La licitación no tiene documentos disponibles para leer.",
  contract_not_found: "No se encontró la licitación.",
  profile_inactive: "Tu perfil de empresa no está activo. Actívalo en tu perfil e inténtalo de nuevo.",
  invalid_model_output: "La IA devolvió una respuesta que no se pudo validar.",
  reading_timeout: "La lectura de los documentos tardó demasiado.",
  no_requirements: "La lectura no contiene requisitos con los que evaluar la capacidad de tu empresa.",
  evaluation_interrupted: "La calificación se interrumpió antes de terminar.",
  evaluation_failed: "No se pudo completar la calificación.",
};

export function isGradingActive(status: string | null | undefined): boolean {
  return status != null && ACTIVE_STATUSES.has(status);
}

export function gradingFailureMessage(reason: string | null | undefined): string {
  const base = (reason && FAILURE_MESSAGES[reason]) || FAILURE_MESSAGES.evaluation_failed;
  return `${base} Puedes intentarlo de nuevo.`;
}

export function gradingLimitMessage(limit: number): string {
  return `Se alcanzó el límite diario de ${limit} calificaciones manuales de tu empresa.`;
}

export function gradingUsageText(usage: GradingUsage | null | undefined): string | null {
  if (!usage || !Number.isFinite(usage.used) || !Number.isFinite(usage.limit)) return null;
  return `Hoy: ${usage.used} de ${usage.limit} calificaciones manuales`;
}

/** Valida la respuesta del servidor: un estado desconocido se descarta en vez de pintarse. */
export function normalizeGradingRequest(value: unknown): GradingRequest | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (typeof row.status !== "string" || !STATUSES.has(row.status)) return null;
  return {
    status: row.status as GradingStatus,
    mode: row.mode === "direct" ? "direct" : "queued",
    reason: typeof row.reason === "string" ? row.reason : null,
    created_at: String(row.created_at ?? ""),
    updated_at: String(row.updated_at ?? ""),
  };
}

export function normalizeGradingUsage(value: unknown): GradingUsage | null {
  if (!value || typeof value !== "object") return null;
  const { used, limit } = value as Record<string, unknown>;
  return Number.isFinite(used) && Number.isFinite(limit) && Number(limit) > 0
    ? { used: Number(used), limit: Number(limit) }
    : null;
}

/** Solicitud sintética para reflejar de inmediato lo que respondió el servidor, sin esperar otra consulta. */
export function localGradingRequest(status: GradingStatus, reason: string | null = null, now: Date = new Date()): GradingRequest {
  const stamp = now.toISOString();
  return { status, mode: "direct", reason, created_at: stamp, updated_at: stamp };
}

function activeHint(request: GradingRequest | null): string {
  return request?.status === "evaluating"
    ? "Evaluando con tu perfil; puede tardar un minuto."
    : "Leyendo los documentos; puede tardar unos minutos.";
}

/** Decide qué mostrar según el estado conocido; el servidor sigue siendo quien aplica las reglas. */
export function gradingPresentation(state: {
  request: GradingRequest | null;
  usage: GradingUsage | null;
  submitting: boolean;
  problem: GradingProblem | null;
  /** Ya existe una calificación de esta licitación para el perfil: la acción es re-calificar. */
  regrade: boolean;
}): GradingPresentation {
  const { request, usage, submitting, problem, regrade } = state;
  const usageText = gradingUsageText(usage);
  const idleLabel = regrade ? "Re-calificar" : "Calificar";

  if (problem?.kind === "no_profile") {
    return { tone: "no_profile", label: "Configura tu perfil para calificar", disabled: false, message: null, usageText };
  }
  if (submitting || isGradingActive(request?.status)) {
    return { tone: "active", label: "Calificación en curso…", disabled: true, message: activeHint(request), usageText };
  }
  if (problem?.kind === "limit" || (usage && usage.used >= usage.limit)) {
    const limit = usage?.limit;
    const message = problem?.kind === "limit" && problem.message
      ? problem.message
      : limit ? gradingLimitMessage(limit) : "Se alcanzó el límite diario de calificaciones manuales de tu empresa.";
    return { tone: "limit", label: idleLabel, disabled: true, message, usageText };
  }
  if (request?.status === "failed") {
    return { tone: "failed", label: "Reintentar calificación", disabled: false, message: gradingFailureMessage(request.reason), usageText };
  }
  if (problem?.kind === "network") {
    return {
      tone: "failed",
      label: "Reintentar calificación",
      disabled: false,
      message: "No se pudo conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.",
      usageText,
    };
  }
  return { tone: "idle", label: idleLabel, disabled: false, message: null, usageText };
}
