-- Histórico analítico de contrataciones culminadas. Se mantiene separado del
-- feed operativo para que un backfill no convierta miles de contratos cerrados
-- en oportunidades activas.

CREATE TABLE historical_entities (
  seace_entity_id BIGINT PRIMARY KEY,
  name TEXT NOT NULL,
  sigla_id BIGINT,
  sigla_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE historical_user_areas (
  seace_area_id BIGINT PRIMARY KEY,
  seace_entity_id BIGINT REFERENCES historical_entities(seace_entity_id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE historical_suppliers (
  ruc VARCHAR(11) PRIMARY KEY,
  razon_social TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  total_awards INTEGER NOT NULL DEFAULT 0,
  total_awarded_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE historical_contract_outcomes (
  id_contrato BIGINT PRIMARY KEY,
  codigo_completo TEXT NOT NULL,
  codigo_tipo TEXT,
  codigo_correlativo INTEGER,
  codigo_anio INTEGER,
  codigo_sigla TEXT,
  seace_entity_id BIGINT REFERENCES historical_entities(seace_entity_id) ON DELETE SET NULL,
  seace_area_id BIGINT REFERENCES historical_user_areas(seace_area_id) ON DELETE SET NULL,
  entity_name TEXT,
  area_name TEXT,
  cubso_segmento VARCHAR(20),
  cubso_item VARCHAR(40),
  cubso_name TEXT,
  descripcion TEXT NOT NULL,
  fec_publica TIMESTAMPTZ,
  fec_ini_cotizacion TIMESTAMPTZ,
  fec_fin_cotizacion TIMESTAMPTZ,
  estado_resultado VARCHAR(20) NOT NULL CHECK (estado_resultado IN ('ADJUDICADO','DESIERTO','SIN_RESULTADO')),
  supplier_ruc VARCHAR(11) REFERENCES historical_suppliers(ruc) ON DELETE SET NULL,
  supplier_name TEXT,
  precio_total NUMERIC(18,2) CHECK (precio_total IS NULL OR precio_total > 0),
  source_document_url TEXT,
  raw_detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_historical_outcomes_segment_state
  ON historical_contract_outcomes (cubso_segmento, estado_resultado);
CREATE INDEX ix_historical_outcomes_cubso_item
  ON historical_contract_outcomes (cubso_item);
CREATE INDEX ix_historical_outcomes_entity
  ON historical_contract_outcomes (seace_entity_id, fec_publica DESC);
CREATE INDEX ix_historical_outcomes_supplier
  ON historical_contract_outcomes (supplier_ruc, fec_publica DESC);
CREATE INDEX ix_historical_outcomes_price
  ON historical_contract_outcomes (precio_total) WHERE precio_total IS NOT NULL;
CREATE INDEX ix_historical_outcomes_description_fts
  ON historical_contract_outcomes USING GIN (to_tsvector('spanish', coalesce(descripcion, '') || ' ' || coalesce(cubso_name, '')));
CREATE INDEX ix_historical_outcomes_code_trgm
  ON historical_contract_outcomes USING GIN (codigo_completo gin_trgm_ops);

COMMENT ON TABLE historical_contract_outcomes IS
  'Resultados culminados SEACE para comparables; no forman parte del feed de oportunidades vigentes.';
