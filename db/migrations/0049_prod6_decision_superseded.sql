-- PROD6 menores: 'superseded' marca una decision pendiente que quedo obsoleta porque existe una fila
-- mas nueva para el mismo contrato y empresa (cambio de ficha o de perfil). No es un descarte ni una
-- aprobacion: solo evita que queden como 'pending' para siempre y distorsionen los conteos.
-- Idempotente: busca el CHECK de `decision` por columna (no por nombre) y lo reemplaza siempre por
-- uno con nombre fijo, asi puede reaplicarse sin error. Nunca modifica filas existentes.
DO $$
DECLARE
  existing RECORD;
BEGIN
  FOR existing IN
    SELECT c.conname
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
    WHERE c.conrelid = 'prod6_profile_decisions'::regclass
      AND c.contype = 'c'
      AND a.attname = 'decision'
      AND cardinality(c.conkey) = 1
  LOOP
    EXECUTE format('ALTER TABLE prod6_profile_decisions DROP CONSTRAINT %I', existing.conname);
  END LOOP;
  ALTER TABLE prod6_profile_decisions
    ADD CONSTRAINT prod6_profile_decisions_decision_check
    CHECK (decision IN ('accepted', 'rejected', 'pending', 'superseded'));
END
$$;

COMMENT ON COLUMN prod6_profile_decisions.decision IS
  'pending: paso el prefiltro y espera afinidad JEV; no es un descarte. superseded: era pending y existe una fila mas nueva para el mismo contrato y empresa (ficha o perfil cambiados); no se califica ni se muestra como pendiente.';
