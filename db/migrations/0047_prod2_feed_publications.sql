-- Explicit opt-in to expose selected listing metadata without inventing a ficha.
CREATE TABLE prod2_feed_publications (
  id_convocatoria BIGINT PRIMARY KEY REFERENCES prod2_discoveries(id_convocatoria),
  publication_run_id TEXT NOT NULL CHECK (publication_run_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'),
  source_row_hash TEXT NOT NULL CHECK (source_row_hash ~ '^[0-9a-f]{64}$'),
  selection_sha256 TEXT NOT NULL CHECK (selection_sha256 ~ '^[0-9a-f]{64}$'),
  reader_outcome_sha256 TEXT NOT NULL CHECK (reader_outcome_sha256 ~ '^[0-9a-f]{64}$'),
  registry_content_hash TEXT NOT NULL CHECK (registry_content_hash ~ '^[0-9a-f]{64}$'),
  reader_status TEXT NOT NULL CHECK (reader_status IN (
    'closed_without_reading', 'rejected_below_60', 'opening_not_confirmed', 'needs_review',
    'selection_needs_review', 'selection_complete', 'llm1_complete', 'documents_missing',
    'failed', 'retained_verification_failed', 'unknown')),
  accepted BOOLEAN,
  published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (reader_status NOT IN ('closed_without_reading', 'rejected_below_60', 'opening_not_confirmed')
         OR accepted IS FALSE)
);
COMMENT ON TABLE prod2_feed_publications IS
  'Explicit selected metadata publication. Join only if source_row_hash equals current discovery row_hash. Reader status is an audited historical observation, not current lifecycle, eligibility or a match score. published_at is publication in BuenaPro, never an official schedule date.';
