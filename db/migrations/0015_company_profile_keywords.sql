ALTER TABLE company_profiles
  ADD COLUMN company_keywords TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN company_profiles.company_keywords IS
  'Señales generales de identidad del proveedor; el motor aplica stemming español y un único bono acotado.';

COMMENT ON COLUMN business_lines.keyword_phrases IS
  'Frases de 2 a 6 palabras; se comparan en orden con stemming español y no se dividen al puntuar.';

COMMENT ON COLUMN business_lines.keyword_terms IS
  'Hasta 8 palabras o siglas fuertes de una sola palabra, elegidas explícitamente.';
