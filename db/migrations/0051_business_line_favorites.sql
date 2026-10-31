-- Líneas de negocio prioritarias (favoritas) de la empresa.
-- La empresa marca en su perfil las líneas que más le importan; en la lista de licitaciones mayores (PROD2) las
-- licitaciones cuya mejor línea es una favorita, con palabras clave que coinciden, aparecen primero. Solo ordena y
-- filtra: no cambia el puntaje, la afinidad ni lo que ve el resto de la lista.
-- Idempotente: IF NOT EXISTS; las líneas existentes quedan sin marcar (false) y no se encola ningún recálculo.
ALTER TABLE business_lines
  ADD COLUMN IF NOT EXISTS is_favorite boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN business_lines.is_favorite IS
  'Línea prioritaria marcada por la empresa en su perfil. Sube en la lista de licitaciones mayores las licitaciones cuya mejor línea (con palabras clave que coinciden) es esta; no altera puntajes ni afinidad.';
