ALTER TABLE historical_contract_outcomes
  ADD COLUMN IF NOT EXISTS department TEXT,
  ADD COLUMN IF NOT EXISTS province TEXT,
  ADD COLUMN IF NOT EXISTS district TEXT;

UPDATE historical_contract_outcomes
SET department = NULLIF(split_part(raw_detail_json #>> '{uitContratoItemProjectionList,0,nomDistritoExt}', '/', 1), ''),
    province = NULLIF(split_part(raw_detail_json #>> '{uitContratoItemProjectionList,0,nomDistritoExt}', '/', 2), ''),
    district = NULLIF(split_part(raw_detail_json #>> '{uitContratoItemProjectionList,0,nomDistritoExt}', '/', 3), '')
WHERE department IS NULL OR province IS NULL OR district IS NULL;

CREATE INDEX IF NOT EXISTS ix_historical_outcomes_department
  ON historical_contract_outcomes (department, fec_publica DESC);

CREATE INDEX IF NOT EXISTS ix_historical_outcomes_year_segment
  ON historical_contract_outcomes (codigo_anio, cubso_segmento);
