-- Map-only PROD2 listing registry. No opportunities, jobs, Redis or provider claims.
-- Apply explicitly after review with apply-schema --go; never during discovery.
CREATE TABLE prod2_map_runs (
  run_id TEXT CONSTRAINT prod2_map_runs_pk PRIMARY KEY,
  manifest_hash TEXT NOT NULL,
  manifest_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT prod2_map_runs_id_ck CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'),
  CONSTRAINT prod2_map_runs_hash_ck CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT prod2_map_runs_json_ck CHECK (jsonb_typeof(manifest_json) = 'object')
);

CREATE TABLE prod2_map_run_queries (
  run_id TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  query_manifest JSONB NOT NULL,
  CONSTRAINT prod2_map_run_queries_pk PRIMARY KEY (run_id, query_hash),
  CONSTRAINT prod2_map_run_queries_run_fk FOREIGN KEY (run_id) REFERENCES prod2_map_runs(run_id),
  CONSTRAINT prod2_map_run_queries_hash_ck CHECK (query_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT prod2_map_run_queries_json_ck CHECK (jsonb_typeof(query_manifest) = 'object')
);

CREATE TABLE prod2_listing_registry (
  convocation_id BIGINT CONSTRAINT prod2_listing_registry_pk PRIMARY KEY,
  procedure_id BIGINT NOT NULL,
  content_hash TEXT NOT NULL,
  listing_json JSONB NOT NULL,
  first_observed_at TIMESTAMPTZ NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT prod2_listing_registry_identity_uk UNIQUE (convocation_id, procedure_id),
  CONSTRAINT prod2_listing_registry_ids_ck CHECK (convocation_id > 0 AND procedure_id > 0),
  CONSTRAINT prod2_listing_registry_hash_ck CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT prod2_listing_registry_json_ck CHECK (jsonb_typeof(listing_json) = 'object'),
  CONSTRAINT prod2_listing_registry_time_ck CHECK (first_observed_at <= observed_at)
);

CREATE TABLE prod2_listing_observations (
  convocation_id BIGINT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  content_hash TEXT NOT NULL,
  listing_json JSONB NOT NULL,
  CONSTRAINT prod2_listing_observations_pk PRIMARY KEY (convocation_id, observed_at),
  CONSTRAINT prod2_listing_observations_hash_uk UNIQUE (convocation_id, observed_at, content_hash),
  CONSTRAINT prod2_listing_observations_id_fk FOREIGN KEY (convocation_id)
    REFERENCES prod2_listing_registry(convocation_id),
  CONSTRAINT prod2_listing_observations_hash_ck CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT prod2_listing_observations_json_ck CHECK (jsonb_typeof(listing_json) = 'object')
);

CREATE TABLE prod2_map_run_members (
  run_id TEXT NOT NULL,
  convocation_id BIGINT NOT NULL,
  procedure_id BIGINT NOT NULL,
  first_classification TEXT NOT NULL,
  first_content_hash TEXT NOT NULL,
  first_observed_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT prod2_map_run_members_pk PRIMARY KEY (run_id, convocation_id),
  CONSTRAINT prod2_map_run_members_run_fk FOREIGN KEY (run_id) REFERENCES prod2_map_runs(run_id),
  CONSTRAINT prod2_map_run_members_identity_fk FOREIGN KEY (convocation_id, procedure_id)
    REFERENCES prod2_listing_registry(convocation_id, procedure_id),
  CONSTRAINT prod2_map_run_members_observation_fk
    FOREIGN KEY (convocation_id, first_observed_at, first_content_hash)
    REFERENCES prod2_listing_observations(convocation_id, observed_at, content_hash),
  CONSTRAINT prod2_map_run_members_class_ck CHECK (first_classification IN
    ('new_id', 'unchanged', 'changed', 'known_baseline_missing'))
);

CREATE TABLE prod2_map_query_observations (
  run_id TEXT NOT NULL,
  convocation_id BIGINT NOT NULL,
  query_hash TEXT NOT NULL,
  observed_query_hash TEXT NOT NULL,
  observed_query JSONB NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  content_hash TEXT NOT NULL,
  page_number INTEGER NOT NULL,
  is_duplicate BOOLEAN NOT NULL,
  CONSTRAINT prod2_map_query_observations_pk
    PRIMARY KEY (run_id, convocation_id, query_hash, observed_query_hash, observed_at),
  CONSTRAINT prod2_map_query_observations_member_fk FOREIGN KEY (run_id, convocation_id)
    REFERENCES prod2_map_run_members(run_id, convocation_id),
  CONSTRAINT prod2_map_query_observations_query_fk FOREIGN KEY (run_id, query_hash)
    REFERENCES prod2_map_run_queries(run_id, query_hash),
  CONSTRAINT prod2_map_query_observations_observation_fk
    FOREIGN KEY (convocation_id, observed_at, content_hash)
    REFERENCES prod2_listing_observations(convocation_id, observed_at, content_hash),
  CONSTRAINT prod2_map_query_observations_hash_ck CHECK (observed_query_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT prod2_map_query_observations_json_ck CHECK (jsonb_typeof(observed_query) = 'object'),
  CONSTRAINT prod2_map_query_observations_page_ck CHECK (page_number > 0)
);
CREATE INDEX prod2_map_query_observations_query_idx
  ON prod2_map_query_observations(run_id, query_hash, convocation_id);
CREATE INDEX IF NOT EXISTS prod2_jev_decision_cache_convocation_idx
  ON prod2_jev_decision_cache(convocation_id);
CREATE INDEX IF NOT EXISTS prod2_accepted_run_members_convocation_idx
  ON prod2_accepted_run_members(convocation_id);

COMMENT ON TABLE prod2_listing_registry IS
  'Permanent latest listing observation only. No published opportunity, semantic decision or current lifecycle claim.';
COMMENT ON TABLE prod2_map_run_members IS
  'First classification within a frozen map-only run, retained on duplicates. known_baseline_missing means a PROD2 historical ID had no registry comparison hash.';
COMMENT ON TABLE prod2_listing_observations IS
  'One listing hash per official convocatoria and source observation instant. Conflicting hashes at the same instant block the whole batch.';
COMMENT ON TABLE prod2_map_query_observations IS
  'Idempotent associations to frozen root queries and observed leaf windows. Duplicate flags describe new associations; replay attempts do not inflate counts.';
