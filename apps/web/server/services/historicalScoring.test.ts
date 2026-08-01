import test from "node:test";
import assert from "node:assert/strict";
import { scoreHistoricalCandidate, summarizeComparables } from "./historicalScoring.ts";

const current = {
  cubsoItem: "81112000",
  entityName: "PROVIAS NACIONAL",
  keywordHits: [
    { keyword: "consultoría de puentes", match: "exact_phrase" },
    { keyword: "supervisión", match: "strong_term" },
  ],
  now: new Date("2026-07-31T00:00:00Z"),
};

test("explica y puntúa CUBSO, keywords, entidad y recencia", () => {
  const result = scoreHistoricalCandidate({
    id_contrato: 1,
    codigo_completo: "CM-1-2026-PVN",
    descripcion: "Consultoría de puentes y supervisión",
    cubso_item: "81112000",
    entity_name: "PROVIAS NACIONAL",
    estado_resultado: "ADJUDICADO",
    precio_total: 100,
    fec_publica: "2026-01-01",
  }, current);
  assert.equal(result.score, 88);
  assert.deepEqual(result.keyword_matches, ["consultoría de puentes", "supervisión"]);
  assert.ok(result.reasons.includes("Mismo código CUBSO"));
});

test("la mediana y rango ignoran precios de desiertos", () => {
  const base = { score: 50, reasons: [], keyword_matches: [] };
  const metrics = summarizeComparables([
    { ...base, id_contrato: 1, codigo_completo: "1", descripcion: "A", estado_resultado: "ADJUDICADO", precio_total: 100 },
    { ...base, id_contrato: 2, codigo_completo: "2", descripcion: "B", estado_resultado: "ADJUDICADO", precio_total: 300 },
    { ...base, id_contrato: 3, codigo_completo: "3", descripcion: "C", estado_resultado: "DESIERTO", precio_total: null },
  ]);
  assert.equal(metrics.precio_median, 200);
  assert.equal(metrics.desiertos_pct, 33);
  assert.equal(metrics.precio_min, 100);
  assert.equal(metrics.precio_max, 300);
});

test("una familia CUBSO aislada no alcanza señal fuerte", () => {
  const result = scoreHistoricalCandidate({
    id_contrato: 4,
    codigo_completo: "CM-4-2026-X",
    descripcion: "Mantenimiento de impresoras",
    cubso_item: "81119999",
    entity_name: "OTRA ENTIDAD",
    estado_resultado: "ADJUDICADO",
    precio_total: 550,
    fec_publica: "2026-01-01",
  }, current);
  assert.equal(result.score, 30);
  assert.deepEqual(result.keyword_matches, []);
});

test("precio cero de SEACE se trata como no informado", () => {
  const base = { score: 50, reasons: [], keyword_matches: [] };
  const metrics = summarizeComparables([
    { ...base, id_contrato: 5, codigo_completo: "5", descripcion: "A", estado_resultado: "ADJUDICADO", precio_total: 0 },
  ]);
  assert.equal(metrics.precio_median, null);
  assert.equal(metrics.precio_min, null);
});
