-- Query progress only: no candidates, opportunities, model calls or lifecycle guesses.
CREATE TABLE IF NOT EXISTS prod2_keyword_sweeps (
  sweep_id TEXT PRIMARY KEY CHECK (sweep_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'),
  scope_hash TEXT NOT NULL CHECK (scope_hash ~ '^[0-9a-f]{64}$'),
  frozen_scope JSONB NOT NULL CHECK (jsonb_typeof(frozen_scope)='object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prod2_keyword_tasks (
  sweep_id TEXT NOT NULL REFERENCES prod2_keyword_sweeps(sweep_id),
  query_hash TEXT NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),
  query_manifest JSONB NOT NULL CHECK (jsonb_typeof(query_manifest)='object'),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','partial','blocked','complete')),
  owner_run_id TEXT,
  claim_token UUID,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts>=0),
  started_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_completed_at TIMESTAMPTZ,
  result_json JSONB CHECK (result_json IS NULL OR jsonb_typeof(result_json)='object'),
  last_complete_json JSONB CHECK (last_complete_json IS NULL OR jsonb_typeof(last_complete_json)='object'),
  PRIMARY KEY(sweep_id,query_hash),
  CHECK (status<>'running' OR (owner_run_id IS NOT NULL AND claim_token IS NOT NULL)),
  CHECK (status<>'complete' OR (last_completed_at IS NOT NULL AND last_complete_json IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_prod2_keyword_task_progress ON prod2_keyword_tasks(sweep_id,status);
COMMENT ON TABLE prod2_keyword_sweeps IS
  'An explicitly named frozen observation epoch. Reuse resumes; a new epoch refreshes mutable source data.';
COMMENT ON TABLE prod2_keyword_tasks IS
  'Used is not complete. Completion requires all native pages/partitions or verified zero evidence; no nationwide or lifecycle coverage claim.';
