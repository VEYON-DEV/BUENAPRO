import { commercialCategories, commercialTaxonomy, COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";

export function commercialFilterValues(defaults: Record<string, string>, key: string): string[] {
  const options = key === "commercial_category" ? commercialCategories.categories
    : key === "commercial_area" ? commercialTaxonomy.areas
    : key === "commercial_work" ? commercialTaxonomy.work_types
    : key === "commercial_specialty" ? commercialTaxonomy.specialties : [];
  const allowed = new Set(options.map(option => option.id));
  return [...new Set((defaults[key] ?? "").split(",").filter(value => allowed.has(value)))];
}

export function commercialSelectionCount(defaults: Record<string, string>): number {
  return ["commercial_category", "commercial_area", "commercial_work", "commercial_specialty"]
    .reduce((count, key) => count + commercialFilterValues(defaults, key).length, 0)
    + (defaults.commercial_status ? 1 : 0);
}

export function commercialDefaultsKey(defaults: Record<string, string>): string {
  return COMMERCIAL_FILTER_KEYS.map(key => defaults[key] ?? "").join("|");
}

/** A newly chosen category replaces bookmarked commercial criteria everywhere. */
export function commercialCategoryDraftDefaults(defaults: Record<string, string>, category: string): Record<string, string> {
  const next = { ...defaults };
  COMMERCIAL_FILTER_KEYS.forEach(key => delete next[key]);
  if (category) next.commercial_category = category;
  return next;
}

export function clearCommercialFiltersHref(defaults: Record<string, string>): string {
  const params = new URLSearchParams(defaults);
  COMMERCIAL_FILTER_KEYS.forEach(key => params.delete(key));
  ["page", "open_only", "closing_before"].forEach(key => params.delete(key));
  return `/feed${params.size ? `?${params.toString()}` : ""}`;
}

/** Keep old bookmarked combinations intact until the user chooses a category. */
export function commercialCategorySelection(defaults: Record<string, string>): string {
  const categories = commercialFilterValues(defaults, "commercial_category");
  const previous = ["commercial_area", "commercial_work", "commercial_specialty", "commercial_status"]
    .some(key => Boolean(defaults[key]));
  if (previous || categories.length > 1) return "previous";
  return categories[0] ?? "";
}
