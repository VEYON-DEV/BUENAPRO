import { findStep } from "./steps.ts";
import type { OpsSource } from "./types.ts";
import { OPS_SOURCES } from "./types.ts";

/** Todos los días del panel son días de Lima: `(ts AT TIME ZONE 'America/Lima')::date`. */
export const OPS_TIME_ZONE = "America/Lima";
export const RANGE_OPTIONS = [7, 14, 30] as const;
export type OpsRange = (typeof RANGE_OPTIONS)[number];
export const DEFAULT_RANGE: OpsRange = 14;
/** Tope de filas de una lista de elementos. */
export const ITEMS_LIMIT = 200;
/** Elementos que enseña cada grupo de «Fallas». */
export const FAILURE_ITEMS_LIMIT = 50;

const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DAY_MS = 86_400_000;

const limaParts = new Intl.DateTimeFormat("en-CA", {
  timeZone: OPS_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** Día de Lima (YYYY-MM-DD) de un instante. */
export function limaDay(instant: Date): string {
  const parts = Object.fromEntries(limaParts.formatToParts(instant).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

/** Fecha de calendario válida (rechaza 2026-02-30 y formatos distintos de YYYY-MM-DD). */
export function isValidDay(value: unknown): value is string {
  if (typeof value !== "string" || !DAY_PATTERN.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

/** Suma días a un día de calendario sin pasar por la zona horaria local. */
export function addDays(day: string, delta: number): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, date) + delta * DAY_MS).toISOString().slice(0, 10);
}

/** Ventana de `range` días que termina en `today` (inclusive); `days` va del más nuevo al más antiguo. */
export function dayWindow(today: string, range: number) {
  const days = Array.from({ length: range }, (_, index) => addDays(today, -index));
  return { from: days[days.length - 1], to: today, days };
}

export type OpsFilters = {
  range: OpsRange;
  from: string;
  to: string;
  /** Más nuevo primero. */
  days: string[];
  today: string;
  /** Día de «Fallas»; hoy por omisión. */
  day: string;
  /** UUID del perfil de empresa, o null para «Todas». */
  company: string | null;
  /** Parámetros presentes pero inválidos; las páginas usan los valores por omisión, las APIs responden 400. */
  errors: string[];
};

export function parseCompany(raw: string | null): { value: string | null; valid: boolean } {
  if (raw == null || raw === "" || raw === "all" || raw === "todas") return { value: null, valid: true };
  return UUID_PATTERN.test(raw) ? { value: raw.toLowerCase(), valid: true } : { value: null, valid: false };
}

export function parseRange(raw: string | null): { value: OpsRange; valid: boolean } {
  if (raw == null || raw === "") return { value: DEFAULT_RANGE, valid: true };
  const number = Number(raw);
  return (RANGE_OPTIONS as readonly number[]).includes(number)
    ? { value: number as OpsRange, valid: true }
    : { value: DEFAULT_RANGE, valid: false };
}

export function parseOpsFilters(params: URLSearchParams, now: Date = new Date()): OpsFilters {
  const errors: string[] = [];
  const today = limaDay(now);
  const range = parseRange(params.get("range"));
  if (!range.valid) errors.push("range");
  const company = parseCompany(params.get("company"));
  if (!company.valid) errors.push("company");
  const rawDay = params.get("day");
  let day = today;
  if (rawDay) {
    if (isValidDay(rawDay) && rawDay <= today) day = rawDay;
    else errors.push("day");
  }
  const window = dayWindow(today, range.value);
  return { range: range.value, ...window, today, day, company: company.value, errors };
}

export type ItemsQuery = {
  source: OpsSource;
  step: string;
  day: string;
  company: string | null;
  range: OpsRange;
};

/** El identificador de una traza en la URL: entero positivo de hasta 19 dígitos (cabe en bigint). */
export function isTraceId(value: string): boolean {
  return /^[1-9]\d{0,18}$/.test(value);
}

export function parseSource(raw: string | null | undefined): OpsSource | null {
  return OPS_SOURCES.find((source) => source === raw) ?? null;
}

/** Valida los parámetros de /ops/items: fuente, paso del catálogo y día; empresa y rango son opcionales. */
export function parseItemsQuery(
  params: URLSearchParams,
  now: Date = new Date(),
): { ok: true; value: ItemsQuery } | { ok: false; error: string } {
  const source = parseSource(params.get("source"));
  if (!source) return { ok: false, error: "source" };
  const step = params.get("step") ?? "";
  if (!findStep(source, step)) return { ok: false, error: "step" };
  const day = params.get("day");
  if (!isValidDay(day) || day > limaDay(now)) return { ok: false, error: "day" };
  const company = parseCompany(params.get("company"));
  if (!company.valid) return { ok: false, error: "company" };
  const range = parseRange(params.get("range"));
  return { ok: true, value: { source, step, day, company: company.value, range: range.value } };
}

/** Parámetros de consulta (sin valores por omisión) para enlazar entre las vistas del panel. */
export function opsHref(path: string, query: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value != null && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}
