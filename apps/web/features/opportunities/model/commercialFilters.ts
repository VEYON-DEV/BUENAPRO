import { commercialTaxonomy, COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";

export function commercialFilterValues(defaults: Record<string, string>, key: string): string[] {
  const options = key === "commercial_area" ? commercialTaxonomy.areas
    : key === "commercial_work" ? commercialTaxonomy.work_types
    : key === "commercial_specialty" ? commercialTaxonomy.specialties : [];
  const allowed = new Set(options.map(option => option.id));
  return [...new Set((defaults[key] ?? "").split(",").filter(value => allowed.has(value)))];
}

export function commercialSelectionCount(defaults: Record<string, string>): number {
  return ["commercial_area", "commercial_work", "commercial_specialty"]
    .reduce((count, key) => count + commercialFilterValues(defaults, key).length, 0)
    + (defaults.commercial_status ? 1 : 0);
}

export function commercialDefaultsKey(defaults: Record<string, string>): string {
  return COMMERCIAL_FILTER_KEYS.map(key => defaults[key] ?? "").join("|");
}

export function clearCommercialFiltersHref(defaults: Record<string, string>): string {
  const params = new URLSearchParams(defaults);
  COMMERCIAL_FILTER_KEYS.forEach(key => params.delete(key));
  ["page", "open_only", "closing_before"].forEach(key => params.delete(key));
  return `/feed${params.size ? `?${params.toString()}` : ""}`;
}
