import test from "node:test";
import assert from "node:assert/strict";
import { cleanCompanyKeywords } from "./companyKeywords.ts";

test("normaliza y deduplica keywords generales sin imponer un rubro", () => {
  assert.deepEqual(cleanCompanyKeywords(["  Software ", "software", "Base   de datos", "API"]), ["software", "base de datos", "api"]);
});

test("rechaza señales aisladas genéricas y expresiones demasiado largas", () => {
  assert.deepEqual(cleanCompanyKeywords(["servicios", "empresa", "una expresión con demasiadas palabras"]), []);
});
