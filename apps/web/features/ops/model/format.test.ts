import test from "node:test";
import assert from "node:assert/strict";
import { formatCostCell, formatDayLabel, formatLimaDateTime, formatLimaTime, formatUsd, limaDayOfIso, ratio } from "./format.ts";

test("la hora se muestra en Lima (UTC-5) y el día cambia a las 05:00 UTC", () => {
  assert.equal(formatLimaTime("2026-10-25T04:59:59Z"), "23:59:59");
  assert.equal(limaDayOfIso("2026-10-25T04:59:59Z"), "2026-10-24");
  assert.equal(limaDayOfIso("2026-10-25T05:00:00Z"), "2026-10-25");
  assert.equal(formatLimaDateTime("2026-10-25T05:00:00Z"), "25/10/2026 00:00:00");
});

test("el día de calendario no depende de la zona horaria del servidor", () => {
  assert.match(formatDayLabel("2026-10-30"), /^vie 30 oct/);
  assert.match(formatDayLabel("2026-01-01"), /^jue 1 ene/);
});

test("el costo usa milésimas bajo un dólar y marca los eventos sin costo", () => {
  assert.equal(formatUsd(0), "$0");
  assert.equal(formatUsd(0.00177), "$0.0018");
  assert.equal(formatUsd(12.3456), "$12.35");
  assert.equal(formatUsd(null), "—");
  assert.equal(formatCostCell(0, 0, 0), "—");
  assert.equal(formatCostCell(0.5, 4, 3), "$0.5000*");
  assert.equal(formatCostCell(0.5, 3, 3), "$0.5000");
});

test("la proporción de una barra queda entre 0 y 1", () => {
  assert.equal(ratio(5, 10), 0.5);
  assert.equal(ratio(5, 0), 0);
  assert.equal(ratio(20, 10), 1);
});
