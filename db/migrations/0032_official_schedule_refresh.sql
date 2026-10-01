-- A partial schedule refresh must not pretend the whole detail was refreshed.
ALTER TABLE seace_contracts ADD COLUMN IF NOT EXISTS schedule_fetched_at TIMESTAMPTZ;
ALTER TABLE prod4_processes ADD COLUMN IF NOT EXISTS schedule_fetched_at TIMESTAMPTZ;
COMMENT ON COLUMN seace_contracts.schedule_fetched_at IS 'Last successful official schedule fetch; separate from full detail freshness';
COMMENT ON COLUMN prod4_processes.schedule_fetched_at IS 'Last successful official schedule fetch; separate from full detail freshness';
