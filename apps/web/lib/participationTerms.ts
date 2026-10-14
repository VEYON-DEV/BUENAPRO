export type ParticipationStatus = "permitted" | "prohibited" | "conditional" | "not_identified" | "pending";
export type ParticipationTerm = {
  status: ParticipationStatus;
  clause: string | null;
  page: number | null;
  conditions: string[];
  needsReview: boolean;
};
export type ParticipationReading = {
  subcontratacion: ParticipationTerm;
  consorcio: ParticipationTerm;
  pageScope: "combined" | "document";
};

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** Only a source-scoped current extraction is supplied here. Canonical opportunity
 * flags may describe another preserved reading and must never be used as fallback. */
export function participationReading(value: unknown): ParticipationReading {
  const extraction = record(value);
  const usable = extraction.id != null && extraction.quality != null && extraction.quality !== "failed";
  const terms = record(extraction.participation);
  const normalize = (value: unknown): ParticipationTerm => {
    const term = record(value);
    const clause = typeof term.clause === "string" && term.clause.trim() ? term.clause.trim() : null;
    const page = typeof term.page === "number" && Number.isSafeInteger(term.page) && term.page >= 1 ? term.page : null;
    const conditions = Array.isArray(term.conditions)
      ? term.conditions.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map(item => item.trim()) : [];
    let status: ParticipationStatus = "not_identified";
    if (!usable) return { status: "pending", clause: null, page: null, conditions: [], needsReview: false };
    if (clause && page && ["permitted", "prohibited", "conditional"].includes(String(term.status))) {
      status = term.status as ParticipationStatus;
      if (status === "permitted" && conditions.length) status = "conditional";
    }
    return { status, clause, page, conditions, needsReview: extraction.requires_human_review !== false };
  };
  return { subcontratacion: normalize(terms.subcontratacion), consorcio: normalize(terms.consorcio),
    pageScope: extraction.page_scope === "combined" ? "combined" : "document" };
}

export function participationLabel(term: ParticipationTerm, kind: "subcontratacion" | "consorcio" = "subcontratacion"): string {
  return { permitted: kind === "consorcio" ? "Permitido" : "Permitida", prohibited: kind === "consorcio" ? "No permitido" : "No permitida", conditional: "Con condiciones",
    not_identified: "Por confirmar", pending: "Por leer" }[term.status];
}

export function participationHint(term: ParticipationTerm, kind: "subcontratacion" | "consorcio" = "subcontratacion"): string {
  if (term.status === "pending") return "La lectura de las bases todavía no está disponible.";
  if (term.status === "not_identified") return "La lectura no identifica una cláusula con página suficiente para determinar el permiso. Revisa las bases.";
  return `${participationLabel(term, kind)} según la lectura de las bases.${term.needsReview ? " Requiere revisión humana de la cita y las condiciones." : " Comprueba la cita y las condiciones antes de participar."}`;
}
