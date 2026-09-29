-- Los resultados existentes provienen del barrido de Servicios. El objeto
-- forma parte de la clasificación del resultado, pero id_contrato sigue siendo
-- la identidad global del contrato en SEACE.
ALTER TABLE historical_contract_outcomes
  ADD COLUMN objeto_codigo SMALLINT NOT NULL DEFAULT 2
    REFERENCES cat_seace_objects(codigo);

ALTER TABLE historical_backfill_progress
  ADD COLUMN objeto_codigo SMALLINT NOT NULL DEFAULT 2
    REFERENCES cat_seace_objects(codigo);

ALTER TABLE historical_backfill_progress
  DROP CONSTRAINT historical_backfill_progress_pkey;

ALTER TABLE historical_backfill_progress
  ADD PRIMARY KEY (objeto_codigo, cubso_segmento);

CREATE INDEX ix_historical_outcomes_object_segment_state
  ON historical_contract_outcomes (objeto_codigo, cubso_segmento, estado_resultado);

COMMENT ON COLUMN historical_contract_outcomes.objeto_codigo IS
  'Objeto SEACE: 1 Bien, 2 Servicio, 3 Obra, 4 Consultoria de Obra.';
COMMENT ON TABLE historical_backfill_progress IS
  'Checkpoint reanudable por objeto SEACE y segmento CUBSO; registros anteriores corresponden a Servicio.';
