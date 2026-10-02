-- Participation terms come only from document evidence. Existing opportunities
-- retain not_identified until a worker extracts an explicit clause.
ALTER TABLE opportunities
  ADD COLUMN IF NOT EXISTS consortium_status TEXT NOT NULL DEFAULT 'not_identified',
  ADD COLUMN IF NOT EXISTS subcontracting_status TEXT NOT NULL DEFAULT 'not_identified',
  ADD COLUMN IF NOT EXISTS participation_terms_json JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'opportunities'::regclass
      AND conname = 'opportunities_consortium_status_check'
  ) THEN
    ALTER TABLE opportunities
      ADD CONSTRAINT opportunities_consortium_status_check
      CHECK (consortium_status IN ('permitted', 'prohibited', 'conditional', 'not_identified'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'opportunities'::regclass
      AND conname = 'opportunities_subcontracting_status_check'
  ) THEN
    ALTER TABLE opportunities
      ADD CONSTRAINT opportunities_subcontracting_status_check
      CHECK (subcontracting_status IN ('permitted', 'prohibited', 'conditional', 'not_identified'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'opportunities'::regclass
      AND conname = 'opportunities_participation_terms_json_check'
  ) THEN
    ALTER TABLE opportunities
      ADD CONSTRAINT opportunities_participation_terms_json_check
      CHECK (jsonb_typeof(participation_terms_json) = 'object');
  END IF;
END;
$$;

COMMENT ON COLUMN opportunities.consortium_status IS
  'Document-backed consortium terms; not_identified means no explicit clause was identified and never implies permission or prohibition.';
COMMENT ON COLUMN opportunities.subcontracting_status IS
  'Document-backed subcontracting terms; not_identified means no explicit clause was identified and never implies permission or prohibition.';
COMMENT ON COLUMN opportunities.participation_terms_json IS
  'Worker-extracted participation evidence, clauses, page references and conditions. Empty until identified from documents; no retrospective inference.';
