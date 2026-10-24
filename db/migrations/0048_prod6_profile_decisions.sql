-- PROD6 menores: una decision por contrato x empresa x version de ficha/perfil/politica.
-- Se guardan las decisiones aprobadas Y las descartadas (y las pendientes de JEV):
-- un contrato con decision vigente nunca se reevalua ni se vuelve a leer.
-- No referencia seace_contracts: los rechazados y pendientes no se guardan como contratos
-- (la web lista seace_contracts); la fila lleva su propia foto del listado de busqueda.
CREATE TABLE IF NOT EXISTS prod6_profile_decisions (
  id BIGSERIAL PRIMARY KEY,
  id_contrato BIGINT NOT NULL,
  profile_id UUID NOT NULL REFERENCES company_profiles(id),
  codigo TEXT,
  cubso_segmento TEXT,
  objeto_codigo SMALLINT,
  fec_fin_cotizacion TIMESTAMPTZ,
  listing_json JSONB NOT NULL,
  contract_hash TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  prefilter_passed BOOLEAN NOT NULL,
  prefilter_reasons TEXT[] NOT NULL DEFAULT '{}',
  keyword_lines TEXT[] NOT NULL DEFAULT '{}',
  decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected', 'pending')),
  decided_by TEXT NOT NULL CHECK (decided_by IN ('prefilter', 'keywords', 'jev')),
  score NUMERIC(5,2) CHECK (score BETWEEN 0 AND 100),
  threshold NUMERIC(5,2),
  domain TEXT,
  subcategory TEXT,
  information TEXT,
  model TEXT,
  request_hash TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd NUMERIC(12,8),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  result_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (id_contrato, profile_id, contract_hash, profile_hash, policy_version)
);

CREATE INDEX IF NOT EXISTS ix_prod6_profile_decisions_profile_decision
  ON prod6_profile_decisions (profile_id, decision);
CREATE INDEX IF NOT EXISTS ix_prod6_profile_decisions_contract
  ON prod6_profile_decisions (id_contrato);

COMMENT ON TABLE prod6_profile_decisions IS
  'Decision de afinidad por contrato menor y empresa. Se guardan aprobadas, descartadas y pendientes para no reevaluar. La clave incluye el hash de la ficha de busqueda, el hash del perfil y la version de politica: si cambia alguno, se decide de nuevo.';
COMMENT ON COLUMN prod6_profile_decisions.keyword_lines IS
  'Lineas con coincidencia de palabras clave; solo una senal, no bloquea.';
COMMENT ON COLUMN prod6_profile_decisions.decision IS
  'pending: paso el prefiltro y espera afinidad JEV; no es un descarte.';
