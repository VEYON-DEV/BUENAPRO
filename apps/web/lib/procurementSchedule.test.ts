import test from "node:test";
import assert from "node:assert/strict";
import { classifyScheduleStage, normalizeProd4Schedule, normalizeProd6Schedule, parseOfficialScheduleDate, prod6QuotationWindow, scheduleTemporalState } from "./procurementSchedule.ts";

test("PROD4 preserves date-only absolution and distinguishes formulation from answers", () => {
  const schedule = normalizeProd4Schedule({ listaCronograma: [
    { nombreEtapa: "FORMULACIÓN DE CONSULTAS Y OBSERVACIONES", fechaInicio: "28/08/2026", horaInicio: "00:01", fechaFin: "01/09/2026", horaFin: "23:59" },
    { nombreEtapa: "ABSOLUCIÓN DE CONSULTAS Y OBSERVACIONES", fechaInicio: "25/09/2026", horaInicio: null, fechaFin: "25/09/2026", horaFin: null },
  ] });
  assert.deepEqual(schedule[0].kinds, ["consultations", "observations"]);
  assert.equal(schedule[0].startsAt, "2026-08-28T05:01:00.000Z");
  assert.equal(schedule[0].endsAt, "2026-09-02T04:59:00.000Z");
  assert.equal(schedule[0].endPrecision, "minute");
  assert.equal(schedule[1].kind, "answers");
  assert.equal(schedule[1].endsAt, "2026-09-25");
  assert.equal(schedule[1].endPrecision, "day");
});

test("PROD6 decodes official second-precision Lima questions and quotations", () => {
  const raw = { uitContratoEtapaProjectionList: [
    { fecFin: "01/10/2026 13:30:00", fecIni: "29/09/2026 13:25:00", idEtapaContrato: 1, nomEtapaContrato: "ETAPA DE CONSULTAS" },
    { fecFin: "02/10/2026 18:00:00", fecIni: "01/10/2026 14:00:00", idEtapaContrato: 2, nomEtapaContrato: "ETAPA DE COTIZACIÓN" },
  ] };
  const schedule = normalizeProd6Schedule(raw);
  assert.equal(schedule[0].kind, "consultations");
  assert.equal(schedule[0].endsAt, "2026-10-01T18:30:00.000Z");
  assert.equal(schedule[0].endPrecision, "second");
  assert.deepEqual(prod6QuotationWindow(raw), { startsAt: "2026-10-01T19:00:00.000Z", endsAt: "2026-10-02T23:00:00.000Z" });
});

test("strict parser rejects invalid dates, hours and offsets without rollover", () => {
  for (const value of ["31/02/2026", "29/02/2026", "00/10/2026", "01/13/2026", "01/10/2026 24:00", "01/10/2026 12:60", "01/10/2026 12:00:60", "2026-10-01T00:00+14:30", "garbage"]) {
    assert.equal(parseOfficialScheduleDate(value), null, value);
  }
  assert.deepEqual(parseOfficialScheduleDate("29/02/2024"), { value: "2024-02-29", precision: "day" });
  assert.equal(parseOfficialScheduleDate("01/10/2026", "25:00"), null);
});

test("explicit offsets and naive ISO timestamps have deterministic conversion", () => {
  assert.equal(parseOfficialScheduleDate("2026-10-01T13:30:00-05:00")?.value, "2026-10-01T18:30:00.000Z");
  assert.equal(parseOfficialScheduleDate("2026-10-01T18:30:00Z")?.value, "2026-10-01T18:30:00.000Z");
  assert.equal(parseOfficialScheduleDate("2026-10-01T13:30")?.value, "2026-10-01T18:30:00.000Z");
});

test("all official stages remain in order, including unknown stages and missing dates", () => {
  const names = ["Convocatoria", "Registro de participantes", "Formulación de observaciones", "Presentación de ofertas", "Evaluación técnica", "Otorgamiento de buena pro"];
  const schedule = normalizeProd4Schedule(names.map(nombreEtapa => ({ nombreEtapa })));
  assert.deepEqual(schedule.map(stage => stage.name), names);
  assert.deepEqual(schedule.map(stage => stage.kind), ["publication", "registration", "observations", "proposals", "other", "award"]);
  assert.ok(schedule.every(stage => stage.startsAt === null && stage.endsAt === null));
  assert.equal(new Set(schedule.map(stage => stage.id)).size, names.length);
});

test("answer labels never become submission-question periods", () => {
  for (const name of ["Respuesta a preguntas", "Absolución de consultas", "Absolución de observaciones"]) assert.deepEqual(classifyScheduleStage(name), ["answers"]);
  assert.deepEqual(classifyScheduleStage("Presentación de propuestas"), ["proposals"]);
});

test("missing closing date stays unknown even if source has only an ending hour", () => {
  const [stage] = normalizeProd4Schedule([{ nombreEtapa: "Presentación de propuestas", fechaInicio: "01/10/2026", horaInicio: "00:01", horaFin: "23:59" }]);
  assert.equal(stage.endsAt, null);
  assert.equal(stage.endPrecision, null);
});

test("missing/date-only quotation windows clear stale timestamps without guessing", () => {
  assert.deepEqual(prod6QuotationWindow({}), { startsAt: null, endsAt: null });
  assert.deepEqual(prod6QuotationWindow([{ idEtapaContrato: "2", nomEtapaContrato: "Cotización", fecIni: "01/10/2026", fecFin: "02/10/2026" }]), { startsAt: null, endsAt: null });
  assert.deepEqual(normalizeProd6Schedule({ etapas: [] }), []);
});

test("timeline distinguishes elapsed, active and future windows at exact boundaries", () => {
  const [stage] = normalizeProd6Schedule([{ nomEtapaContrato: "Cotización", fecIni: "01/10/2026 10:00:00", fecFin: "01/10/2026 12:00:00" }]);
  assert.equal(scheduleTemporalState(stage, new Date("2026-10-01T14:59:59Z")), "upcoming");
  assert.equal(scheduleTemporalState(stage, new Date("2026-10-01T15:00:00Z")), "active");
  assert.equal(scheduleTemporalState(stage, new Date("2026-10-01T17:00:00Z")), "past");
});

test("date-only today remains uncertain and uses Lima day, not UTC day", () => {
  const [stage] = normalizeProd4Schedule([{ nombreEtapa: "Absolución", fechaInicio: "01/10/2026", fechaFin: "01/10/2026" }]);
  assert.equal(scheduleTemporalState(stage, new Date("2026-10-01T04:59:00Z")), "upcoming");
  assert.equal(scheduleTemporalState(stage, new Date("2026-10-02T04:59:00Z")), "unknown");
  assert.equal(scheduleTemporalState(stage, new Date("2026-10-02T05:00:00Z")), "past");
});

test("partial dates do not invent active windows and reversed ranges stay unknown", () => {
  const now = new Date("2026-10-01T17:00:00Z");
  const schedules = normalizeProd4Schedule([
    { nombreEtapa: "Preguntas", fechaFin: "02/10/2026", horaFin: "12:00" },
    { nombreEtapa: "Preguntas", fechaInicio: "01/10/2026", horaInicio: "10:00" },
    { nombreEtapa: "Preguntas", fechaInicio: "03/10/2026", horaInicio: "10:00", fechaFin: "02/10/2026", horaFin: "10:00" },
    { nombreEtapa: "Preguntas" },
  ]);
  assert.ok(schedules.every(stage => scheduleTemporalState(stage, now) === "unknown"));
});

test("official extension changes temporal position without persisting a stale status", () => {
  const now = new Date("2026-10-01T17:00:00Z");
  const [old] = normalizeProd6Schedule([{ nomEtapaContrato: "Cotización", fecIni: "01/10/2026 10:00", fecFin: "01/10/2026 11:00" }]);
  const [extended] = normalizeProd6Schedule([{ nomEtapaContrato: "Cotización", fecIni: "01/10/2026 10:00", fecFin: "01/10/2026 13:00" }]);
  assert.equal(scheduleTemporalState(old, now), "past");
  assert.equal(scheduleTemporalState(extended, now), "active");
});
