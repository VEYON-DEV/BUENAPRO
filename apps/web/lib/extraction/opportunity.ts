type AnyRecord = Record<string, any>;

const tipoPagoLabels: Record<string, string> = {
  pago_unico: "Pago único",
  armadas: "En armadas",
  mensual: "Pago mensual",
  por_entregable: "Por entregable",
};

export function tipoPagoLabel(value?: string | null): string | null {
  if (!value || value === "no_determinado") return null;
  return tipoPagoLabels[value] ?? value.replaceAll("_", " ");
}

export type OpportunityFacts = {
  descripcionCorta: string | null;
  lecturaRapida: string | null;
  tipoPago: string | null;
  plazoDias: number | null;
  entregables: number;
  penalidadTopePct: number | null;
  roles: string[];
  documentosClave: string[];
  observaciones: string[];
  monto: number | null;
};

function toNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter(Boolean).map(String) : [];
}

function dedupe(values: string[]): string[] {
  const seen = new Set<string>();
  return values.filter((value) => {
    const key = value.trim().toLowerCase();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * Lee los hechos de una oportunidad con fallback en cascada:
 * summary_json derivado -> columnas del filter index -> extracción cruda.
 * Toda vista debe pasar por aquí en vez de leer summary_json a mano.
 */
export function opportunityFacts(row: AnyRecord | null | undefined): OpportunityFacts {
  const summary: AnyRecord = row?.summary_json ?? {};
  const extraction: AnyRecord = row?.raw_extraction_json ?? {};
  const rawSummary = extraction.summary;
  const lecturaRapida =
    typeof rawSummary === "string"
      ? rawSummary
      : typeof rawSummary?.description === "string"
        ? rawSummary.description
        : typeof rawSummary?.descripcion_corta === "string"
          ? rawSummary.descripcion_corta
          : null;

  return {
    descripcionCorta: summary.descripcion_corta || null,
    lecturaRapida: lecturaRapida || summary.descripcion_corta || null,
    tipoPago: tipoPagoLabel(row?.tipo_pago ?? summary.tipo_pago),
    plazoDias: toNumber(summary.plazo_ejecucion_dias) ?? toNumber(row?.plazo_ejecucion_dias),
    entregables: toNumber(row?.entregables_count) ?? toNumber(summary.entregables_count) ?? 0,
    penalidadTopePct: toNumber(row?.penalidad_tope_pct) ?? toNumber(summary.penalidad_tope_pct),
    roles: dedupe([...toStringArray(row?.roles_requeridos), ...toStringArray(summary.roles_requeridos)]),
    documentosClave: toStringArray(summary.documentos_clave),
    observaciones: toStringArray(summary.observaciones_clave),
    monto: toNumber(row?.valor_estimado) ?? toNumber(summary.valor_estimado?.monto),
  };
}

export function plazoLabel(days: number | null): string | null {
  if (!days) return null;
  if (days % 30 === 0 && days >= 60) return `${days / 30} meses`;
  return `${days} días`;
}

export function compactMoney(monto: number | null): string | null {
  if (monto == null) return null;
  if (monto >= 1_000_000) return `S/ ${(monto / 1_000_000).toFixed(1)} M`;
  if (monto >= 10_000) return `S/ ${Math.round(monto / 1000)} mil`;
  return `S/ ${monto.toLocaleString("es-PE", { maximumFractionDigits: 0 })}`;
}

export const verdictShortLabels: Record<string, string> = {
  verde: "Cumples",
  ambar: "Te falta poco",
  rojo: "No conviene",
  gris: "Revisar",
};

/**
 * Fit de rubro: base 50 por segmento CUBSO + keywords ponderadas por
 * especificidad (frase exacta 15, término fuerte explícito 10, topes 30/45) + factor económico
 * moderado (±8: compara el monto de experiencia económica exigido contra la
 * facturación acreditable de la empresa; pesa poco porque un consorcio puede
 * cubrir el gap). Los puntos llegan calculados del SQL (`fit_points`).
 */
export function fitScore(fitPoints: number | null | undefined): number {
  return 50 + Math.min(50, Math.max(0, Number(fitPoints ?? 0)));
}

/** "Exacto" exige al menos 30 puntos; "relacionado", un término fuerte sobre la base CUBSO. */
export const FIT_EXACTO = 80;
export const FIT_RELACIONADO = 60;

export function fitLabel(fitPoints: number | null | undefined): string {
  const fit = fitScore(fitPoints);
  if (fit >= FIT_EXACTO) return "Tu rubro exacto";
  if (fit >= FIT_RELACIONADO) return "Muy relacionado";
  return "Rubro general";
}

/** Versión corta para celdas de tabla (la larga no entra sin truncarse). */
export function fitShortLabel(fitPoints: number | null | undefined): string {
  const fit = fitScore(fitPoints);
  if (fit >= FIT_EXACTO) return "Rubro exacto";
  if (fit >= FIT_RELACIONADO) return "Relacionado";
  return "General";
}

export type CotizacionStatus = {
  key: "abierta" | "por_abrir" | "cerrada" | "no_disponible";
  label: string;
};

/**
 * Estado operativo de cotización: combina el flag vivo `cotizar` de SEACE
 * con la ventana de fechas y el estado del contrato. Es LA señal para saber
 * si todavía se puede postular.
 */
export function cotizacionStatus(row: AnyRecord): CotizacionStatus {
  const now = Date.now();
  const ini = row?.fec_ini_cotizacion ? new Date(row.fec_ini_cotizacion).getTime() : null;
  const fin = row?.fec_fin_cotizacion ? new Date(row.fec_fin_cotizacion).getTime() : null;

  if (row?.estado_codigo != null && Number(row.estado_codigo) !== 2) {
    return { key: "cerrada", label: "Ya no disponible" };
  }
  if (fin != null && now > fin) {
    return { key: "cerrada", label: "Cotización cerrada" };
  }
  if (ini != null && now < ini) {
    const fecha = new Date(ini).toLocaleDateString("es-PE", { day: "2-digit", month: "2-digit", timeZone: "America/Lima" });
    return { key: "por_abrir", label: `Abre el ${fecha}` };
  }
  if (row?.cotizar === false) {
    return { key: "no_disponible", label: "No cotizable" };
  }
  return { key: "abierta", label: "Cotización abierta" };
}
