export type KeywordAffinityInput = {
  keyword_points?: number | null;
  fit_points?: number | null;
  fit_level?: number | null;
  business_line_name?: string | null;
  keyword_hits?: Array<{ keyword: string; match?: string; points?: number }> | null;
};

/** Current profile keywords describe commercial affinity, never eligibility. */
export function keywordAffinitySummary(input: KeywordAffinityInput) {
  const points = input.keyword_points ?? input.fit_points;
  const level = input.fit_level;
  const lineName = input.business_line_name?.trim();
  if (points == null || !Number.isFinite(points) || points < 0 || points > 45
    || level == null || !Number.isInteger(level) || level < 1 || level > 3 || !lineName) return null;
  const label = level === 3 ? "Afinidad alta" : level === 2 ? "Afinidad media" : "Afinidad baja";
  const hits = [...new Set((input.keyword_hits ?? []).map(hit => hit.keyword.trim()).filter(Boolean))];
  const pointLabel = new Intl.NumberFormat("es-PE", { maximumFractionDigits: 1 }).format(points);
  const description = `${label}: ${pointLabel} de 45 puntos por palabras clave de ${lineName}. ${hits.length ? `Coincidencias: ${hits.join(", ")}.` : "Sin coincidencias de palabras clave."} No confirma que cumplas los requisitos.`;
  return { points, pointLabel, level, label, lineName, hits, description };
}
