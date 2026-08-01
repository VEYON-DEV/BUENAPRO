ALTER TABLE historical_contract_outcomes
  ALTER COLUMN codigo_correlativo TYPE BIGINT;

COMMENT ON COLUMN historical_contract_outcomes.codigo_correlativo IS
  'Correlativo SEACE; algunos códigos institucionales superan el rango INTEGER.';
