/** Línea de negocio activa del perfil, tal como la lista de mayores la entrega para el selector. */
export type Prod2LineOption = { id: string; name: string; is_favorite: boolean };

type ListingRow = {
  keyword_points?: number | null;
  business_line_id?: string | null;
  business_line_name?: string | null;
  business_line_favorite?: boolean | null;
  keyword_hits?: Array<{ keyword?: string | null }> | null;
};

/** Lo que un enlace de la vista rápida conserva al fijar la línea o la palabra: el resto de filtros se reinicia. */
const DRILLDOWN_KEYS = ["profile_id", "deadline", "object", "q", "with_score"] as const;

/** Cuántas palabras caben en la fila antes de resumir el resto con «+N». */
export const ROW_KEYWORD_LIMIT = 3;

/** Enlace a la misma lista con filtros cambiados: conserva los demás, vuelve a la página 1 y quita lo que valga null. */
export function prod2ListingHref(current: Record<string, string>, changes: Record<string, string | null>) {
  const next = new URLSearchParams(current);
  next.set("source", "prod2");
  next.delete("page");
  for (const [key, value] of Object.entries(changes)) {
    if (value) next.set(key, value);
    else next.delete(key);
  }
  return `/feed?${next.toString()}`;
}

/** Filtra por una línea o por una palabra desde la vista rápida; solo conserva perfil, cierre, tipo, búsqueda y puntaje. */
export function prod2DrilldownHref(current: Record<string, string>, change: { line: string } | { kw: string }) {
  const next = new URLSearchParams({ source: "prod2" });
  for (const key of DRILLDOWN_KEYS) {
    if (current[key]) next.set(key, current[key]);
  }
  for (const [key, value] of Object.entries(change)) next.set(key, value);
  return `/feed?${next.toString()}`;
}

/** Una licitación solo pertenece a una línea si esa línea suma puntos; sin puntos no se muestra ninguna. */
export function rowLineTag(row: ListingRow) {
  const points = row.keyword_points;
  const name = row.business_line_name?.trim();
  if (typeof points !== "number" || !Number.isFinite(points) || points <= 0 || !name) return null;
  return { id: row.business_line_id || null, name, favorite: row.business_line_favorite === true };
}

/** Palabras que coinciden, sin repetir (ni por mayúsculas) y en el orden en que llegan; solo si la línea suma puntos. */
export function rowKeywordChips(row: ListingRow, limit = ROW_KEYWORD_LIMIT) {
  const all: string[] = [];
  if (rowLineTag(row)) {
    const seen = new Set<string>();
    for (const hit of row.keyword_hits ?? []) {
      const keyword = typeof hit?.keyword === "string" ? hit.keyword.trim() : "";
      const key = keyword.toLowerCase();
      if (!keyword || seen.has(key)) continue;
      seen.add(key);
      all.push(keyword);
    }
  }
  return { shown: all.slice(0, limit), extra: Math.max(all.length - limit, 0), all };
}
