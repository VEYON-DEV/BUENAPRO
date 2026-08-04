CREATE TABLE automation_rules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL UNIQUE REFERENCES company_profiles(id) ON DELETE CASCADE,
  enabled BOOLEAN NOT NULL DEFAULT true,
  min_fit_level SMALLINT NOT NULL DEFAULT 2 CHECK (min_fit_level BETWEEN 1 AND 3),
  min_notification_score SMALLINT NOT NULL DEFAULT 50 CHECK (min_notification_score BETWEEN 0 AND 99),
  max_daily_evaluations SMALLINT NOT NULL DEFAULT 0 CHECK (max_daily_evaluations BETWEEN 0 AND 200),
  min_hours_before_close SMALLINT NOT NULL DEFAULT 0 CHECK (min_hours_before_close BETWEEN 0 AND 168),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN automation_rules.min_fit_level IS
  'Nivel visual preliminar del feed: 1 general, 2 relacionado, 3 rubro exacto.';
COMMENT ON COLUMN automation_rules.min_notification_score IS
  'La alerta se envia solo cuando el score final es estrictamente mayor a este valor.';
COMMENT ON COLUMN automation_rules.max_daily_evaluations IS
  'Cero significa sin limite diario.';

CREATE TABLE telegram_integrations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
  token_ciphertext BYTEA NOT NULL,
  token_hint TEXT NOT NULL,
  bot_username TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE telegram_recipients (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  integration_id UUID NOT NULL REFERENCES telegram_integrations(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  last_tested_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (integration_id, chat_id)
);

ALTER TABLE notifications
  ADD COLUMN read_at TIMESTAMPTZ,
  ADD COLUMN telegram_recipient_id UUID REFERENCES telegram_recipients(id) ON DELETE SET NULL;
CREATE INDEX ix_notifications_user_unread
  ON notifications (user_id, created_at DESC)
  WHERE read_at IS NULL AND channel = 'in_app';
CREATE INDEX ix_notifications_telegram_recipient
  ON notifications (telegram_recipient_id, created_at DESC)
  WHERE telegram_recipient_id IS NOT NULL;

-- La nueva politica avisa resultados por encima de 50; ambar debe poder pasar
-- el filtro de canal y el umbral numerico decide finalmente.
UPDATE notification_preferences
SET min_verdict = 'ambar', max_alerts_per_day = 0, updated_at = now();

INSERT INTO notification_preferences (
  tenant_id, user_id, channel, enabled, mode, min_verdict, max_alerts_per_day
)
SELECT tm.tenant_id, tm.user_id, 'in_app', true, 'realtime', 'ambar', 0
FROM tenant_members tm
WHERE NOT EXISTS (
  SELECT 1
  FROM notification_preferences p
  WHERE p.tenant_id = tm.tenant_id
    AND p.user_id = tm.user_id
    AND p.channel = 'in_app'
    AND p.business_line_id IS NULL
);

-- Una sola fuente SQL para el nivel preliminar que usan el feed y el worker.
-- La funcion conserva stemming espanol, topes y factor economico del motor actual.
CREATE OR REPLACE FUNCTION profile_contract_fit(p_profile_id UUID, p_id_contrato BIGINT)
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
  SELECT cp.*,
    COALESCE((
      SELECT max((entry.value)::numeric)
      FROM jsonb_each_text(COALESCE(cp.econ_experience_json, '{}'::jsonb)) entry
      WHERE entry.value ~ '^[0-9]+\.?[0-9]*$'
    ), 0) AS econ_capacity
  FROM company_profiles cp
  WHERE cp.id = p_profile_id AND cp.is_active = true
), contract AS (
  SELECT c.* FROM seace_contracts c WHERE c.id_contrato = p_id_contrato
), doc AS (
  SELECT to_tsvector('spanish'::regconfig, COALESCE(c.descripcion, '')) AS value
  FROM contract c
), eligible_lines AS (
  SELECT bl.id, bl.nombre, bl.keyword_phrases, bl.keyword_terms, p.company_keywords
  FROM profile p
  JOIN business_lines bl ON bl.profile_id = p.id AND bl.is_active = true
  CROSS JOIN contract c
  WHERE c.cubso_segmento = ANY(bl.cubso_segmentos)
), phrase_signals AS (
  SELECT DISTINCT ON (el.id, phraseto_tsquery('spanish'::regconfig, phrase)::text)
    el.id, el.nombre, phrase AS original,
    phraseto_tsquery('spanish'::regconfig, phrase) AS query,
    tsvector_to_array(to_tsvector('spanish'::regconfig, phrase)) AS lexemes
  FROM eligible_lines el CROSS JOIN LATERAL unnest(el.keyword_phrases) phrase
  WHERE numnode(plainto_tsquery('spanish'::regconfig, phrase)) >= 2
  ORDER BY el.id, phraseto_tsquery('spanish'::regconfig, phrase)::text, phrase
), phrase_hits AS (
  SELECT s.id, s.nombre, s.original, s.query, s.lexemes, 15 AS points
  FROM phrase_signals s CROSS JOIN doc WHERE doc.value @@ s.query
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
  SELECT s.id, s.nombre, s.original, s.query, s.lexemes, 10 AS points
  FROM term_signals s CROSS JOIN doc
  WHERE doc.value @@ s.query
    AND NOT EXISTS (
      SELECT 1 FROM covered_lexemes covered
      WHERE covered.id = s.id AND covered.lexeme = ANY(s.lexemes)
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
      WHERE covered.id = s.id AND covered.lexeme = ANY(s.lexemes)
    )
    AND NOT EXISTS (
      SELECT 1 FROM term_hits term WHERE term.id = s.id AND term.query::text = s.query::text
    )
), company_hits AS (
  SELECT id, nombre, original, query, lexemes, 10 AS points
  FROM company_candidates WHERE rank = 1
), econ AS (
  SELECT max((rf.details_json->>'monto')::numeric) AS required
  FROM requirement_facets rf
  WHERE rf.id_contrato = p_id_contrato
    AND rf.facet = 'economic_experience'
    AND rf.is_current = true
    AND rf.details_json->>'monto' ~ '^[0-9]+\.?[0-9]*$'
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
), scored AS (
  SELECT ls.*,
    ls.keyword_points + CASE
      WHEN econ.required IS NULL THEN 0
      WHEN p.econ_capacity >= econ.required THEN 8
      WHEN p.econ_capacity >= econ.required * 0.5 THEN 3
      WHEN p.econ_capacity >= econ.required * 0.25 THEN -4
      ELSE -8
    END AS total_points
  FROM line_scores ls CROSS JOIN profile p CROSS JOIN econ
)
SELECT id, nombre, keyword_points, total_points::int,
  (50 + LEAST(50, GREATEST(0, total_points)))::int AS fit_score,
  CASE
    WHEN 50 + LEAST(50, GREATEST(0, total_points)) >= 80 THEN 3
    WHEN 50 + LEAST(50, GREATEST(0, total_points)) >= 60 THEN 2
    ELSE 1
  END::smallint AS fit_level,
  keyword_hits
FROM scored
ORDER BY keyword_points DESC, id
LIMIT 1;
$$;
