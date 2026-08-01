import { normalizeSearchText } from "./companyAnalysis.ts";

export type HistoricalCandidate = {
  id_contrato: number;
  codigo_completo: string;
  descripcion: string;
  cubso_item?: string | null;
  cubso_segmento?: string | null;
  entity_name?: string | null;
  estado_resultado: "ADJUDICADO" | "DESIERTO" | "SIN_RESULTADO";
  supplier_ruc?: string | null;
  supplier_name?: string | null;
  precio_total?: number | string | null;
  fec_publica?: string | Date | null;
  source_document_url?: string | null;
};

export type ComparableContext = {
  cubsoItem?: string | null;
  cubsoSegment?: string | null;
  entityName?: string | null;
  keywordHits: Array<{ keyword: string; match: string }>;
  now?: Date;
};

function hasPhrase(document: string, phrase: string) {
  return ` ${document} `.includes(` ${normalizeSearchText(phrase)} `);
}

export function scoreHistoricalCandidate(candidate: HistoricalCandidate, context: ComparableContext) {
  let score = 0;
  const reasons: string[] = [];
  const keywordMatches: string[] = [];
  const candidateCubso = String(candidate.cubso_item ?? "");
  const currentCubso = String(context.cubsoItem ?? "");

  if (candidateCubso && currentCubso && candidateCubso === currentCubso) {
    score += 50;
    reasons.push("Mismo código CUBSO");
  } else if (candidateCubso && currentCubso && candidateCubso.slice(0, 4) === currentCubso.slice(0, 4)) {
    score += 25;
    reasons.push("Misma familia CUBSO");
  }

  const document = normalizeSearchText(`${candidate.descripcion} ${candidate.entity_name ?? ""}`);
  const phraseHits = context.keywordHits
    .filter((hit) => hit.match === "exact_phrase" && hasPhrase(document, hit.keyword))
    .slice(0, 3);
  const termHits = context.keywordHits
    .filter((hit) => hit.match !== "exact_phrase" && hasPhrase(document, hit.keyword))
    .slice(0, 4);
  score += Math.min(45, phraseHits.length * 15);
  score += Math.min(32, termHits.length * 8);
  keywordMatches.push(...phraseHits.map((hit) => hit.keyword), ...termHits.map((hit) => hit.keyword));
  if (keywordMatches.length) reasons.push(`Coincide en ${keywordMatches.join(", ")}`);

  if (
    candidate.entity_name &&
    context.entityName &&
    normalizeSearchText(candidate.entity_name) === normalizeSearchText(context.entityName)
  ) {
    score += 10;
    reasons.push("Misma entidad");
  }
  const published = candidate.fec_publica ? new Date(candidate.fec_publica) : null;
  const now = context.now ?? new Date();
  if (published && Number.isFinite(published.getTime()) && now.getTime() - published.getTime() <= 730 * 86_400_000) {
    score += 5;
    reasons.push("Resultado reciente");
  }
  return { ...candidate, score, reasons, keyword_matches: keywordMatches };
}

function percentile(sorted: number[], position: number) {
  if (!sorted.length) return null;
  const index = (sorted.length - 1) * position;
  const lower = Math.floor(index);
  const upper = Math.ceil(index);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (index - lower);
}

function frequent(values: Array<string | null | undefined>, limit = 5) {
  const counts = new Map<string, number>();
  for (const value of values) {
    if (value) counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
    .slice(0, limit);
}

export function summarizeComparables(comparables: ReturnType<typeof scoreHistoricalCandidate>[]) {
  const adjudicated = comparables.filter((row) => row.estado_resultado === "ADJUDICADO");
  const deserted = comparables.filter((row) => row.estado_resultado === "DESIERTO");
  const prices = adjudicated
    .map((row) => Number(row.precio_total))
    .filter((price) => Number.isFinite(price) && price > 0)
    .sort((a, b) => a - b);
  return {
    total_count: comparables.length,
    adjudicados_count: adjudicated.length,
    desiertos_count: deserted.length,
    desiertos_pct: comparables.length ? Math.round((deserted.length / comparables.length) * 100) : 0,
    precio_min: prices[0] ?? null,
    precio_median: percentile(prices, 0.5),
    precio_max: prices.at(-1) ?? null,
    rango_frecuente: { min: percentile(prices, 0.25), max: percentile(prices, 0.75) },
    proveedores_frecuentes: frequent(adjudicated.map((row) => row.supplier_name)),
    entidades_frecuentes: frequent(comparables.map((row) => row.entity_name)),
  };
}
