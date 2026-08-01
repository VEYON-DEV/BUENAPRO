CREATE TABLE historical_backfill_progress (
  cubso_segmento VARCHAR(20) PRIMARY KEY,
  status VARCHAR(24) NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')),
  next_page INTEGER NOT NULL DEFAULT 1 CHECK (next_page > 0),
  page_size INTEGER NOT NULL DEFAULT 50 CHECK (page_size BETWEEN 1 AND 100),
  total_elements INTEGER NOT NULL DEFAULT 0 CHECK (total_elements >= 0),
  processed INTEGER NOT NULL DEFAULT 0 CHECK (processed >= 0),
  saved INTEGER NOT NULL DEFAULT 0 CHECK (saved >= 0),
  failed INTEGER NOT NULL DEFAULT 0 CHECK (failed >= 0),
  failed_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  started_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE historical_backfill_progress IS
  'Checkpoint reanudable del barrido de resultados culminados por segmento CUBSO.';
