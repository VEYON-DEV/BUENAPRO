/** Formatos del panel de operación. Días y horas siempre en America/Lima. */

const TIME_ZONE = "America/Lima";

const dayLabel = new Intl.DateTimeFormat("es-PE", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" });
const fullDay = new Intl.DateTimeFormat("es-PE", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric" });
const timeOnly = new Intl.DateTimeFormat("es-PE", { timeZone: TIME_ZONE, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const dateTime = new Intl.DateTimeFormat("es-PE", {
  timeZone: TIME_ZONE,
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const counts = new Intl.NumberFormat("es-PE");

function clean(text: string): string {
  return text.replace(/[  ]/g, " ").replace(/[.,]/g, "").replace(/\s+/g, " ").trim();
}

/** «jue 30 oct» para un día YYYY-MM-DD (fecha de calendario, sin zona horaria). */
export function formatDayLabel(day: string): string {
  return clean(dayLabel.format(new Date(`${day}T12:00:00Z`)));
}

/** «jueves 30 de octubre de 2026». */
export function formatFullDay(day: string): string {
  return fullDay.format(new Date(`${day}T12:00:00Z`)).replace(/[  ]/g, " ");
}

/** «14:05:09» en hora de Lima. */
export function formatLimaTime(iso: string): string {
  return timeOnly.format(new Date(iso)).replace(/[  ]/g, " ");
}

/** «30/10/2026 14:05:09» en hora de Lima. */
export function formatLimaDateTime(iso: string): string {
  return dateTime.format(new Date(iso)).replace(/[  ]/g, " ").replace(",", "");
}

/** Día de Lima (YYYY-MM-DD) de un instante ISO. */
export function limaDayOfIso(iso: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(iso));
}

export function formatCount(value: number): string {
  return counts.format(value);
}

/** USD con precisión útil: centavos desde 1 dólar, cuatro decimales por debajo (los costos por contrato son de milésimas). */
export function formatUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value === 0) return "$0";
  return `$${value >= 1 ? value.toFixed(2) : value.toFixed(4)}`;
}

/** Costo de una celda de la tabla de costos: «—» si no hubo gasto; «*» si algún evento no trae costo. */
export function formatCostCell(usd: number, events: number, priced: number): string {
  if (events === 0) return "—";
  return `${formatUsd(usd)}${priced < events ? "*" : ""}`;
}

/** 0–1: proporción de `value` respecto del máximo, para escalar barras. */
export function ratio(value: number, max: number): number {
  return max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
}
