/** Official SEACE schedule only: no Gemini inference and no end-of-day defaults. */
export type ScheduleStageKind =
  | "consultations" | "observations" | "answers" | "registration"
  | "proposals" | "publication" | "award" | "other";
export type ScheduleDatePrecision = "day" | "minute" | "second";
export type ProcurementScheduleStage = {
  id: string;
  name: string;
  kind: ScheduleStageKind;
  kinds: ScheduleStageKind[];
  /** Date-only values stay YYYY-MM-DD; timed values are ISO UTC instants. */
  startsAt: string | null;
  endsAt: string | null;
  startPrecision: ScheduleDatePrecision | null;
  endPrecision: ScheduleDatePrecision | null;
  timezone: "America/Lima";
};

type RecordValue = Record<string, unknown>;
type ParsedDate = { value: string; precision: ScheduleDatePrecision };

function record(value: unknown): RecordValue {
  return value && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function plain(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

export function classifyScheduleStage(name: string): ScheduleStageKind[] {
  const label = plain(name);
  // Answers are not the period in which suppliers may submit questions.
  if (/absoluci|respuesta|contestaci/.test(label)) return ["answers"];
  if (/cotizaci|presentacion.*(oferta|propuesta)|recepcion.*(oferta|propuesta)/.test(label)) return ["proposals"];
  if (/registro.*participante|inscripci.*participante/.test(label)) return ["registration"];
  const kinds: ScheduleStageKind[] = [];
  if (/consulta|pregunta/.test(label)) kinds.push("consultations");
  if (/observacion/.test(label)) kinds.push("observations");
  if (kinds.length) return kinds;
  if (/buena pro|adjudicaci/.test(label)) return ["award"];
  if (/convocatoria|publicaci/.test(label)) return ["publication"];
  return ["other"];
}

/** Strict calendar parsing. A source date without a time never becomes a deadline instant. */
export function parseOfficialScheduleDate(value: unknown, separateTime?: unknown): ParsedDate | null {
  const input = text(value);
  if (!input) return null;
  const match = input.match(/^(?:(\d{2})\/(\d{2})\/(\d{4})|(\d{4})-(\d{2})-(\d{2}))(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,3})?)?(Z|[+-]\d{2}:?\d{2})?)?$/);
  if (!match) return null;
  const year = Number(match[3] ?? match[4]);
  const month = Number(match[2] ?? match[5]);
  const day = Number(match[1] ?? match[6]);
  const calendar = new Date(Date.UTC(year, month - 1, day));
  if (year < 1000 || calendar.getUTCFullYear() !== year || calendar.getUTCMonth() !== month - 1 || calendar.getUTCDate() !== day) return null;
  const date = `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const separate = text(separateTime);
  const timeMatch = separate ? separate.match(/^(\d{2}):(\d{2})(?::(\d{2}))?$/) : null;
  if (separate && !timeMatch) return null;
  if (!match[7] && !timeMatch) return { value: date, precision: "day" };
  // A separate official hour, if present, belongs to the source's date.
  const hour = Number(timeMatch?.[1] ?? match[7]);
  const minute = Number(timeMatch?.[2] ?? match[8]);
  const secondValue = timeMatch ? timeMatch[3] : match[9];
  const second = Number(secondValue ?? "0");
  if (hour > 23 || minute > 59 || second > 59) return null;
  const zone = timeMatch ? null : match[10];
  let offsetMinutes = -5 * 60; // Both official SEACE sources publish local Lima times.
  if (zone === "Z") offsetMinutes = 0;
  else if (zone) {
    const offset = zone.match(/^([+-])(\d{2}):?(\d{2})$/)!;
    if (Number(offset[2]) > 14 || Number(offset[3]) > 59 || (Number(offset[2]) === 14 && Number(offset[3]) !== 0)) return null;
    offsetMinutes = (offset[1] === "+" ? 1 : -1) * (Number(offset[2]) * 60 + Number(offset[3]));
  }
  const fraction = timeMatch ? "" : input.match(/:\d{2}\.(\d{1,3})/)?.[1] ?? "";
  const milliseconds = fraction ? Number(fraction.padEnd(3, "0")) : 0;
  const timestamp = Date.UTC(year, month - 1, day, hour, minute, second, milliseconds) - offsetMinutes * 60000;
  return { value: new Date(timestamp).toISOString(), precision: secondValue === undefined ? "minute" : "second" };
}

function stages(payload: unknown, field: string): unknown[] {
  if (Array.isArray(payload)) return payload;
  const object = record(payload);
  const values = object[field] ?? object.etapas;
  return Array.isArray(values) ? values : [];
}

function normalize(payload: unknown, source: "prod4" | "prod6"): ProcurementScheduleStage[] {
  return stages(payload, source === "prod4" ? "listaCronograma" : "uitContratoEtapaProjectionList").map((value, index) => {
    const stage = record(value);
    const name = text(source === "prod4" ? stage.nombreEtapa ?? stage.descripcionEtapa : stage.nomEtapaContrato) || "Etapa sin nombre";
    const kinds = classifyScheduleStage(name);
    const start = source === "prod4"
      ? parseOfficialScheduleDate(stage.fechaInicio, stage.horaInicio)
      : parseOfficialScheduleDate(stage.fecIni);
    const end = source === "prod4"
      ? parseOfficialScheduleDate(stage.fechaFin, stage.horaFin)
      : parseOfficialScheduleDate(stage.fecFin);
    return {
      id: `${source}-${String(stage.nidCronograma ?? stage.idContratoEtapa ?? stage.idEtapaContrato ?? stage.idEtapa ?? index)}-${index}`,
      name, kind: kinds[0], kinds,
      startsAt: start?.value ?? null, endsAt: end?.value ?? null,
      startPrecision: start?.precision ?? null, endPrecision: end?.precision ?? null,
      timezone: "America/Lima",
    };
  });
}

export function normalizeProd4Schedule(payload: unknown): ProcurementScheduleStage[] {
  return normalize(payload, "prod4");
}

export function normalizeProd6Schedule(payload: unknown): ProcurementScheduleStage[] {
  return normalize(payload, "prod6");
}

export type ScheduleTemporalState = "past" | "active" | "upcoming" | "unknown";

/** Temporal position only, never proof of evaluation, award or eligibility. */
export function scheduleTemporalState(stage: ProcurementScheduleStage, now: Date): ScheduleTemporalState {
  if (!Number.isFinite(now.getTime())) return "unknown";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Lima", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const part = (type: string) => parts.find(value => value.type === type)?.value;
  const today = `${part("year")}-${part("month")}-${part("day")}`;
  const compare = (value: string | null, precision: ScheduleDatePrecision | null): number | null => {
    if (!value || !precision) return null;
    if (precision === "day") return value < today ? -1 : value > today ? 1 : null;
    const instant = new Date(value).getTime();
    return Number.isFinite(instant) ? Math.sign(instant - now.getTime()) : null;
  };
  // Corrupt/reversed ranges do not become a reassuring status.
  if (stage.startsAt && stage.endsAt) {
    const bound = (value: string, precision: ScheduleDatePrecision | null) => precision === "day" ? `${value}T00:00:00-05:00` : value;
    if (new Date(bound(stage.startsAt, stage.startPrecision)).getTime() > new Date(bound(stage.endsAt, stage.endPrecision)).getTime()) return "unknown";
  }
  const start = compare(stage.startsAt, stage.startPrecision);
  const end = compare(stage.endsAt, stage.endPrecision);
  if (end !== null && end <= 0) return "past";
  if (start !== null && start > 0) return "upcoming";
  if (start !== null && start <= 0 && end !== null && end > 0) return "active";
  return "unknown";
}

/** Date-only quotation windows cannot safely populate timestamptz deadline columns. */
export function prod6QuotationWindow(payload: unknown): { startsAt: string | null; endsAt: string | null } {
  const sourceStages = stages(payload, "uitContratoEtapaProjectionList");
  const index = sourceStages.findIndex((value) => {
    const stage = record(value);
    return Number(stage.idEtapaContrato) === 2 || classifyScheduleStage(text(stage.nomEtapaContrato)).includes("proposals");
  });
  const quotation = normalizeProd6Schedule(sourceStages)[index];
  return {
    startsAt: quotation && quotation.startPrecision !== "day" ? quotation.startsAt : null,
    endsAt: quotation && quotation.endPrecision !== "day" ? quotation.endsAt : null,
  };
}
