import { companyAnalysisConfig, normalizeSearchText } from "./companyAnalysis.ts";

const scoring = companyAnalysisConfig.keyword_scoring;

export type KeywordHit = { keyword: string; match: "exact_phrase" | "strong_term" | "company_keyword"; points: number };

function uniqueNormalized(values: string[]) {
  return [...new Set(values.map(normalizeSearchText).filter(Boolean))];
}

/** Referencia ligera para reglas de topes; el stemming autoritativo vive en PostgreSQL. */
export function scoreBusinessLine(description: string, keywordPhrases: string[], keywordTerms: string[], companyKeywords: string[] = []) {
  const document = normalizeSearchText(description);
  const paddedDocument = ` ${document} `;
  const matchedPhrases = uniqueNormalized(keywordPhrases)
    .filter((phrase) => phrase.includes(" ") && paddedDocument.includes(` ${phrase} `));
  const wordsCoveredByPhrases = new Set(matchedPhrases.flatMap((phrase) => phrase.split(" ")));
  const termHits = uniqueNormalized(keywordTerms)
    .filter((term) => !term.includes(" "))
    .filter((term) => !wordsCoveredByPhrases.has(term) && paddedDocument.includes(` ${term} `))
    .map((keyword) => ({ keyword, match: "strong_term" as const, points: scoring.strong_term }));
  const lineWords = new Set(termHits.map((hit) => hit.keyword));
  const companyHit = uniqueNormalized(companyKeywords)
    .find((keyword) => !lineWords.has(keyword) && !keyword.split(" ").some((word) => wordsCoveredByPhrases.has(word)) && paddedDocument.includes(` ${keyword} `));
  const phraseHits = matchedPhrases.map((keyword) => ({ keyword, match: "exact_phrase" as const, points: scoring.exact_phrase }));
  const companyHits = companyHit ? [{ keyword: companyHit, match: "company_keyword" as const, points: scoring.company_keyword }] : [];
  const phrasePoints = phraseHits.reduce((total, hit) => total + hit.points, 0);
  const termPoints = Math.min(scoring.term_cap, termHits.reduce((total, hit) => total + hit.points, 0));
  const companyPoints = Math.min(scoring.company_cap, companyHits.reduce((total, hit) => total + hit.points, 0));
  return { points: Math.min(scoring.total_cap, phrasePoints + termPoints + companyPoints), hits: [...phraseHits, ...termHits, ...companyHits] };
}

/** Calcula una sola vez el mejor fit entre las líneas del segmento del contrato. */
export function keywordFitLateralSql(tenantParam: number) {
  return `
  LEFT JOIN LATERAL (
    SELECT fit.*
    FROM company_profiles cp
    CROSS JOIN LATERAL profile_contract_fit(cp.id,c.id_contrato) fit
    WHERE cp.tenant_id = $${tenantParam} AND cp.is_active = true
    ORDER BY fit.fit_score DESC,fit.business_line_id
    LIMIT 1
  ) fit ON true`;
}
