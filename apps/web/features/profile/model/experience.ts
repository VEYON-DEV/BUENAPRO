export const EXPERIENCE_OBJECT_TYPES = ["bienes", "servicios", "obras", "consultoria_obras"] as const;
export const EXPERIENCE_PARTICIPATION_MODES = ["individual", "consorcio", "subcontratista"] as const;
export const EXPERIENCE_CURRENCIES = ["PEN", "USD", "EUR"] as const;
export const EXPERIENCE_ACCREDITATION_STATUSES = ["declarada", "documentada", "pendiente"] as const;

function missing(value: unknown): boolean {
  return value == null || value === "";
}

function validDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
}

// Check only known fields; legacy descriptions and unknown metadata are never rewritten.
export function validateExperienceRecords(value: unknown): string | null {
  if (value == null) return null;
  if (!Array.isArray(value)) return "La experiencia debe ser una lista de contratos.";
  for (const [index, item] of value.entries()) {
    const prefix = `Contrato ${index + 1}: `;
    if (typeof item === "string") continue;
    if (!item || typeof item !== "object" || Array.isArray(item)) return `${prefix}el registro debe ser un objeto o una descripción.`;
    const record = item as Record<string, unknown>;
    for (const key of ["rubro", "especialidad", "actividades"]) {
      if (!missing(record[key]) && typeof record[key] !== "string") return `${prefix}${key} debe ser texto.`;
    }
    for (const [key, allowed] of [
      ["tipo_objeto", EXPERIENCE_OBJECT_TYPES],
      ["moneda", EXPERIENCE_CURRENCIES],
      ["modalidad_participacion", EXPERIENCE_PARTICIPATION_MODES],
      ["acreditacion", EXPERIENCE_ACCREDITATION_STATUSES],
    ] as const) {
      if (!missing(record[key]) && !(allowed as readonly unknown[]).includes(record[key])) return `${prefix}${key} tiene un valor no válido.`;
    }
    for (const key of ["monto", "porcentaje_participacion"]) {
      const number = record[key];
      if (missing(number)) continue;
      if (typeof number !== "number" || !Number.isFinite(number) || number < 0) return `${prefix}${key} debe ser un número mayor o igual a cero.`;
      if (key === "porcentaje_participacion" && number > 100) return `${prefix}la participación debe estar entre 0 y 100%.`;
    }
    for (const key of ["fecha_inicio", "fecha_fin", "fecha_conformidad"]) {
      if (!missing(record[key]) && !validDate(record[key])) return `${prefix}${key} debe ser una fecha válida (AAAA-MM-DD).`;
    }
    const start = record.fecha_inicio;
    const end = record.fecha_fin;
    const acceptance = record.fecha_conformidad;
    if (validDate(start) && validDate(end) && end < start) return `${prefix}la fecha final no puede ser anterior al inicio.`;
    if (validDate(acceptance) && ((validDate(end) && acceptance < end) || (validDate(start) && acceptance < start))) return `${prefix}la conformidad no puede ser anterior a la ejecución del contrato.`;
  }
  return null;
}
