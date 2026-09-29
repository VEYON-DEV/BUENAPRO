-- Preliminary, tenant-profile-specific affinity for PROD4 procedures.
-- This deliberately does not infer eligibility or a final 0-100 verdict from
-- the listing: those require evaluating the official documents and schedule.
CREATE OR REPLACE FUNCTION profile_prod4_fit(p_profile_id UUID, p_id_procedimiento BIGINT)
RETURNS TABLE (
  business_line_id UUID,
  business_line_name TEXT,
  keyword_points INT,
  fit_points INT,
  fit_score INT,
  fit_level SMALLINT,
  keyword_hits JSONB
)
LANGUAGE sql
STABLE
AS $$
WITH profile AS (
  SELECT cp.id, cp.company_keywords
  FROM company_profiles cp
  WHERE cp.id = p_profile_id AND cp.is_active = true
), procedure AS (
  SELECT p.id_procedimiento, p.title, p.description
  FROM prod4_processes p
  WHERE p.id_procedimiento = p_id_procedimiento
), source_items AS (
  SELECT i.description, left(i.cubso_code, 2) AS segment
  FROM prod4_items i
  WHERE i.id_procedimiento = p_id_procedimiento
), doc AS (
  SELECT to_tsvector('spanish'::regconfig,
    concat_ws(' ', pr.title, pr.description,
      (SELECT string_agg(si.description, ' ' ORDER BY si.description) FROM source_items si)
    )) AS value
  FROM procedure pr
), eligible_lines AS (
  SELECT bl.id, bl.nombre, bl.keyword_phrases, bl.keyword_terms, cp.company_keywords
  FROM profile cp
  JOIN business_lines bl ON bl.profile_id = cp.id AND bl.is_active = true
  WHERE EXISTS (
    SELECT 1 FROM source_items si
    WHERE si.segment = ANY(bl.cubso_segmentos)
  )
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
  SELECT el.id, el.nombre,
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
SELECT id, nombre, keyword_points, keyword_points AS fit_points,
  (50 + LEAST(50, GREATEST(0, keyword_points)))::int AS fit_score,
  CASE
    WHEN 50 + LEAST(50, GREATEST(0, keyword_points)) >= 80 THEN 3
    WHEN 50 + LEAST(50, GREATEST(0, keyword_points)) >= 60 THEN 2
    ELSE 1
  END::smallint AS fit_level,
  keyword_hits
FROM line_scores
ORDER BY keyword_points DESC, id
LIMIT 1;
$$;

COMMENT ON FUNCTION profile_prod4_fit(UUID, BIGINT) IS
  'Afinidad preliminar PROD4 por perfil: segmento CUBSO de ítems y señales léxicas de título, descripción e ítems. No es evaluación de requisitos ni puntaje final.';
