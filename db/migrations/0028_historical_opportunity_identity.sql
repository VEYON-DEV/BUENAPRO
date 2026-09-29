-- Historical PROD6 results use the same native id_contrato as the live table.
-- Reuse its canonical UUID when present; allocate one only for history-only IDs.
ALTER TABLE historical_contract_outcomes
  ADD COLUMN IF NOT EXISTS opportunity_id UUID REFERENCES opportunities(id);

CREATE INDEX IF NOT EXISTS ix_historical_outcomes_opportunity
  ON historical_contract_outcomes (opportunity_id);

UPDATE historical_contract_outcomes h
SET opportunity_id = s.opportunity_id
FROM opportunity_sources s
WHERE h.opportunity_id IS NULL
  AND s.source_key = 'seace_prod6'
  AND s.id_kind = 'id_contrato'
  AND s.external_id = h.id_contrato::text;

CREATE TEMP TABLE _historical_opportunity_backfill (
  id_contrato BIGINT PRIMARY KEY,
  opportunity_id UUID NOT NULL
) ON COMMIT DROP;

INSERT INTO _historical_opportunity_backfill (id_contrato, opportunity_id)
SELECT id_contrato, gen_random_uuid()
FROM historical_contract_outcomes
WHERE opportunity_id IS NULL;

INSERT INTO opportunities (
  id, record_kind, object_type, procurement_method, lifecycle_stage,
  participation_access, actionability, created_at, updated_at
)
SELECT
  b.opportunity_id,
  'opportunity',
  CASE h.objeto_codigo
    WHEN 1 THEN 'good' WHEN 2 THEN 'service'
    WHEN 3 THEN 'work' WHEN 4 THEN 'works_consulting'
    ELSE 'unknown' END,
  'minor_purchase',
  CASE h.estado_resultado
    WHEN 'ADJUDICADO' THEN 'awarded'
    WHEN 'DESIERTO' THEN 'closed'
    ELSE 'unknown' END,
  'registration_required',
  'informational',
  h.first_seen_at,
  h.updated_at
FROM _historical_opportunity_backfill b
JOIN historical_contract_outcomes h USING (id_contrato);

INSERT INTO opportunity_sources (
  opportunity_id, source_key, id_kind, external_id, native_values, created_at
)
SELECT
  b.opportunity_id,
  'seace_prod6',
  'id_contrato',
  h.id_contrato::text,
  jsonb_build_object(
    'codigo', h.codigo_completo,
    'objeto_codigo', h.objeto_codigo,
    'estado_resultado', h.estado_resultado
  ),
  h.first_seen_at
FROM _historical_opportunity_backfill b
JOIN historical_contract_outcomes h USING (id_contrato)
ON CONFLICT (source_key, id_kind, external_id) DO NOTHING;

UPDATE historical_contract_outcomes h
SET opportunity_id = s.opportunity_id
FROM opportunity_sources s
WHERE h.opportunity_id IS NULL
  AND s.source_key = 'seace_prod6'
  AND s.id_kind = 'id_contrato'
  AND s.external_id = h.id_contrato::text;

UPDATE opportunity_sources s
SET native_values = s.native_values || jsonb_build_object(
  'estado_resultado', h.estado_resultado,
  'objeto_codigo', h.objeto_codigo
)
FROM historical_contract_outcomes h
WHERE s.source_key = 'seace_prod6'
  AND s.id_kind = 'id_contrato'
  AND s.external_id = h.id_contrato::text;

UPDATE opportunities o
SET object_type = CASE h.objeto_codigo
      WHEN 1 THEN 'good' WHEN 2 THEN 'service'
      WHEN 3 THEN 'work' WHEN 4 THEN 'works_consulting'
      ELSE 'unknown' END,
    lifecycle_stage = CASE h.estado_resultado
      WHEN 'ADJUDICADO' THEN 'awarded'
      WHEN 'DESIERTO' THEN 'closed'
      ELSE 'unknown' END,
    actionability = 'informational',
    updated_at = now()
FROM historical_contract_outcomes h
WHERE h.opportunity_id = o.id;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM historical_contract_outcomes WHERE opportunity_id IS NULL) THEN
    RAISE EXCEPTION 'Historical outcome backfill left rows without canonical identity';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM historical_contract_outcomes h
    JOIN seace_contracts c USING (id_contrato)
    WHERE h.opportunity_id IS DISTINCT FROM c.opportunity_id
  ) THEN
    RAISE EXCEPTION 'Live and historical PROD6 identities diverged';
  END IF;
END;
$$;
