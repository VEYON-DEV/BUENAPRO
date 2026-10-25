/**
 * Afinidad JEV de licitaciones menores (PROD6): calificación comercial preliminar por empresa.
 * Estas funciones solo presentan lo guardado en `prod6_profile_decisions`; no evalúan requisitos
 * ni elegibilidad y nunca inventan un puntaje cuando la decisión no lo trae.
 */

export type JevDecision = "accepted" | "rejected" | "pending";
export type JevDecidedBy = "prefilter" | "keywords" | "jev";

/** Campos por perfil activo que expone `GET /api/contracts` (null si no hay decisión). */
export type JevListFields = {
  jev_score?: number | null;
  jev_decision?: string | null;
  jev_subcategory?: string | null;
  jev_threshold?: number | null;
  jev_decided_at?: string | Date | null;
};

/** Fila de `jev_by_company` en `GET /api/contracts/:id`. */
export type JevCompanyRow = {
  profile_id: string;
  profile_name: string | null;
  is_current_profile?: boolean | null;
  score?: number | null;
  threshold?: number | null;
  decision?: string | null;
  decided_by?: string | null;
  subcategory?: string | null;
  domain?: string | null;
  information?: string | null;
  decided_at?: string | Date | null;
};

export const JEV_LABEL = "Afinidad JEV";
export const JEV_DISCLAIMER = "Calificación preliminar de afinidad comercial (JEV); no evalúa requisitos ni elegibilidad.";

export const JEV_DECISION_LABELS: Record<JevDecision, string> = {
  accepted: "Aprobada",
  rejected: "Descartada",
  pending: "Pendiente",
};

export const JEV_DECIDED_BY_LABELS: Record<JevDecidedBy, string> = {
  prefilter: "Prefiltro",
  keywords: "Palabras clave",
  jev: "JEV",
};

export const JEV_SUBCATEGORY_LABELS: Record<string, string> = {
  licencias_software: "Licencias de software",
  servicios_informacion: "Servicios de información",
  hardware_ti: "Hardware TI",
  desarrollo_sistemas: "Desarrollo de sistemas",
  soporte_ti: "Soporte TI",
  redes_telecom: "Redes y telecomunicaciones",
  automatizacion_industrial: "Automatización industrial",
  electrico_instrumentacion: "Eléctrico e instrumentación",
  capacitacion: "Capacitación",
  otro: "Otro rubro",
};

export const JEV_DOMAIN_LABELS: Record<string, string> = {
  informatica: "Informática",
  industrial_electrica: "Industrial y eléctrica",
  otro: "Otro dominio",
  ambiguo: "Dominio ambiguo",
};

export const JEV_INFORMATION_LABELS: Record<string, string> = {
  claro: "Información suficiente",
  insuficiente: "Información insuficiente",
};

/** Un código nuevo todavía sin traducción se muestra legible en lugar de ocultarse o romper la vista. */
function readable(code: string) {
  const text = code.replace(/_/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : null;
}

function labelFor(map: Record<string, string>, code: unknown) {
  if (typeof code !== "string" || !code.trim()) return null;
  const key = code.trim();
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : readable(key);
}

export const jevSubcategoryLabel = (code: unknown) => labelFor(JEV_SUBCATEGORY_LABELS, code);
export const jevDomainLabel = (code: unknown) => labelFor(JEV_DOMAIN_LABELS, code);
export const jevInformationLabel = (code: unknown) => labelFor(JEV_INFORMATION_LABELS, code);
export const jevDecisionLabel = (code: unknown) => labelFor(JEV_DECISION_LABELS, code);
export const jevDecidedByLabel = (code: unknown) => labelFor(JEV_DECIDED_BY_LABELS, code);

function validScore(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

function validThreshold(value: unknown) {
  return validScore(value);
}

/**
 * `superseded`: era pendiente y existe una fila más nueva para el mismo contrato y empresa. No es un
 * estado para el usuario: nunca se muestra como pendiente ni como resultado.
 */
export function isJevSuperseded(value: unknown) {
  return value === "superseded";
}

function asDecision(value: unknown): JevDecision | null {
  return value === "accepted" || value === "rejected" || value === "pending" ? value : null;
}

export type JevAffinity = {
  /** Entero 0–100 para mostrar; el valor exacto se conserva en `exactScore`. */
  score: number;
  exactScore: number;
  valueLabel: string;
  label: string;
  threshold: number | null;
  decision: JevDecision | null;
  decisionLabel: string | null;
  subcategory: string | null;
  subcategoryLabel: string | null;
  description: string;
};

/**
 * Afinidad JEV del perfil activo para una fila de la lista. Devuelve null cuando no hay un puntaje
 * válido (sin decisión, pendiente o descartada por prefiltro): la lista conserva entonces su indicador actual.
 */
export function jevAffinity(row: JevListFields | null | undefined): JevAffinity | null {
  if (isJevSuperseded(row?.jev_decision)) return null;
  const exact = validScore(row?.jev_score);
  if (exact == null) return null;
  const score = Math.round(exact);
  const threshold = validThreshold(row?.jev_threshold);
  const decision = asDecision(row?.jev_decision);
  const subcategoryLabel = jevSubcategoryLabel(row?.jev_subcategory);
  const parts = [`${JEV_LABEL}: ${score} de 100`];
  if (subcategoryLabel) parts.push(subcategoryLabel);
  if (threshold != null) parts.push(`umbral ${Math.round(threshold)}`);
  return {
    score, exactScore: exact, valueLabel: `${score} / 100`, label: JEV_LABEL, threshold, decision,
    decisionLabel: decision ? JEV_DECISION_LABELS[decision] : null,
    subcategory: typeof row?.jev_subcategory === "string" && row.jev_subcategory.trim() ? row.jev_subcategory.trim() : null,
    subcategoryLabel,
    description: `${parts.join(". ")}. ${JEV_DISCLAIMER}`,
  };
}

export type JevCompanyAffinity = {
  profileId: string;
  name: string;
  isCurrent: boolean;
  /** Puntaje JEV 0–100 redondeado; null si la decisión no lo trae (prefiltro, palabras clave o pendiente). */
  score: number | null;
  threshold: number | null;
  decision: JevDecision | null;
  decisionLabel: string;
  /** Tono semántico del estado; el texto del estado siempre acompaña al color. */
  tone: "green" | "red" | "amber" | "neutral";
  decidedBy: string | null;
  decidedByLabel: string | null;
  subcategoryLabel: string | null;
  domainLabel: string | null;
  informationLabel: string | null;
  informationClear: boolean | null;
  decidedAt: string | Date | null;
  /** Explicación de las filas sin puntaje; null cuando hay medidor. */
  noScoreNote: string | null;
  valueText: string;
};

const DECISION_TONES: Record<JevDecision, JevCompanyAffinity["tone"]> = { accepted: "green", rejected: "red", pending: "amber" };

/** Normaliza `jev_by_company` conservando el orden recibido (perfil activo y mayor puntaje primero); omite las filas reemplazadas (`superseded`). */
export function jevCompanyAffinities(rows: JevCompanyRow[] | null | undefined): JevCompanyAffinity[] {
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    if (!row || typeof row.profile_id !== "string" || isJevSuperseded(row.decision)) return [];
    const decision = asDecision(row.decision);
    const score = validScore(row.score);
    const threshold = validThreshold(row.threshold);
    const decidedBy = typeof row.decided_by === "string" ? row.decided_by : null;
    const scoreRounded = score == null ? null : Math.round(score);
    const decidedByLabel = decidedBy ? jevDecidedByLabel(decidedBy) : null;
    const noScoreNote = scoreRounded != null ? null
      : decision === "pending" ? "En espera de la calificación JEV."
      : decidedBy && decidedBy !== "jev" ? `Sin calificación JEV: decidida por ${(decidedByLabel ?? "").toLowerCase()}.`
      : "Sin calificación JEV disponible.";
    const information = typeof row.information === "string" ? row.information.trim() : "";
    const name = row.profile_name?.trim() || "Empresa sin nombre";
    const decisionLabel = decision ? JEV_DECISION_LABELS[decision] : jevDecisionLabel(row.decision) ?? "Sin estado";
    return [{
      profileId: row.profile_id, name, isCurrent: Boolean(row.is_current_profile),
      score: scoreRounded, threshold, decision, decisionLabel,
      tone: decision ? DECISION_TONES[decision] : "neutral",
      decidedBy, decidedByLabel,
      subcategoryLabel: jevSubcategoryLabel(row.subcategory),
      domainLabel: jevDomainLabel(row.domain),
      informationLabel: information ? jevInformationLabel(information) : null,
      informationClear: information === "claro" ? true : information === "insuficiente" ? false : null,
      decidedAt: row.decided_at ?? null,
      noScoreNote,
      valueText: scoreRounded == null ? noScoreNote ?? ""
        : `${scoreRounded} de 100${threshold != null ? `; umbral ${Math.round(threshold)}` : ""}`,
    }];
  });
}
