-- Canonical identity and independent classification axes for source records.
-- Legacy PROD6 IDs remain the primary keys of the existing operational tables.

CREATE TABLE IF NOT EXISTS opportunities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  record_kind TEXT NOT NULL DEFAULT 'opportunity'
    CHECK (record_kind IN (
      'opportunity', 'early_notice', 'market_sounding', 'purchase_order',
      'catalog_reference', 'other'
    )),
  object_type TEXT NOT NULL DEFAULT 'unknown'
    CHECK (object_type IN (
      'good', 'service', 'general_consulting', 'work',
      'works_consulting', 'other', 'unknown'
    )),
  procurement_method TEXT NOT NULL DEFAULT 'unknown'
    CHECK (procurement_method IN (
      'minor_purchase', 'selection_procedure', 'electronic_catalog',
      'other', 'unknown'
    )),
  lifecycle_stage TEXT NOT NULL DEFAULT 'unknown'
    CHECK (lifecycle_stage IN (
      'planned', 'pre_notice', 'open', 'evaluation', 'awarded',
      'contracted', 'closed', 'unknown'
    )),
  participation_access TEXT NOT NULL DEFAULT 'unknown'
    CHECK (participation_access IN (
      'public', 'registration_required', 'catalog_member',
      'invitation_only', 'tenant_private', 'unknown'
    )),
  actionability TEXT NOT NULL DEFAULT 'unknown'
    CHECK (actionability IN ('actionable', 'informational', 'unknown')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS opportunity_sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  source_key TEXT NOT NULL CHECK (length(btrim(source_key)) > 0),
  id_kind TEXT NOT NULL CHECK (length(btrim(id_kind)) > 0),
  external_id TEXT NOT NULL CHECK (length(btrim(external_id)) > 0),
  native_values JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_key, id_kind, external_id)
);

CREATE INDEX IF NOT EXISTS ix_opportunity_sources_opportunity
  ON opportunity_sources (opportunity_id);

ALTER TABLE seace_contracts
  ADD COLUMN IF NOT EXISTS opportunity_id UUID REFERENCES opportunities(id);

CREATE INDEX IF NOT EXISTS ix_seace_contracts_opportunity
  ON seace_contracts (opportunity_id);

-- Recover an existing identity first if a previous backfill was interrupted.
INSERT INTO opportunity_sources (
  opportunity_id, source_key, id_kind, external_id, native_values, created_at
)
SELECT
  c.opportunity_id,
  'seace_prod6',
  'id_contrato',
  c.id_contrato::text,
  jsonb_build_object(
    'codigo', c.codigo,
    'objeto_codigo', c.objeto_codigo,
    'estado_codigo', c.estado_codigo
  ),
  c.first_seen_at
FROM seace_contracts c
WHERE c.opportunity_id IS NOT NULL
ON CONFLICT (source_key, id_kind, external_id) DO NOTHING;

UPDATE seace_contracts c
SET opportunity_id = s.opportunity_id
FROM opportunity_sources s
WHERE c.opportunity_id IS NULL
  AND s.source_key = 'seace_prod6'
  AND s.id_kind = 'id_contrato'
  AND s.external_id = c.id_contrato::text;

CREATE TEMP TABLE _prod6_opportunity_backfill (
  id_contrato BIGINT PRIMARY KEY,
  opportunity_id UUID NOT NULL
) ON COMMIT DROP;

INSERT INTO _prod6_opportunity_backfill (id_contrato, opportunity_id)
SELECT id_contrato, gen_random_uuid()
FROM seace_contracts
WHERE opportunity_id IS NULL;

INSERT INTO opportunities (
  id, record_kind, object_type, procurement_method, lifecycle_stage,
  participation_access, actionability, created_at, updated_at
)
SELECT
  b.opportunity_id,
  'opportunity',
  CASE c.objeto_codigo
    WHEN 1 THEN 'good'
    WHEN 2 THEN 'service'
    WHEN 3 THEN 'work'
    WHEN 4 THEN 'works_consulting'
    ELSE 'unknown'
  END,
  'minor_purchase',
  CASE c.estado_codigo
    WHEN 2 THEN 'open'
    WHEN 3 THEN 'evaluation'
    WHEN 4 THEN 'closed'
    ELSE 'unknown'
  END,
  'registration_required',
  'unknown',
  c.first_seen_at,
  c.updated_at
FROM _prod6_opportunity_backfill b
JOIN seace_contracts c USING (id_contrato);

INSERT INTO opportunity_sources (
  opportunity_id, source_key, id_kind, external_id, native_values, created_at
)
SELECT
  b.opportunity_id,
  'seace_prod6',
  'id_contrato',
  c.id_contrato::text,
  jsonb_build_object(
    'codigo', c.codigo,
    'objeto_codigo', c.objeto_codigo,
    'estado_codigo', c.estado_codigo
  ),
  c.first_seen_at
FROM _prod6_opportunity_backfill b
JOIN seace_contracts c USING (id_contrato)
ON CONFLICT (source_key, id_kind, external_id) DO NOTHING;

UPDATE seace_contracts c
SET opportunity_id = s.opportunity_id
FROM opportunity_sources s
WHERE c.opportunity_id IS NULL
  AND s.source_key = 'seace_prod6'
  AND s.id_kind = 'id_contrato'
  AND s.external_id = c.id_contrato::text;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM seace_contracts WHERE opportunity_id IS NULL) THEN
    RAISE EXCEPTION 'PROD6 opportunity backfill left contracts without canonical identity';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM seace_contracts c
    LEFT JOIN opportunity_sources s
      ON s.source_key = 'seace_prod6'
      AND s.id_kind = 'id_contrato'
      AND s.external_id = c.id_contrato::text
    WHERE s.opportunity_id IS DISTINCT FROM c.opportunity_id
  ) THEN
    RAISE EXCEPTION 'PROD6 source identity does not match canonical contract identity';
  END IF;
END;
$$;

COMMENT ON COLUMN opportunities.actionability IS
  'A lifecycle stage alone does not establish that a supplier can currently act.';
COMMENT ON COLUMN opportunity_sources.native_values IS
  'Source-native classification and status values; full source payload remains in source-specific raw data.';
