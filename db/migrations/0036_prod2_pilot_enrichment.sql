-- PROD2 pilot enrichment; PostgreSQL scratch integration validated before application.
-- Requires 0035_prod2_discovery and the canonical opportunity/matching schema.
-- PROD2 evidence is never written into PROD4 source tables.

CREATE TABLE IF NOT EXISTS prod2_pilot_batches (
  batch_id TEXT PRIMARY KEY CHECK (batch_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'),
  requested_limit INTEGER NOT NULL DEFAULT 10 CHECK (requested_limit BETWEEN 1 AND 10),
  coverage TEXT NOT NULL DEFAULT 'pilot_selection' CHECK (coverage = 'pilot_selection'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prod2_pilot_observations (
  batch_id TEXT NOT NULL REFERENCES prod2_pilot_batches(batch_id) ON DELETE CASCADE,
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria) ON DELETE CASCADE,
  query_hash TEXT NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),
  query_manifest JSONB NOT NULL CHECK (jsonb_typeof(query_manifest) = 'object'),
  row_hash TEXT NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
  row_manifest JSONB NOT NULL CHECK (jsonb_typeof(row_manifest) = 'object'),
  observed_at TIMESTAMPTZ NOT NULL,
  coverage TEXT NOT NULL DEFAULT 'pilot_selection' CHECK (coverage = 'pilot_selection'),
  PRIMARY KEY (batch_id, id_convocatoria)
);

CREATE TABLE IF NOT EXISTS prod2_details (
  id_convocatoria BIGINT PRIMARY KEY REFERENCES prod2_discoveries(id_convocatoria) ON DELETE CASCADE,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  detail_hash TEXT NOT NULL CHECK (detail_hash ~ '^[0-9a-f]{64}$'),
  detail_fetched_at TIMESTAMPTZ NOT NULL,
  source_url TEXT NOT NULL,
  source_session_dependent BOOLEAN NOT NULL DEFAULT true,
  source_access_method TEXT NOT NULL DEFAULT 'native_row_navigation',
  registration_closes_at TIMESTAMPTZ,
  proposals_start_at TIMESTAMPTZ,
  proposals_closes_at TIMESTAMPTZ,
  schedule_json JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(schedule_json) = 'array'),
  item_states TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  buena_pro_json JSONB NOT NULL DEFAULT '{"award_result":"unknown"}'::jsonb
    CHECK (jsonb_typeof(buena_pro_json) = 'object'),
  document_inventory_total INTEGER CHECK (document_inventory_total >= 0),
  document_inventory_complete BOOLEAN NOT NULL DEFAULT false,
  raw_detail JSONB NOT NULL CHECK (jsonb_typeof(raw_detail) = 'object'),
  CHECK (NOT document_inventory_complete OR document_inventory_total IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_prod2_details_opportunity ON prod2_details(opportunity_id);

CREATE TABLE IF NOT EXISTS prod2_documents (
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_details(id_convocatoria) ON DELETE CASCADE,
  codigo_alfresco UUID NOT NULL,
  name TEXT NOT NULL,
  document_type TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  published_at TIMESTAMPTZ,
  published_at_text TEXT NOT NULL,
  source_url TEXT NOT NULL,
  portal_download_guest BOOLEAN NOT NULL DEFAULT false CHECK (portal_download_guest = false),
  is_listed BOOLEAN NOT NULL DEFAULT true,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (id_convocatoria, codigo_alfresco),
  CHECK (last_seen_at >= first_seen_at)
);

-- Immutable downloaded revisions: the same official UUID can return new bytes.
CREATE TABLE IF NOT EXISTS prod2_document_downloads (
  id_convocatoria BIGINT NOT NULL,
  codigo_alfresco UUID NOT NULL,
  sha256_original TEXT NOT NULL CHECK (sha256_original ~ '^[0-9a-f]{64}$'),
  bytes BIGINT NOT NULL CHECK (bytes > 0),
  content_type TEXT NOT NULL,
  format TEXT NOT NULL CHECK (format IN ('pdf', 'zip', 'ole', 'rar')),
  storage_key TEXT,
  downloaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id_convocatoria, codigo_alfresco, sha256_original),
  FOREIGN KEY (id_convocatoria, codigo_alfresco)
    REFERENCES prod2_documents(id_convocatoria, codigo_alfresco) ON DELETE CASCADE
);

-- Durable API reservations. request_hash covers the full actual provider input,
-- including profile, document hashes, prompts, and policy versions as applicable.
-- An expired running reservation becomes uncertain, never an automatic API retry.
CREATE TABLE IF NOT EXISTS prod2_evaluations (
  id BIGSERIAL PRIMARY KEY,
  evaluation_key TEXT NOT NULL UNIQUE CHECK (evaluation_key ~ '^[0-9a-f]{64}$'),
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('jev_affinity', 'gemini_extraction', 'gemini_match')),
  request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  model TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  profile_id UUID REFERENCES company_profiles(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'uncertain', 'failed')),
  claim_token UUID,
  claimed_at TIMESTAMPTZ,
  lease_until TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  result_json JSONB CHECK (result_json IS NULL OR jsonb_typeof(result_json) = 'object'),
  input_tokens INTEGER CHECK (input_tokens >= 0),
  output_tokens INTEGER CHECK (output_tokens >= 0),
  cost_usd NUMERIC(12, 8) CHECK (cost_usd >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status <> 'running' OR (claim_token IS NOT NULL AND lease_until IS NOT NULL)),
  CHECK (status <> 'done' OR (completed_at IS NOT NULL AND result_json IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_prod2_evaluations_candidate
  ON prod2_evaluations(id_convocatoria, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS prod2_document_extractions (
  id BIGSERIAL PRIMARY KEY,
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria) ON DELETE CASCADE,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  codigo_alfresco UUID NOT NULL,
  sha256_original TEXT NOT NULL,
  evaluation_id BIGINT NOT NULL UNIQUE REFERENCES prod2_evaluations(id),
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  input_tokens INTEGER CHECK (input_tokens >= 0),
  output_tokens INTEGER CHECK (output_tokens >= 0),
  cost_usd NUMERIC(12, 8) CHECK (cost_usd >= 0),
  raw_extraction_json JSONB NOT NULL CHECK (jsonb_typeof(raw_extraction_json) = 'object'),
  summary_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(summary_json) = 'object'),
  requires_human_review BOOLEAN NOT NULL DEFAULT false,
  quality TEXT NOT NULL DEFAULT 'auto' CHECK (quality IN ('auto', 'reviewed', 'corrected', 'failed')),
  facet_count INTEGER NOT NULL DEFAULT 0 CHECK (facet_count >= 0),
  is_current BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (id_convocatoria, codigo_alfresco, sha256_original)
    REFERENCES prod2_document_downloads(id_convocatoria, codigo_alfresco, sha256_original)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prod2_extraction_current
  ON prod2_document_extractions(id_convocatoria) WHERE is_current;

CREATE TABLE IF NOT EXISTS prod2_requirement_facets (
  id BIGSERIAL PRIMARY KEY,
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria) ON DELETE CASCADE,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  extraction_id BIGINT NOT NULL REFERENCES prod2_document_extractions(id) ON DELETE CASCADE,
  facet TEXT NOT NULL,
  label TEXT NOT NULL,
  required BOOLEAN NOT NULL DEFAULT true,
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  facet_hash TEXT NOT NULL CHECK (facet_hash ~ '^[0-9a-f]{64}$'),
  is_current BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_prod2_facets_current
  ON prod2_requirement_facets(id_convocatoria) WHERE is_current;

-- Binding for canonical matches: affinity is stored only in prod2_evaluations.
CREATE TABLE IF NOT EXISTS prod2_match_provenance (
  match_id BIGINT PRIMARY KEY REFERENCES opportunity_matches(id) ON DELETE CASCADE,
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria) ON DELETE CASCADE,
  extraction_id BIGINT NOT NULL REFERENCES prod2_document_extractions(id),
  evaluation_id BIGINT NOT NULL REFERENCES prod2_evaluations(id),
  profile_hash TEXT NOT NULL CHECK (profile_hash ~ '^[0-9a-f]{64}$'),
  matched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE prod2_evaluations IS
  'Separate preliminary affinity, extraction and final matching; commit reservations before remote API calls.';
COMMENT ON COLUMN prod2_documents.source_url IS
  'Stable official JSON/JSONP resolver; not a binary download URL. Never persist alf_ticket or JSF session state.';
COMMENT ON COLUMN prod2_details.buena_pro_json IS
  'Observed stage/date/documents are separate from confirmed winner/award result. Default result is unknown.';
