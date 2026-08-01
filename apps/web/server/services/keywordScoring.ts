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
    WITH doc AS (
      SELECT to_tsvector('spanish'::regconfig, coalesce(c.descripcion,'')) AS value
    ), eligible_lines AS (
      SELECT bl.id, bl.nombre, bl.keyword_phrases, bl.keyword_terms, cp.company_keywords
      FROM company_profiles cp
      JOIN business_lines bl ON bl.profile_id = cp.id AND bl.is_active = true
      WHERE cp.tenant_id = $${tenantParam} AND cp.is_active = true
        AND c.cubso_segmento = ANY(bl.cubso_segmentos)
    ), phrase_signals AS (
      SELECT DISTINCT ON (el.id, phraseto_tsquery('spanish'::regconfig, phrase)::text)
        el.id, el.nombre, phrase AS original,
        phraseto_tsquery('spanish'::regconfig, phrase) AS query,
        tsvector_to_array(to_tsvector('spanish'::regconfig, phrase)) AS lexemes
      FROM eligible_lines el CROSS JOIN LATERAL unnest(el.keyword_phrases) phrase
      WHERE numnode(plainto_tsquery('spanish'::regconfig, phrase)) >= 2
      ORDER BY el.id, phraseto_tsquery('spanish'::regconfig, phrase)::text, phrase
    ), phrase_hits AS (
      SELECT s.id, s.nombre, s.original, s.query, s.lexemes, ${scoring.exact_phrase}::int AS points
      FROM phrase_signals s CROSS JOIN doc
      WHERE doc.value @@ s.query
    ), covered_lexemes AS (
      SELECT DISTINCT ph.id, lexeme
      FROM phrase_hits ph CROSS JOIN LATERAL unnest(ph.lexemes) lexeme
    ), term_signals AS (
      SELECT DISTINCT ON (el.id, plainto_tsquery('spanish'::regconfig, term)::text)
        el.id, el.nombre, term AS original,
        plainto_tsquery('spanish'::regconfig, term) AS query,
        tsvector_to_array(to_tsvector('spanish'::regconfig, term)) AS lexemes
      FROM eligible_lines el CROSS JOIN LATERAL unnest(el.keyword_terms) term
      WHERE numnode(plainto_tsquery('spanish'::regconfig, term)) = 1
      ORDER BY el.id, plainto_tsquery('spanish'::regconfig, term)::text, term
    ), term_hits AS (
      SELECT s.id, s.nombre, s.original, s.query, s.lexemes, ${scoring.strong_term}::int AS points
      FROM term_signals s CROSS JOIN doc
      WHERE doc.value @@ s.query
        AND NOT EXISTS (
          SELECT 1 FROM covered_lexemes covered
          WHERE covered.id=s.id AND covered.lexeme=ANY(s.lexemes)
        )
    ), company_signals AS (
      SELECT DISTINCT ON (el.id, plainto_tsquery('spanish'::regconfig, keyword)::text)
        el.id, el.nombre, keyword AS original,
        plainto_tsquery('spanish'::regconfig, keyword) AS query,
        tsvector_to_array(to_tsvector('spanish'::regconfig, keyword)) AS lexemes,
        ordinality
      FROM eligible_lines el
      CROSS JOIN LATERAL unnest(el.company_keywords) WITH ORDINALITY AS signal(keyword, ordinality)
      WHERE numnode(plainto_tsquery('spanish'::regconfig, keyword)) BETWEEN 1 AND 3
      ORDER BY el.id, plainto_tsquery('spanish'::regconfig, keyword)::text, ordinality
    ), company_candidates AS (
      SELECT s.*, row_number() OVER (PARTITION BY s.id ORDER BY s.ordinality, s.original) AS rank
      FROM company_signals s CROSS JOIN doc
      WHERE doc.value @@ s.query
        AND NOT EXISTS (
          SELECT 1 FROM covered_lexemes covered
          WHERE covered.id=s.id AND covered.lexeme=ANY(s.lexemes)
        )
        AND NOT EXISTS (
          SELECT 1 FROM term_hits term
          WHERE term.id=s.id AND term.query::text=s.query::text
        )
    ), company_hits AS (
      SELECT id, nombre, original, query, lexemes, ${scoring.company_keyword}::int AS points
      FROM company_candidates WHERE rank=1
    ), line_scores AS (
      SELECT el.id AS business_line_id, el.nombre AS business_line_name,
        LEAST(${scoring.total_cap},
          COALESCE((SELECT sum(points) FROM phrase_hits ph WHERE ph.id=el.id),0)
          + LEAST(${scoring.term_cap}, COALESCE((SELECT sum(points) FROM term_hits th WHERE th.id=el.id),0))
          + LEAST(${scoring.company_cap}, COALESCE((SELECT sum(points) FROM company_hits ch WHERE ch.id=el.id),0))
        )::int AS keyword_points,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('keyword', original, 'match', 'exact_phrase', 'points', points) ORDER BY original) FROM phrase_hits ph WHERE ph.id=el.id),'[]'::jsonb)
        || COALESCE((SELECT jsonb_agg(jsonb_build_object('keyword', original, 'match', 'strong_term', 'points', points) ORDER BY original) FROM term_hits th WHERE th.id=el.id),'[]'::jsonb)
        || COALESCE((SELECT jsonb_agg(jsonb_build_object('keyword', original, 'match', 'company_keyword', 'points', points) ORDER BY original) FROM company_hits ch WHERE ch.id=el.id),'[]'::jsonb) AS keyword_hits
      FROM eligible_lines el
    )
    SELECT * FROM line_scores ORDER BY keyword_points DESC, business_line_id LIMIT 1
  ) fit ON true`;
}
