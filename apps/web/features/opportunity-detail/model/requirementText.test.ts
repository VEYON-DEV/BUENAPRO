import test from "node:test";
import assert from "node:assert/strict";
import { requirementFullText, requirementSourceFacet } from "./requirementText.ts";

test("full source text follows identity when the historical index has a different facet order", () => {
  const complete = "Experiencia acreditada S/ 80 000 con contratos y conformidades. ".repeat(35);
  const facets = [
    { label: "RNP vigente", facet: "rnp", details_json: { descripcion: "Constancia vigente" } },
    { label: "Experiencia acreditada…", facet: "specific_experience", details_json: { descripcion: complete } },
  ];
  const row = { requisito: "Experiencia acreditada…", source_facet: "specific_experience", source_index: 1 };
  assert.equal(requirementSourceFacet(row, facets), facets[1]);
  assert.equal(requirementFullText(row, facets), complete.trim());
  assert.equal(requirementFullText(row, [...facets].reverse()), complete.trim());
});

test("a wrong type, missing label or ambiguous duplicate never exposes another requirement", () => {
  const facet = { label: "Experiencia…", facet: "specific_experience", details_json: { descripcion: "Descripción íntegra" } };
  assert.equal(requirementFullText({ requisito: "Experiencia…", source_facet: "rnp", source_index: 1 }, [facet]), null);
  assert.equal(requirementFullText({ requisito: "Otro requisito…", source_index: 1 }, [facet]), null);
  assert.equal(requirementFullText({ source_index: 1 }, [facet]), null);
  assert.equal(requirementFullText({ label: facet.label }, [facet, { ...facet }]), null);
  assert.equal(requirementFullText({ label: facet.label }, [facet]), "Descripción íntegra");
});
