import type { ProcurementScheduleStage } from "@/lib/procurementSchedule";

const DAY = 86400000;
/** Calendar coordinates, never interpreted as deadline instants. */
export function limaDay(value: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Lima", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(date);
  return ["year", "month", "day"].map(type => parts.find(p => p.type === type)?.value).join("-");
}
export function addDays(value: string, offset: number): string {
  return new Date(Date.parse(`${value}T12:00:00Z`) + offset * DAY).toISOString().slice(0, 10);
}
export function dayOffset(value: string, start: string): number {
  return Math.round((Date.parse(`${limaDay(value)}T12:00:00Z`) - Date.parse(`${start}T12:00:00Z`)) / DAY);
}
export function validDay(value: string | undefined): value is string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T12:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}
export function stageLabel(stage: ProcurementScheduleStage): string {
  return ({ proposals: "Ofertas", consultations: "Preguntas", observations: "Observaciones", registration: "Registro", publication: "Convocatoria", answers: "Respuestas", award: "Buena pro", other: "" })[stage.kind] || stage.name.replace(/\(.*\)/g, "").trim();
}
export function layoutStages(stages: ProcurementScheduleStage[], start: string, days: number) {
  const lanes: number[] = [];
  const bars = stages.flatMap(stage => {
    if (!stage.startsAt || !stage.endsAt) return [];
    const rawStart = dayOffset(stage.startsAt, start);
    const rawEnd = dayOffset(stage.endsAt, start);
    if (rawEnd < rawStart || rawEnd < 0 || rawStart >= days) return [];
    return [{ stage, start: Math.max(0, rawStart), end: Math.min(days, rawEnd + 1), clippedStart: rawStart < 0, clippedEnd: rawEnd >= days }];
  }).sort((a, b) => a.start - b.start || a.end - b.end);
  const positioned = bars.map(bar => {
    let lane = lanes.findIndex(end => end <= bar.start);
    if (lane < 0) lane = lanes.length;
    lanes[lane] = bar.end;
    return { ...bar, lane };
  });
  return { bars: positioned, lanes: Math.max(1, lanes.length) };
}
export function formatBoundary(value: string | null, precision: string | null): string {
  if (!value) return "No informado";
  const date = new Date(precision === "day" ? `${value}T12:00:00-05:00` : value);
  if (!Number.isFinite(date.getTime())) return "No informado";
  const day = new Intl.DateTimeFormat("es-PE", { timeZone: "America/Lima", day: "numeric", month: "short", year: "numeric" }).format(date);
  return precision === "day" ? `${day} · hora no informada` : `${day}, ${new Intl.DateTimeFormat("es-PE", { timeZone: "America/Lima", hour: "2-digit", minute: "2-digit", hour12: false }).format(date)}`;
}
