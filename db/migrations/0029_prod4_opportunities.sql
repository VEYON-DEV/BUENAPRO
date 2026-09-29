-- PROD4 is a current-opportunities source, not a second historical contract table.
-- Keep its native procedure identity and item rows separate from PROD6 contracts.
CREATE TABLE IF NOT EXISTS prod4_processes (
  id_procedimiento BIGINT PRIMARY KEY,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id),
  id_convocatoria_pub BIGINT,
  numero_procedimiento TEXT,
  nomenclatura TEXT,
  title TEXT,
  description TEXT,
  object_type TEXT NOT NULL CHECK (object_type IN ('good', 'service')),
  procedure_type TEXT,
  buyer_name TEXT,
  buyer_id TEXT,
  region TEXT,
  published_at TIMESTAMPTZ,
  registration_closes_at TIMESTAMPTZ,
  proposals_start_at TIMESTAMPTZ,
  proposals_closes_at TIMESTAMPTZ,
  reference_amount NUMERIC(18, 2),
  currency TEXT,
  source_url TEXT NOT NULL,
  technology_relevant BOOLEAN NOT NULL DEFAULT false,
  technology_match_reason JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  missing_since TIMESTAMPTZ,
  raw_listing JSONB NOT NULL DEFAULT '[]'::jsonb,
  raw_detail JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT prod4_processes_source_identity UNIQUE (opportunity_id),
  CONSTRAINT prod4_processes_nonnegative_amount CHECK (reference_amount IS NULL OR reference_amount >= 0)
);

CREATE INDEX IF NOT EXISTS ix_prod4_processes_current_tech
  ON prod4_processes (object_type, proposals_closes_at, published_at DESC)
  WHERE technology_relevant = true AND missing_since IS NULL;
CREATE INDEX IF NOT EXISTS ix_prod4_processes_convocatoria
  ON prod4_processes (id_convocatoria_pub)
  WHERE id_convocatoria_pub IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_prod4_processes_last_seen
  ON prod4_processes (last_seen_at);
CREATE INDEX IF NOT EXISTS ix_prod4_processes_search
  ON prod4_processes USING GIN (
    to_tsvector('spanish', coalesce(nomenclatura, '') || ' ' || coalesce(description, '') || ' ' || coalesce(buyer_name, ''))
  );

CREATE TABLE IF NOT EXISTS prod4_items (
  id_procedimiento BIGINT NOT NULL REFERENCES prod4_processes(id_procedimiento) ON DELETE CASCADE,
  nro_item INTEGER NOT NULL CHECK (nro_item > 0),
  cubso_code TEXT,
  description TEXT,
  quantity NUMERIC,
  unit TEXT,
  raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (id_procedimiento, nro_item)
);
CREATE INDEX IF NOT EXISTS ix_prod4_items_cubso
  ON prod4_items (left(cubso_code, 2))
  WHERE cubso_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS prod4_documents (
  id_procedimiento BIGINT NOT NULL REFERENCES prod4_processes(id_procedimiento) ON DELETE CASCADE,
  codigo_alfresco UUID NOT NULL,
  name TEXT,
  document_type TEXT,
  extension TEXT,
  published_at TIMESTAMPTZ,
  source_url TEXT NOT NULL,
  raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (id_procedimiento, codigo_alfresco)
);

CREATE TABLE IF NOT EXISTS prod4_schedule (
  id_procedimiento BIGINT NOT NULL REFERENCES prod4_processes(id_procedimiento) ON DELETE CASCADE,
  position INTEGER NOT NULL CHECK (position >= 0),
  stage_key TEXT,
  stage_name TEXT,
  starts_at TIMESTAMPTZ,
  ends_at TIMESTAMPTZ,
  raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (id_procedimiento, position)
);

COMMENT ON COLUMN prod4_processes.technology_relevant IS
  'True only after the worker matched configured technology CUBSO segments; API listings exclude other sectors.';
COMMENT ON COLUMN prod4_processes.missing_since IS
  'Exited the complete PROD4 source snapshot; does not imply cancellation, award or contract.';
COMMENT ON COLUMN prod4_processes.source_url IS
  'Official PROD4 page; document links stay on the official SEACE host.';
