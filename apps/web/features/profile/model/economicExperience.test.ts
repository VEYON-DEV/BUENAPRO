import test from "node:test";
import assert from "node:assert/strict";
import { validateEconomicExperience } from "./economicExperience.ts";

test("preserves legacy values and arbitrary metadata without coercion", () => {
  for (const value of [undefined, null, {}, { servicios: "24000", bienes: 0, metadata: { demo: true } }]) {
    const before = JSON.stringify(value);
    assert.equal(validateEconomicExperience(value), null);
    assert.equal(JSON.stringify(value), before);
  }
});

test("accepts multiple area amounts and preserves document metadata", () => {
  const value = { servicios: 24000, custom: 123, areas: [
    { rubro: "Software", especialidad: "Desarrollo web", tipo_objeto: "servicios", monto: 300000, moneda: "PEN", descripcion: "Contratos ejecutados", documents: [{ id: "a", custom: { provenance: "manual" } }] },
    { rubro: "Servidores", tipo_objeto: "bienes", monto: 0, moneda: "USD" },
    { rubro: "Ingeniería", monto: 4500, moneda: "EUR", extra: true },
  ] };
  const before = JSON.stringify(value);
  assert.equal(validateEconomicExperience(value), null);
  assert.equal(JSON.stringify(value), before);
});

test("checks container shape and the maximum of 100 areas", () => {
  for (const value of [[], "legacy", 42, { areas: null }, { areas: {} }, { areas: [null] }, { areas: [[]] }]) assert.ok(validateEconomicExperience(value));
  const area = { rubro: "Tecnología", monto: 10, moneda: "PEN" };
  assert.equal(validateEconomicExperience({ areas: Array(100).fill(area) }), null);
  assert.ok(validateEconomicExperience({ areas: Array(101).fill(area) }));
  assert.equal(validateEconomicExperience({ areas: [] }), null);
});

test("requires a nonempty area and a finite numeric nonnegative amount", () => {
  for (const rubro of [undefined, null, "", "  ", 81]) assert.ok(validateEconomicExperience({ areas: [{ rubro, monto: 1, moneda: "PEN" }] }));
  for (const monto of [undefined, null, "100", -1, NaN, Infinity, false]) assert.ok(validateEconomicExperience({ areas: [{ rubro: "Software", monto, moneda: "PEN" }] }));
});

test("checks currencies, optional object type and text fields", () => {
  const area = { rubro: "Tecnología", monto: 10, moneda: "PEN" };
  for (const moneda of [undefined, null, "", "SOL", 1]) assert.ok(validateEconomicExperience({ areas: [{ ...area, moneda }] }));
  for (const tipo_objeto of ["bienes", "servicios", "obras", "consultoria_obras", undefined, null, ""]) assert.equal(validateEconomicExperience({ areas: [{ ...area, tipo_objeto }] }), null);
  for (const key of ["especialidad", "descripcion", "tipo_objeto"]) assert.match(validateEconomicExperience({ areas: [area, { ...area, [key]: {} }] })!, /^Área 2:/);
});
