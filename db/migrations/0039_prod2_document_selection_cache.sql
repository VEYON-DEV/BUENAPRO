-- Durable textual Jev selector receipts. Apply with the normal migration workflow.
CREATE TABLE IF NOT EXISTS prod2_document_selection_cache (
  request_hash TEXT PRIMARY KEY CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  convocation_id BIGINT NOT NULL REFERENCES prod2_discoveries(id_convocatoria),
  model TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','done','uncertain')),
  claim_token UUID NOT NULL,
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  lease_until TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  result_json JSONB CHECK (result_json IS NULL OR jsonb_typeof(result_json)='object'),
  blocked_reason TEXT,
  CHECK (status <> 'done' OR (result_json IS NOT NULL AND completed_at IS NOT NULL))
);
COMMENT ON TABLE prod2_document_selection_cache IS
  'Global textual request cache. Remap ordinal answers to current candidates before use. Expired/uncertain payments never retry automatically; original owner may reconcile a saved receipt.';
