const OBJECT_TYPES = ["bienes", "servicios", "obras", "consultoria_obras"];
const CURRENCIES = ["PEN", "USD", "EUR"];

/** Validates the new area breakdown without coercing or rewriting legacy metadata. */
export function validateEconomicExperience(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value !== "object" || Array.isArray(value)) return "La experiencia económica debe ser un objeto.";
  const areas = (value as Record<string, unknown>).areas;
  if (areas === undefined) return null;
  if (!Array.isArray(areas)) return "Los montos por área deben ser una lista.";
  if (areas.length > 100) return "Puedes registrar hasta 100 montos por área.";
  for (const [index, item] of areas.entries()) {
    const prefix = `Área ${index + 1}: `;
    if (!item || typeof item !== "object" || Array.isArray(item)) return `${prefix}el registro debe ser un objeto.`;
    const area = item as Record<string, unknown>;
    if (typeof area.rubro !== "string" || !area.rubro.trim()) return `${prefix}indica el rubro.`;
    for (const key of ["especialidad", "descripcion"]) {
      if (area[key] != null && typeof area[key] !== "string") return `${prefix}${key} debe ser texto.`;
    }
    if (area.tipo_objeto != null && area.tipo_objeto !== "" && !OBJECT_TYPES.includes(area.tipo_objeto as string)) return `${prefix}tipo_objeto tiene un valor no válido.`;
    if (typeof area.monto !== "number" || !Number.isFinite(area.monto) || area.monto < 0) return `${prefix}indica un monto mayor o igual a cero.`;
    if (!CURRENCIES.includes(area.moneda as string)) return `${prefix}selecciona una moneda válida.`;
  }
  return null;
}
