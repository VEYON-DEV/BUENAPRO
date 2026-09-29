-- PROD6 ignora el parametro anio: corregir los contratos ya ingeridos con el
-- año real de publicación en Lima. El id_contrato permanece como PK estable.
UPDATE seace_contracts
SET anio = EXTRACT(YEAR FROM fec_publica AT TIME ZONE 'America/Lima')::smallint,
    updated_at = now()
WHERE fec_publica IS NOT NULL
  AND anio IS DISTINCT FROM EXTRACT(YEAR FROM fec_publica AT TIME ZONE 'America/Lima')::smallint;
