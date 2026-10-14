-- PROD2 schema validated with PostgreSQL scratch integration and rollback.
-- Verified listing captures only: no lifecycle inference, profile routing or jobs.
CREATE TABLE prod2_discovery_queries (
  query_hash TEXT PRIMARY KEY CHECK (query_hash ~ '^[0-9a-f]{64}$'),
  query_manifest JSONB NOT NULL CHECK (jsonb_typeof(query_manifest) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE prod2_discovery_runs (
  run_id TEXT PRIMARY KEY CHECK (length(run_id) BETWEEN 1 AND 120),
  query_hash TEXT NOT NULL REFERENCES prod2_discovery_queries(query_hash),
  total_results INTEGER NOT NULL CHECK (total_results BETWEEN 0 AND 498),
  total_pages INTEGER NOT NULL CHECK (total_pages >= 1),
  page_size INTEGER NOT NULL CHECK (page_size BETWEEN 1 AND 100),
  status TEXT NOT NULL DEFAULT 'partial' CHECK (status IN ('partial', 'complete')),
  first_observed_at TIMESTAMPTZ NOT NULL,
  last_observed_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  CHECK (last_observed_at >= first_observed_at),
  CHECK ((status = 'complete') = (completed_at IS NOT NULL)),
  CHECK (total_pages = GREATEST(1, (total_results + page_size - 1) / page_size))
);

CREATE TABLE prod2_discoveries (
  id_convocatoria BIGINT PRIMARY KEY CHECK (id_convocatoria > 0),
  id_procedimiento BIGINT NOT NULL CHECK (id_procedimiento > 0),
  opportunity_id UUID NOT NULL REFERENCES opportunities(id),
  nomenclature TEXT NOT NULL CHECK (length(btrim(nomenclature)) > 0),
  buyer_name TEXT NOT NULL CHECK (length(btrim(buyer_name)) > 0),
  description TEXT NOT NULL CHECK (length(btrim(description)) > 0),
  object_type TEXT NOT NULL CHECK (object_type IN ('good', 'service')),
  object_label TEXT NOT NULL CHECK (object_label IN ('Bien', 'Servicio')),
  published_at TIMESTAMPTZ NOT NULL,
  reference_amount NUMERIC(18, 2) CHECK (reference_amount IS NULL OR reference_amount >= 0),
  currency TEXT NOT NULL,
  seace_version TEXT NOT NULL CHECK (seace_version = '3'),
  row_hash TEXT NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  CHECK (last_seen_at >= first_seen_at),
  CHECK ((object_type = 'good' AND object_label = 'Bien')
      OR (object_type = 'service' AND object_label = 'Servicio'))
);
CREATE INDEX ix_prod2_discoveries_procedure ON prod2_discoveries(id_procedimiento);
CREATE INDEX ix_prod2_discoveries_opportunity ON prod2_discoveries(opportunity_id);

CREATE TABLE prod2_discovery_pages (
  run_id TEXT NOT NULL REFERENCES prod2_discovery_runs(run_id),
  page_number INTEGER NOT NULL CHECK (page_number > 0),
  observed_at TIMESTAMPTZ NOT NULL,
  page_hash TEXT NOT NULL CHECK (page_hash ~ '^[0-9a-f]{64}$'),
  row_count INTEGER NOT NULL CHECK (row_count BETWEEN 0 AND 100),
  -- Sanitized snapshots preserve each pass even when the latest row changes.
  rows_manifest JSONB NOT NULL CHECK (jsonb_typeof(rows_manifest) = 'array'),
  PRIMARY KEY (run_id, page_number),
  CHECK (jsonb_array_length(rows_manifest) = row_count)
);

CREATE TABLE prod2_discovery_page_rows (
  run_id TEXT NOT NULL,
  page_number INTEGER NOT NULL,
  row_number INTEGER NOT NULL CHECK (row_number > 0),
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria),
  PRIMARY KEY (run_id, page_number, row_number),
  UNIQUE (run_id, id_convocatoria),
  FOREIGN KEY (run_id, page_number) REFERENCES prod2_discovery_pages(run_id, page_number)
);

COMMENT ON TABLE prod2_discovery_runs IS
  'A bounded query pass. Complete means captured pages/IDs reconcile with reported total, not national coverage or current lifecycle.';
COMMENT ON TABLE prod2_discoveries IS
  'Latest sanitized listing observation by official convocatoria; preserves procedure linkage and does not establish vigente status.';
