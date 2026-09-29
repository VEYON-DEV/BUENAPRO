-- Document-derived matching for selection procedures. Preliminary affinity
-- remains separate: these rows exist only after an official PDF was read.
ALTER TABLE prod4_processes
  ADD COLUMN IF NOT EXISTS detail_fetched_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS document_analysis_status TEXT
    CHECK (document_analysis_status IN ('extracted', 'skipped')),
  ADD COLUMN IF NOT EXISTS document_analysis_reason TEXT,
  ADD COLUMN IF NOT EXISTS document_analysis_checked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS analyzed_document_code UUID;

CREATE TABLE IF NOT EXISTS prod4_document_extractions (
  id BIGSERIAL PRIMARY KEY,
  id_procedimiento BIGINT NOT NULL REFERENCES prod4_processes(id_procedimiento) ON DELETE CASCADE,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  codigo_alfresco UUID NOT NULL,
  sha256_original TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd NUMERIC(10, 6),
  raw_extraction_json JSONB NOT NULL,
  summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  requires_human_review BOOLEAN NOT NULL DEFAULT false,
  quality TEXT NOT NULL DEFAULT 'auto'
    CHECK (quality IN ('auto', 'reviewed', 'corrected', 'failed')),
  facet_count INTEGER NOT NULL DEFAULT 0 CHECK (facet_count >= 0),
  is_current BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prod4_extraction_current
  ON prod4_document_extractions (id_procedimiento) WHERE is_current;
CREATE INDEX IF NOT EXISTS ix_prod4_extraction_document_hash
  ON prod4_document_extractions (codigo_alfresco, sha256_original, prompt_version);

CREATE TABLE IF NOT EXISTS prod4_requirement_facets (
  id BIGSERIAL PRIMARY KEY,
  id_procedimiento BIGINT NOT NULL REFERENCES prod4_processes(id_procedimiento) ON DELETE CASCADE,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  extraction_id BIGINT REFERENCES prod4_document_extractions(id) ON DELETE SET NULL,
  facet TEXT NOT NULL,
  label TEXT NOT NULL,
  required BOOLEAN NOT NULL DEFAULT true,
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  facet_hash TEXT NOT NULL,
  is_current BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_prod4_facets_current
  ON prod4_requirement_facets (id_procedimiento) WHERE is_current;

CREATE TABLE IF NOT EXISTS opportunity_matches (
  id BIGSERIAL PRIMARY KEY,
  profile_id UUID NOT NULL REFERENCES company_profiles(id) ON DELETE CASCADE,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  business_line_id UUID REFERENCES business_lines(id) ON DELETE SET NULL,
  score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
  verdict TEXT NOT NULL CHECK (verdict IN ('verde', 'ambar', 'rojo', 'gris')),
  breakdown_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  missing_actions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  matched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (profile_id, opportunity_id)
);
CREATE INDEX IF NOT EXISTS ix_opportunity_matches_profile_score
  ON opportunity_matches (profile_id, score DESC, matched_at DESC);

COMMENT ON TABLE opportunity_matches IS
  'Final document-backed matching; preliminary profile affinity is separate.';
