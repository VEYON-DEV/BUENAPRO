import { companyAnalysisConfig, normalizeSearchText } from "./companyAnalysis.ts";

const forbidden = new Set(companyAnalysisConfig.keyword_scoring.forbidden_terms);

export function cleanCompanyKeywords(value: unknown, max = 12) {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value
    .map((item) => String(item).trim().toLowerCase().replace(/\s+/g, " "))
    .filter((item) => {
      const normalized = normalizeSearchText(item);
      const words = normalized.split(" ").filter(Boolean);
      if (!normalized || words.length > 3 || (words.length === 1 && forbidden.has(normalized)) || seen.has(normalized)) return false;
      seen.add(normalized);
      return true;
    })
    .slice(0, max);
}
