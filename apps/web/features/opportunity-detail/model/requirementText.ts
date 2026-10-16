type Requirement = Record<string, any>;

function label(row: Requirement): string {
  return String(row.requisito ?? row.label ?? "").trim();
}

/** Source indexes can refer to a different facet order in historical matches. */
export function requirementSourceFacet(row: Requirement, facets: Requirement[]): Requirement | undefined {
  const title = label(row);
  if (!title) return undefined;
  const matches = facets.filter(facet => label(facet) === title
    && (!row.source_facet || facet.facet === row.source_facet));
  return matches.length === 1 ? matches[0] : undefined;
}

/** Read the stored source description, without shortening it or regenerating analysis. */
export function requirementFullText(row: Requirement, facets: Requirement[]): string | null {
  const source = requirementSourceFacet(row, facets);
  const details = source?.details_json ?? source?.details;
  for (const value of [details?.descripcion, details?.description, details?.texto, details?.value]) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}
