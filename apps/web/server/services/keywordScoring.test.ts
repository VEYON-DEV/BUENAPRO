import test from "node:test";
import assert from "node:assert/strict";
import { scoreBusinessLine } from "./keywordScoring.ts";

test("frase exacta y término fuerte", () => {
  assert.equal(scoreBusinessLine("Asesoría legal ante OSCE", ["asesoría legal"], ["osce"]).points, 25);
});

test("una frase no se divide ni coincide si sus palabras están separadas", () => {
  assert.equal(scoreBusinessLine("Asesoría especializada en materia legal", ["asesoría legal"], []).points, 0);
  assert.equal(scoreBusinessLine("Especialista legal", ["asesoría legal"], []).points, 0);
});

test("solo puntúa una palabra que fue declarada explícitamente como término fuerte", () => {
  assert.equal(scoreBusinessLine("Servicio de software", ["desarrollo de software"], []).points, 0);
  assert.equal(scoreBusinessLine("Servicio de software", ["desarrollo de software"], ["software"]).points, 10);
  assert.equal(scoreBusinessLine("Servicio de construcción", [], ["software"]).points, 0);
});

test("no duplica un término contenido en una frase exacta", () => {
  assert.equal(scoreBusinessLine("registro de marcas ante indecopi", ["registro de marcas"], ["marcas", "indecopi"]).points, 25);
});

test("respeta límites de palabra, tildes y topes", () => {
  assert.equal(scoreBusinessLine("actividades ilegales", [], ["legal"]).points, 0);
  assert.equal(scoreBusinessLine("ARBITRAJE, OSCE, SUNAFIL y cobranza", [], ["arbitraje", "osce", "sunafil", "cobranza"]).points, 30);
  assert.equal(scoreBusinessLine("asesoría legal defensa judicial derecho administrativo arbitraje internacional", ["asesoría legal", "defensa judicial", "derecho administrativo", "arbitraje internacional"], []).points, 45);
});

test("la identidad de empresa suma una sola vez y no duplica una señal de línea", () => {
  assert.equal(scoreBusinessLine("software para gestión", [], [], ["software", "gestión"]).points, 10);
  assert.equal(scoreBusinessLine("software para gestión", [], ["software"], ["software"]).points, 10);
});
