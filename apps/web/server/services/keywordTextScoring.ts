/** Keyword affinity uses the same Spanish lexical rules as profile_contract_fit.
 * It does not infer a CUBSO segment, economic capacity or documentary eligibility.
 */
export type KeywordTextFit = {
  business_line_id: string;
  business_line_name: string;
  /** Línea prioritaria marcada por la empresa; solo ordena y filtra, nunca puntúa. */
  business_line_favorite: boolean;
  keyword_points: number;
  fit_points: number;
  fit_level: 1 | 2 | 3;
  keyword_hits: Array<{ keyword: string; match: "exact_phrase" | "strong_term" | "company_keyword"; points: number }>;
};

/**
 * The caller must resolve the profile's tenant ownership before using its UUID.
 * descriptionSql is a fixed internal SQL expression, never user-supplied text.
 * Text itself remains in the source column or a bound parameter. Results are
 * computed from current profile/line keywords on each read, without a cache.
 */
export function keywordTextFitLateralSql(profileParam: number, descriptionSql: string) {
  if (!Number.isSafeInteger(profileParam) || profileParam < 1) {
    throw new TypeError("Profile parameter must be a positive integer");
  }
  if (!descriptionSql.trim()) throw new TypeError("Description SQL expression is required");
  return `
  LEFT JOIN LATERAL (
    WITH profile AS (
      SELECT cp.id, cp.company_keywords
      FROM company_profiles cp
      WHERE cp.id = $${profileParam}::uuid AND cp.is_active = true
    ), doc AS (
      SELECT to_tsvector('spanish'::regconfig, COALESCE(${descriptionSql}, '')) AS value
    ), eligible_lines AS (
      SELECT bl.id, bl.nombre, bl.is_favorite, bl.keyword_phrases, bl.keyword_terms, cp.company_keywords
      FROM profile cp
      JOIN business_lines bl ON bl.profile_id = cp.id AND bl.is_active = true
    ), phrase_signals AS (
      SELECT DISTINCT ON (el.id, phraseto_tsquery('spanish'::regconfig, phrase)::text)
        el.id, phrase AS original,
        phraseto_tsquery('spanish'::regconfig, phrase) AS query,
        tsvector_to_array(to_tsvector('spanish'::regconfig, phrase)) AS lexemes
      FROM eligible_lines el CROSS JOIN LATERAL unnest(el.keyword_phrases) phrase
      WHERE numnode(plainto_tsquery('spanish'::regconfig, phrase)) >= 2
      ORDER BY el.id, phraseto_tsquery('spanish'::regconfig, phrase)::text, phrase
    ), phrase_hits AS (
      SELECT s.id, s.original, s.query, s.lexemes, 15 AS points
      FROM phrase_signals s CROSS JOIN doc WHERE doc.value @@ s.query
    ), covered_lexemes AS (
      SELECT DISTINCT ph.id, lexeme
      FROM phrase_hits ph CROSS JOIN LATERAL unnest(ph.lexemes) lexeme
    ), term_signals AS (
      SELECT DISTINCT ON (el.id, plainto_tsquery('spanish'::regconfig, term)::text)
        el.id, term AS original,
        plainto_tsquery('spanish'::regconfig, term) AS query,
        tsvector_to_array(to_tsvector('spanish'::regconfig, term)) AS lexemes
      FROM eligible_lines el CROSS JOIN LATERAL unnest(el.keyword_terms) term
      WHERE numnode(plainto_tsquery('spanish'::regconfig, term)) = 1
      ORDER BY el.id, plainto_tsquery('spanish'::regconfig, term)::text, term
    ), term_hits AS (
      SELECT s.id, s.original, s.query, s.lexemes, 10 AS points
      FROM term_signals s CROSS JOIN doc
      WHERE doc.value @@ s.query
        AND NOT EXISTS (
          SELECT 1 FROM covered_lexemes covered
          WHERE covered.id = s.id AND covered.lexeme = ANY(s.lexemes)
        )
    ), company_signals AS (
      SELECT DISTINCT ON (el.id, plainto_tsquery('spanish'::regconfig, keyword)::text)
        el.id, keyword AS original,
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
          WHERE covered.id = s.id AND covered.lexeme = ANY(s.lexemes)
        )
        AND NOT EXISTS (
          SELECT 1 FROM term_hits term WHERE term.id = s.id AND term.query::text = s.query::text
        )
    ), company_hits AS (
      SELECT id, original, query, lexemes, 10 AS points
      FROM company_candidates WHERE rank = 1
    ), line_scores AS (
      SELECT el.id, el.nombre, el.is_favorite,
        LEAST(45,
          COALESCE((SELECT sum(points) FROM phrase_hits ph WHERE ph.id = el.id), 0)
          + LEAST(30, COALESCE((SELECT sum(points) FROM term_hits th WHERE th.id = el.id), 0))
          + LEAST(10, COALESCE((SELECT sum(points) FROM company_hits ch WHERE ch.id = el.id), 0))
        )::int AS keyword_points,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('keyword', original, 'match', 'exact_phrase', 'points', points) ORDER BY original) FROM phrase_hits ph WHERE ph.id = el.id), '[]'::jsonb)
        || COALESCE((SELECT jsonb_agg(jsonb_build_object('keyword', original, 'match', 'strong_term', 'points', points) ORDER BY original) FROM term_hits th WHERE th.id = el.id), '[]'::jsonb)
        || COALESCE((SELECT jsonb_agg(jsonb_build_object('keyword', original, 'match', 'company_keyword', 'points', points) ORDER BY original) FROM company_hits ch WHERE ch.id = el.id), '[]'::jsonb) AS keyword_hits
      FROM eligible_lines el
    )
    SELECT id AS business_line_id, nombre AS business_line_name, is_favorite AS business_line_favorite,
      keyword_points, keyword_points AS fit_points,
      CASE WHEN keyword_points >= 30 THEN 3 WHEN keyword_points >= 10 THEN 2 ELSE 1 END::smallint AS fit_level,
      keyword_hits
    FROM line_scores
    ORDER BY keyword_points DESC, is_favorite DESC, id
    LIMIT 1
  ) fit ON true`;
}
