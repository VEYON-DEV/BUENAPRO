export type ProfileRecord = Record<string, any> | string;

// Legacy strings remain strings until edited. Unknown metadata must survive UI edits.
export function recordObject(record: ProfileRecord, primary: string): Record<string, any> {
  return typeof record === "string" ? { [primary]: record } : { ...record };
}

export function updateRecord(record: ProfileRecord, primary: string, key: string, value: unknown): ProfileRecord {
  return { ...recordObject(record, primary), [key]: value };
}

export function recordText(record: ProfileRecord, key: string, primary: string): string {
  const value = recordObject(record, primary)[key];
  return value == null ? "" : String(value);
}

export function recordList(value: unknown): ProfileRecord[] {
  return Array.isArray(value) ? value : [];
}

export function validateProfileRecords(value: unknown, primary = "role", previous: unknown = []): string | null {
  const baseline = recordList(previous).map(item => JSON.stringify(item));
  for (const record of recordList(value)) {
    if (baseline.includes(JSON.stringify(record))) continue;
    const object = recordObject(record, primary);
    if (typeof object[primary] === "string" && !object[primary].trim()) return "Completa el nombre, rol u objeto de los registros nuevos antes de guardar.";
    for (const key of ["experiencia_anios", "cantidad", "monto", "anio", "horas"]) {
      const item = object[key];
      if (typeof item === "number" && (!Number.isFinite(item) || item < 0)) return "Los montos, años, horas y cantidades no pueden ser negativos.";
    }
    const nested = validateProfileRecords(object.experience, "role") || validateProfileRecords(object.certifications, "nombre");
    if (nested) return nested;
  }
  return null;
}
