-- Oportunidades guardadas por workspace. Guardar no implica seguir ni postular.

CREATE TABLE saved_contracts (
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  id_contrato BIGINT NOT NULL REFERENCES seace_contracts(id_contrato) ON DELETE CASCADE,
  saved_by UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id_contrato)
);

CREATE INDEX ix_saved_contracts_tenant_created
  ON saved_contracts (tenant_id, created_at DESC);

COMMENT ON TABLE saved_contracts IS
  'Marcadores de oportunidades compartidos dentro del workspace; no crean matches ni cambian seguimiento.';
