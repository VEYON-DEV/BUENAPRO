-- Resumable bounded PROD2 metadata loads. Not applied by this code change.
-- Discovery coverage refers only to the immutable configured scope.
CREATE TABLE IF NOT EXISTS prod2_batch_runs (
  run_id TEXT PRIMARY KEY CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'),
  manifest_hash TEXT NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
  frozen_manifest JSONB NOT NULL CHECK (jsonb_typeof(frozen_manifest) = 'object'),
  phase TEXT NOT NULL DEFAULT 'discovery' CHECK (phase IN ('discovery','enrichment','complete')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','blocked','complete')),
  coverage_status TEXT NOT NULL DEFAULT 'pending' CHECK (coverage_status IN ('pending','complete','blocked')),
  blocked_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  discovery_completed_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  CHECK ((status = 'complete') = (phase = 'complete')),
  CHECK (phase = 'discovery' OR coverage_status = 'complete')
);

CREATE TABLE IF NOT EXISTS prod2_batch_tasks (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES prod2_batch_runs(run_id) ON DELETE CASCADE,
  task_key TEXT NOT NULL CHECK (task_key ~ '^[0-9a-f]{64}$'),
  kind TEXT NOT NULL CHECK (kind IN ('discovery','enrich')),
  query_manifest JSONB,
  id_convocatoria BIGINT REFERENCES prod2_discoveries(id_convocatoria),
  parent_task_id BIGINT REFERENCES prod2_batch_tasks(id),
  discovery_run_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','complete','split','blocked')),
  owner_id TEXT,
  owner_token UUID,
  claimed_at TIMESTAMPTZ,
  lease_until TIMESTAMPTZ,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  snapshot_restarts INTEGER NOT NULL DEFAULT 0 CHECK (snapshot_restarts >= 0),
  checkpoint_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(checkpoint_json) = 'object'),
  result_json JSONB CHECK (result_json IS NULL OR jsonb_typeof(result_json) = 'object'),
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(run_id,task_key),
  CHECK ((kind='discovery' AND query_manifest IS NOT NULL AND id_convocatoria IS NULL AND discovery_run_id IS NOT NULL)
      OR (kind='enrich' AND query_manifest IS NULL AND id_convocatoria IS NOT NULL)),
  CHECK (status<>'running' OR (owner_id IS NOT NULL AND owner_token IS NOT NULL AND lease_until IS NOT NULL)),
  CHECK (status<>'split' OR kind='discovery')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prod2_batch_enrich_candidate
  ON prod2_batch_tasks(run_id,id_convocatoria) WHERE kind='enrich';
CREATE INDEX IF NOT EXISTS ix_prod2_batch_task_claim
  ON prod2_batch_tasks(run_id,kind,status,id);

CREATE TABLE IF NOT EXISTS prod2_batch_task_passes (
  task_id BIGINT NOT NULL REFERENCES prod2_batch_tasks(id) ON DELETE CASCADE,
  discovery_run_id TEXT NOT NULL UNIQUE,
  restart_number INTEGER NOT NULL CHECK (restart_number>=0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(task_id,restart_number)
);

CREATE TABLE IF NOT EXISTS prod2_batch_memberships (
  run_id TEXT NOT NULL REFERENCES prod2_batch_runs(run_id) ON DELETE CASCADE,
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria),
  row_hash TEXT NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
  row_manifest JSONB NOT NULL CHECK (jsonb_typeof(row_manifest) = 'object'),
  first_observed_at TIMESTAMPTZ NOT NULL,
  last_observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(run_id,id_convocatoria),
  CHECK (last_observed_at>=first_observed_at)
);

CREATE TABLE IF NOT EXISTS prod2_participation_assessments (
  id BIGSERIAL PRIMARY KEY,
  assessment_key TEXT NOT NULL UNIQUE CHECK (assessment_key ~ '^[0-9a-f]{64}$'),
  id_convocatoria BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria),
  detail_hash TEXT NOT NULL CHECK (detail_hash ~ '^[0-9a-f]{64}$'),
  as_of TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,
  policy_version TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'temporal_participation_window' CHECK (scope='temporal_participation_window'),
  state TEXT NOT NULL CHECK (state IN ('closed','adjudicado','cancelado','desierto','suspendido','unknown',
    'pending_registration','registration_open','registered_only','submission_open')),
  provider_registration_verified BOOLEAN NOT NULL DEFAULT false CHECK (provider_registration_verified=false),
  evidence_json JSONB NOT NULL CHECK (jsonb_typeof(evidence_json)='object'),
  is_current BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (valid_until IS NULL OR valid_until>as_of)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prod2_participation_current
  ON prod2_participation_assessments(id_convocatoria) WHERE is_current;

COMMENT ON TABLE prod2_batch_runs IS
  'Frozen configured scope; enrichment begins only after all discovery leaf partitions are complete. No national-coverage claim.';
COMMENT ON TABLE prod2_participation_assessments IS
  'Versioned temporal assessment tied to official ficha evidence; never updates canonical lifecycle or proves provider eligibility/registration.';
