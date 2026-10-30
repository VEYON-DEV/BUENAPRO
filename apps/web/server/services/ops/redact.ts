/**
 * Saneamiento de lo que el panel muestra. El panel nunca enseña secretos ni texto completo de documentos:
 * las consultas ya piden solo columnas y claves conocidas; esto es la segunda barrera.
 */

const HIDDEN = "[oculto]";
const OMITTED = "[omitido]";
const MAX_STRING = 400;
const MAX_ARRAY = 20;
const MAX_DEPTH = 6;
const MAX_JSON = 20_000;

/** Claves cuyo valor es un secreto: nunca se muestra. */
const SECRET_KEY = /(secret|token|password|passwd|authorization|api[_-]?key|apikey|cookie|credential|bearer|private[_-]?key|ticket|signature|session|storage_key)/i;
/** Los conteos de tokens («input_tokens») no son secretos aunque contengan «token». */
const TOKEN_COUNT_KEY = /tokens$/i;
/** Claves que suelen cargar texto de documentos o respuestas completas del proveedor: se omiten. */
const BULK_KEY = /^(raw_[a-z_]*|llm1_input|full_text|document_text|extracted_text|content|text|texto|markdown|html|body|response|prompt|input_json)$/i;

/** Quita parámetros de URL, credenciales en línea y tokens sueltos de un texto libre (mensajes de error). */
export function scrubText(value: string, max: number = MAX_STRING): string {
  const cleaned = value
    .replace(/(https?:\/\/[^\s"'<>?#]+)[?#][^\s"'<>]*/gi, "$1?…")
    .replace(/(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, `$1${HIDDEN}`)
    .replace(/((?:api[_-]?key|apikey|token|secret|password|passwd|authorization|ticket)["']?\s*[=:]\s*["']?)[^\s,;&"'}]+/gi, `$1${HIDDEN}`);
  return cleaned.length > max ? `${cleaned.slice(0, max)}… (+${cleaned.length - max} caracteres)` : cleaned;
}

/** Copia saneada de un valor JSON: sin secretos, con textos y listas acotados y profundidad limitada. */
export function redact(value: unknown, depth: number = 0): unknown {
  if (value == null || typeof value === "number" || typeof value === "boolean") return value ?? null;
  if (typeof value === "bigint") return value.toString();
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.toISOString();
  if (typeof value === "string") return scrubText(value);
  if (depth >= MAX_DEPTH) return "[…]";
  if (Array.isArray(value)) {
    const items = value.slice(0, MAX_ARRAY).map((item) => redact(item, depth + 1));
    return value.length > MAX_ARRAY ? [...items, `… (+${value.length - MAX_ARRAY} elementos)`] : items;
  }
  if (typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (SECRET_KEY.test(key) && !TOKEN_COUNT_KEY.test(key)) result[key] = item == null ? null : HIDDEN;
      else if (BULK_KEY.test(key)) result[key] = item == null ? null : OMITTED;
      else result[key] = redact(item, depth + 1);
    }
    return result;
  }
  return String(value);
}

/** JSON legible para el bloque «ver detalle»; recorta si aun así es demasiado largo. */
export function detailJson(value: unknown): string {
  const text = JSON.stringify(redact(value), null, 2) ?? "null";
  return text.length > MAX_JSON ? `${text.slice(0, MAX_JSON)}\n… (recortado, +${text.length - MAX_JSON} caracteres)` : text;
}
