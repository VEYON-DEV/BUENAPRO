-- Jev-before-promotion pilot. Not applied by this code change.
-- Decisions deliberately have no discovery FK: rejected candidates stay here.
CREATE TABLE IF NOT EXISTS prod2_jev_decision_cache (
  cache_key TEXT PRIMARY KEY CHECK (cache_key ~ '^[0-9a-f]{64}$'),
  convocation_id BIGINT NOT NULL CHECK (convocation_id > 0),
  model TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','done','uncertain')),
  claim_token UUID,
  claimed_at TIMESTAMPTZ,
  lease_until TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  result_json JSONB CHECK (result_json IS NULL OR jsonb_typeof(result_json)='object'),
  reused_evaluation_id BIGINT,
  blocked_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status<>'running' OR (claim_token IS NOT NULL AND lease_until IS NOT NULL)),
  CHECK (status<>'done' OR (result_json IS NOT NULL AND completed_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS prod2_accepted_run_members (
  run_id TEXT NOT NULL CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'),
  convocation_id BIGINT NOT NULL CHECK (convocation_id>0),
  request_hash TEXT NOT NULL REFERENCES prod2_jev_decision_cache(cache_key),
  accepted BOOLEAN NOT NULL,
  result_json JSONB NOT NULL CHECK (jsonb_typeof(result_json)='object'),
  opportunity_id UUID REFERENCES opportunities(id),
  query_manifest JSONB CHECK (query_manifest IS NULL OR jsonb_typeof(query_manifest)='object'),
  row_manifest JSONB CHECK (row_manifest IS NULL OR jsonb_typeof(row_manifest)='object'),
  observed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(run_id,convocation_id),
  CHECK (accepted OR opportunity_id IS NULL)
);
CREATE INDEX IF NOT EXISTS ix_prod2_accepted_run_members
  ON prod2_accepted_run_members(run_id,accepted,convocation_id);

COMMENT ON TABLE prod2_jev_decision_cache IS
  'Exact Jev request hash is a global semantic cache; origin convocation has no discovery FK. Uncertain requests never retry automatically.';
COMMENT ON TABLE prod2_accepted_run_members IS
  'Pilot selection membership, not full listing coverage. Only Jev affinity >=60 can be promoted; rejected membership contains no ficha/PDF.';
