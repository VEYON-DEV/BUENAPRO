-- Oportunidades guardadas por workspace sobre la identidad canónica (opportunities.id).
-- Sirve a cualquier fuente que enlace opportunity_id (p. ej. Buscador SEACE PROD2).
-- Contratos menores conservan saved_contracts; guardar no implica seguir ni postular.

CREATE TABLE IF NOT EXISTS saved_opportunities (
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  saved_by UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS ix_saved_opportunities_tenant_created
  ON saved_opportunities (tenant_id, created_at DESC);

COMMENT ON TABLE saved_opportunities IS
  'Marcadores del workspace por identidad canónica; no crean matches, seguimiento ni postulaciones.';
