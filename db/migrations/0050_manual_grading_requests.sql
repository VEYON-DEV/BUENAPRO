-- Calificacion manual: un usuario pide «Calificar» un contrato que aun no tiene calificacion
-- (incluidos contratos cerrados que nunca se leyeron). La web registra la solicitud aqui:
--   mode 'direct': ya habia una lectura valida y la web la evaluo ella misma (el worker la ignora).
--   mode 'queued': no habia lectura; el worker la lee y luego la evalua para la empresa.
-- Estados del worker (mode 'queued'): pending -> reading -> evaluating -> done | failed. 'reason' lleva
-- el codigo corto de un fallo (reading_requires_review, reading_failed, no_documents, contract_not_found,
-- profile_inactive, invalid_model_output, reading_timeout).
-- Idempotente: IF NOT EXISTS; no toca filas existentes.
CREATE TABLE IF NOT EXISTS manual_grading_requests (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  profile_id uuid NOT NULL REFERENCES company_profiles(id),
  source_key text NOT NULL CHECK (source_key IN ('seace_prod6', 'seace_prod2')),
  source_id bigint NOT NULL,            -- id_contrato (prod6) or id_convocatoria (prod2)
  requested_by text,
  mode text NOT NULL CHECK (mode IN ('direct', 'queued')),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'reading', 'evaluating', 'done', 'failed')),
  reason text,
  lima_day date NOT NULL DEFAULT ((now() AT TIME ZONE 'America/Lima')::date),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS manual_grading_requests_active_uniq
  ON manual_grading_requests (source_key, source_id, profile_id) WHERE status IN ('pending', 'reading', 'evaluating');
CREATE INDEX IF NOT EXISTS manual_grading_requests_tenant_day_idx ON manual_grading_requests (tenant_id, lima_day);
CREATE INDEX IF NOT EXISTS manual_grading_requests_queue_idx ON manual_grading_requests (source_key, mode, status, created_at);

COMMENT ON TABLE manual_grading_requests IS
  'Solicitudes manuales de calificacion (boton Calificar) por contrato y empresa. Una sola solicitud activa por contrato y empresa. La web cuenta el limite diario por tenant con lima_day; el worker no lo vuelve a comprobar pero nunca procesa dos veces una solicitud.';
COMMENT ON COLUMN manual_grading_requests.source_id IS
  'id_contrato (seace_prod6) o id_convocatoria (seace_prod2).';
COMMENT ON COLUMN manual_grading_requests.mode IS
  'direct: la web evaluo una lectura ya valida; el worker la ignora. queued: la procesa el worker (lee si hace falta y evalua).';
COMMENT ON COLUMN manual_grading_requests.status IS
  'pending -> reading (se encolo la lectura) -> evaluating (se encolo analyze_match) -> done | failed. Una solicitud atascada en reading o evaluating mas de 24 h pasa a failed (reading_timeout).';
COMMENT ON COLUMN manual_grading_requests.reason IS
  'Codigo corto del fallo cuando status = failed.';
COMMENT ON COLUMN manual_grading_requests.lima_day IS
  'Dia calendario en America/Lima en que se pidio; base del limite diario por tenant.';
