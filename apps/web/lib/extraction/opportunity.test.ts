import test from "node:test";
import assert from "node:assert/strict";
import { opportunityFacts } from "./opportunity.ts";

test("detail reads the complete Spanish source summary instead of the abbreviated index", () => {
  const complete = "Entrega de tableros en 45 días. " + "Condiciones y experiencia acreditable. ".repeat(30);
  const row = { raw_extraction_json: { summary: { descripcion_corta: complete } }, summary_json: { descripcion_corta: "Entrega de tableros…" } };
  assert.equal(opportunityFacts(row).lecturaRapida, complete);
  assert.equal(opportunityFacts(row).descripcionCorta, "Entrega de tableros…");
});

test("source string and English descriptions retain their existing precedence and fallback", () => {
  assert.equal(opportunityFacts({ raw_extraction_json: { summary: "Resumen íntegro" } }).lecturaRapida, "Resumen íntegro");
  assert.equal(opportunityFacts({ raw_extraction_json: { summary: { description: "Complete description", descripcion_corta: "Descripción" } } }).lecturaRapida, "Complete description");
  assert.equal(opportunityFacts({ summary_json: { descripcion_corta: "Único resumen disponible" } }).lecturaRapida, "Único resumen disponible");
  assert.equal(opportunityFacts(null).lecturaRapida, null);
});
