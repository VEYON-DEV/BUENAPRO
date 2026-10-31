-- Cola de lectura automática de licitaciones mayores (PROD2).
-- El descubrimiento (worker 1) deja aquí lo aceptado por JEV con afinidad desde el umbral de lectura (85) que no
-- tiene lectura previa; la lectura (worker 2, contenedor prod2-manual) la atiende de madrugada. Una fila por
-- convocatoria: no se encola dos veces y un fallo queda registrado para revisión (el botón «Calificar» sigue
-- disponible para releer a pedido).
CREATE TABLE IF NOT EXISTS prod2_reading_queue (
  id_convocatoria bigint PRIMARY KEY,
  affinity_index double precision NOT NULL CHECK (affinity_index >= 0 AND affinity_index <= 100),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'reading', 'done', 'failed', 'skipped')),
  reason text,
  attempts smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  enqueued_by text NOT NULL CHECK (length(enqueued_by) BETWEEN 1 AND 160),
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS prod2_reading_queue_open_idx
  ON prod2_reading_queue (status, enqueued_at) WHERE status IN ('pending', 'reading');

COMMENT ON TABLE prod2_reading_queue IS
  'Lecturas automaticas pendientes de mayores: las encola el descubrimiento (afinidad >= umbral de lectura, sin lectura previa) y las atiende el worker de lectura de madrugada.';
COMMENT ON COLUMN prod2_reading_queue.status IS
  'pending -> reading -> done | failed (reason) | skipped (cerrada, ya leida o sin documentos). Una fila en reading mas de 6 h vuelve a pending.';
COMMENT ON COLUMN prod2_reading_queue.attempts IS
  'Lecturas iniciadas; despues de 2 intentos fallidos queda failed.';
COMMENT ON COLUMN prod2_reading_queue.enqueued_by IS
  'run_id del ciclo de descubrimiento que la encolo.';
