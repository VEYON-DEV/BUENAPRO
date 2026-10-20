-- Preliminary navigation labels from complete listing titles. No extraction or match mutation.
CREATE TABLE prod2_title_category_runs (
  run_id TEXT PRIMARY KEY CHECK (length(run_id) BETWEEN 1 AND 120),
  manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  manifest_json JSONB NOT NULL CHECK (jsonb_typeof(manifest_json) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE prod2_title_category_receipts (
  request_hash TEXT PRIMARY KEY CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  normalized_title TEXT NOT NULL CHECK (length(btrim(normalized_title)) > 0),
  title_sha256 TEXT NOT NULL CHECK (title_sha256 ~ '^[0-9a-f]{64}$'),
  model TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  catalog_sha256 TEXT NOT NULL CHECK (catalog_sha256 ~ '^[0-9a-f]{64}$'),
  prompt_sha256 TEXT NOT NULL CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
  request_json JSONB NOT NULL CHECK (jsonb_typeof(request_json) = 'object'),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'running', 'done', 'uncertain', 'needs_review')),
  claim_token UUID NOT NULL,
  claimed_run_id TEXT NOT NULL REFERENCES prod2_title_category_runs(run_id),
  request_sent BOOLEAN NOT NULL DEFAULT false,
  response_json JSONB,
  result_json JSONB,
  input_tokens BIGINT CHECK (input_tokens >= 0),
  output_tokens BIGINT CHECK (output_tokens >= 0),
  cost_usd NUMERIC(18,12) CHECK (cost_usd >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  CHECK (status <> 'done' OR (request_sent AND response_json IS NOT NULL AND result_json IS NOT NULL
    AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND cost_usd IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX ix_prod2_title_receipts_run ON prod2_title_category_receipts(claimed_run_id, status);

CREATE TABLE prod2_title_classifications (
  convocation_id BIGINT PRIMARY KEY REFERENCES prod2_discoveries(id_convocatoria),
  source_title TEXT NOT NULL CHECK (length(btrim(source_title)) > 0),
  source_title_sha256 TEXT NOT NULL CHECK (source_title_sha256 ~ '^[0-9a-f]{64}$'),
  request_hash TEXT NOT NULL REFERENCES prod2_title_category_receipts(request_hash),
  categories TEXT[] NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('classified', 'other', 'unclear')),
  policy_version TEXT NOT NULL CHECK (policy_version = 'title_commercial_classification_v1'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (categories <@ ARRAY['licencias_suscripciones', 'desarrollo_software', 'ia_automatizacion',
    'industrial', 'cloud_infraestructura', 'soporte_mantenimiento']::TEXT[]),
  CHECK ((decision = 'classified') = (cardinality(categories) > 0))
);
CREATE INDEX ix_prod2_title_categories_gin ON prod2_title_classifications USING GIN(categories);
COMMENT ON TABLE prod2_title_classifications IS
  'Preliminary title-only navigation labels. Join only when source_title equals current discovery description; documentary classification takes precedence. Not eligibility or a match score.';
