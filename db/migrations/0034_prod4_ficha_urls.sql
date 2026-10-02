-- Each procedure links to its own official ficha, rather than the general map.
-- Document/PDF URLs remain in their existing fields and tables.
UPDATE prod4_processes
SET source_url = 'https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/' || id_procedimiento::text
WHERE source_url IS DISTINCT FROM
  'https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/' || id_procedimiento::text;
