CREATE INDEX IF NOT EXISTS ix_contracts_lifecycle_refresh
  ON seace_contracts (estado_codigo, detail_fetched_at, fec_fin_cotizacion)
  WHERE estado_codigo IN (2, 3);

COMMENT ON INDEX ix_contracts_lifecycle_refresh IS
  'Rota el refresco lifecycle por contratos activos menos recientemente consultados.';
