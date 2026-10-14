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

const SMALL_WORDS = new Set(["de", "del", "la", "las", "el", "los", "y", "e", "en", "para", "por", "a"]);

/** Accent/case/space-insensitive key so "OIL & GAS" and "Oil & Gas" share a group. */
export function normalizeGroup(text: string): string {
  return text.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
}

function titleCase(text: string): string {
  return text.toLowerCase().split(" ").map((word, index) => index > 0 && SMALL_WORDS.has(word) ? word : word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

/** Display label for a group: the first variant typed in mixed case, otherwise a title-cased one. */
export function groupLabel(variants: string[]): string {
  const clean = variants.map(item => item.replace(/\s+/g, " ").trim()).filter(Boolean);
  return clean.find(item => item !== item.toUpperCase()) ?? titleCase(clean[0] ?? "");
}

export type RecordGroup = { key: string; label: string; items: Array<{ record: ProfileRecord; index: number }> };

/** Groups records by a text field, keeping original indexes. Records without a value go last. */
export function groupRecords(records: ProfileRecord[], primary: string, field?: string): RecordGroup[] {
  if (!field) return [{ key: "", label: "", items: records.map((record, index) => ({ record, index })) }];
  const groups = new Map<string, { variants: string[]; items: RecordGroup["items"] }>();
  records.forEach((record, index) => {
    const raw = recordText(record, field, primary);
    const key = normalizeGroup(raw);
    const group = groups.get(key) ?? { variants: [], items: [] };
    group.variants.push(raw); group.items.push({ record, index });
    groups.set(key, group);
  });
  return [...groups.entries()]
    .map(([key, group]) => ({ key, label: key ? groupLabel(group.variants) : "Sin rubro", items: group.items }))
    .sort((a, b) => (a.key ? 0 : 1) - (b.key ? 0 : 1) || b.items.length - a.items.length || a.label.localeCompare(b.label, "es"));
}

/** Free-text search over every string value of a record. */
export function recordMatches(record: ProfileRecord, primary: string, query: string): boolean {
  const needle = normalizeGroup(query);
  if (!needle) return true;
  return Object.values(recordObject(record, primary)).some(value => (typeof value === "string" || typeof value === "number") && normalizeGroup(String(value)).includes(needle));
}

export function formatMoney(amount: unknown, currency: unknown): string | null {
  if (typeof amount !== "number" || !Number.isFinite(amount)) return null;
  const symbol = currency === "PEN" ? "S/" : typeof currency === "string" && currency ? currency : "Moneda pendiente";
  return `${symbol} ${new Intl.NumberFormat("es-PE", { maximumFractionDigits: 2 }).format(amount)}`;
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
