import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_RANGE,
  addDays,
  dayWindow,
  isTraceId,
  isValidDay,
  limaDay,
  opsHref,
  parseCompany,
  parseItemsQuery,
  parseOpsFilters,
  parseRange,
} from "./filters.ts";

const COMPANY = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3";
// 30/10/2026 14:00 en Lima.
const NOW = new Date("2026-10-30T19:00:00Z");

test("el día es el de Lima: cambia a las 05:00 UTC, no a medianoche UTC", () => {
  assert.equal(limaDay(new Date("2026-10-25T04:59:59Z")), "2026-10-24");
  assert.equal(limaDay(new Date("2026-10-25T05:00:00Z")), "2026-10-25");
  assert.equal(limaDay(new Date("2026-01-01T03:00:00Z")), "2025-12-31");
});

test("isValidDay rechaza formatos y fechas imposibles", () => {
  assert.equal(isValidDay("2026-10-30"), true);
  assert.equal(isValidDay("2028-02-29"), true);
  for (const value of ["2026-02-30", "2026-13-01", "2026-1-1", "30/10/2026", "2026-10-30T00:00", "", null, undefined, 20261030]) {
    assert.equal(isValidDay(value), false, String(value));
  }
});

test("addDays cruza meses y años sin depender de la zona horaria", () => {
  assert.equal(addDays("2026-11-01", -1), "2026-10-31");
  assert.equal(addDays("2026-01-01", -1), "2025-12-31");
  assert.equal(addDays("2026-12-31", 1), "2027-01-01");
  assert.equal(addDays("2028-03-01", -1), "2028-02-29");
});

test("la ventana tiene `range` días, termina hoy y va del más nuevo al más antiguo", () => {
  const window = dayWindow("2026-10-30", 14);
  assert.equal(window.days.length, 14);
  assert.equal(window.days[0], "2026-10-30");
  assert.equal(window.days[13], "2026-10-17");
  assert.equal(window.from, "2026-10-17");
  assert.equal(window.to, "2026-10-30");
  assert.equal(dayWindow("2026-03-02", 7).from, "2026-02-24");
});

test("el rango admite 7, 14 y 30; cualquier otro valor vuelve a 14 y queda marcado inválido", () => {
  assert.deepEqual(parseRange(null), { value: DEFAULT_RANGE, valid: true });
  assert.deepEqual(parseRange(""), { value: 14, valid: true });
  for (const value of ["7", "14", "30"]) assert.deepEqual(parseRange(value), { value: Number(value), valid: true });
  for (const value of ["0", "1", "15", "31", "-7", "abc", "7.5", "100"]) assert.deepEqual(parseRange(value), { value: 14, valid: false }, value);
});

test("la empresa es un UUID, «todas»/vacío significa sin filtro y lo demás es inválido", () => {
  assert.deepEqual(parseCompany(null), { value: null, valid: true });
  assert.deepEqual(parseCompany("all"), { value: null, valid: true });
  assert.deepEqual(parseCompany(COMPANY.toUpperCase()), { value: COMPANY, valid: true });
  for (const value of ["veyon", "1", `${COMPANY}x`, "'; DROP TABLE x;--"]) assert.equal(parseCompany(value).valid, false, value);
});

test("sin parámetros: últimos 14 días, todas las empresas y hoy como día de fallas", () => {
  const filters = parseOpsFilters(new URLSearchParams(), NOW);
  assert.equal(filters.range, 14);
  assert.equal(filters.today, "2026-10-30");
  assert.equal(filters.day, "2026-10-30");
  assert.equal(filters.company, null);
  assert.equal(filters.from, "2026-10-17");
  assert.equal(filters.to, "2026-10-30");
  assert.deepEqual(filters.errors, []);
});

test("los parámetros válidos se respetan y los inválidos caen al valor por omisión con el error anotado", () => {
  const ok = parseOpsFilters(new URLSearchParams(`range=30&company=${COMPANY}&day=2026-10-25`), NOW);
  assert.equal(ok.range, 30);
  assert.equal(ok.days.length, 30);
  assert.equal(ok.company, COMPANY);
  assert.equal(ok.day, "2026-10-25");
  assert.deepEqual(ok.errors, []);

  const bad = parseOpsFilters(new URLSearchParams("range=99&company=zzz&day=2026-11-15"), NOW);
  assert.equal(bad.range, 14);
  assert.equal(bad.company, null);
  assert.equal(bad.day, "2026-10-30", "un día futuro no es válido");
  assert.deepEqual(bad.errors.sort(), ["company", "day", "range"]);
});

test("el «hoy» de Lima manda: a las 22:00 en Lima ya es otro día en UTC", () => {
  const late = new Date("2026-10-31T03:00:00Z"); // 30/10 22:00 en Lima
  assert.equal(parseOpsFilters(new URLSearchParams(), late).today, "2026-10-30");
});

test("parseItemsQuery exige fuente, paso del catálogo y día válido", () => {
  const ok = parseItemsQuery(new URLSearchParams(`source=menores&step=jev_accepted&day=2026-10-25&company=${COMPANY}&range=7`), NOW);
  assert.deepEqual(ok, { ok: true, value: { source: "menores", step: "jev_accepted", day: "2026-10-25", company: COMPANY, range: 7 } });

  const cases: Array<[string, string]> = [
    ["step=jev_accepted&day=2026-10-25", "source"],
    ["source=otros&step=jev_accepted&day=2026-10-25", "source"],
    ["source=mayores&step=jev_accepted&day=2026-10-25", "step"],
    ["source=menores&step=x&day=2026-10-25", "step"],
    ["source=menores&step=jev_accepted", "day"],
    ["source=menores&step=jev_accepted&day=2026-10-31", "day"],
    ["source=menores&step=jev_accepted&day=2026-02-30", "day"],
    ["source=menores&step=jev_accepted&day=2026-10-25&company=zzz", "company"],
  ];
  for (const [query, error] of cases) {
    assert.deepEqual(parseItemsQuery(new URLSearchParams(query), NOW), { ok: false, error }, query);
  }
});

test("isTraceId solo acepta enteros positivos que caben en bigint", () => {
  for (const value of ["1", "104188", "9223372036854775807"]) assert.equal(isTraceId(value), true, value);
  for (const value of ["0", "01", "-5", "1.5", "abc", "1; DROP", "", "12345678901234567890"]) assert.equal(isTraceId(value), false, value);
});

test("opsHref omite los parámetros vacíos", () => {
  assert.equal(opsHref("/ops", { range: null, company: undefined, day: "" }), "/ops");
  assert.equal(opsHref("/ops/items", { source: "menores", day: "2026-10-25", company: null }), "/ops/items?source=menores&day=2026-10-25");
});
