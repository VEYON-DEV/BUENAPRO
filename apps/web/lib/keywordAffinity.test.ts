import test from "node:test";
import assert from "node:assert/strict";
import { keywordAffinitySummary } from "./keywordAffinity.ts";

test("a calculated zero stays distinct from a missing profile or unconfigured lines", () => {
  assert.equal(keywordAffinitySummary({ fit_points: null, fit_level: null }), null);
  assert.equal(keywordAffinitySummary({ fit_points: 0, fit_level: 1, business_line_name: null }), null);
  const low = keywordAffinitySummary({ fit_points: 0, fit_level: 1, business_line_name: "Software", keyword_hits: [] });
  assert.equal(low?.label, "Afinidad baja");
  assert.match(low?.description ?? "", /Sin coincidencias/);
});

test("keyword evidence explains high affinity without reusing historical Jev or eligibility", () => {
  const fit = keywordAffinitySummary({ keyword_points: 35, fit_level: 3, business_line_name: "Integración",
    keyword_hits: [{ keyword: "software" }, { keyword: "software" }, { keyword: "integración de sistemas" }] });
  assert.equal(fit?.label, "Afinidad alta");
  assert.deepEqual(fit?.hits, ["software", "integración de sistemas"]);
  assert.match(fit?.description ?? "", /35 de 45/);
  assert.match(fit?.description ?? "", /No confirma que cumplas los requisitos/);
});

test("invalid or unavailable affinity cannot render a confident label", () => {
  for (const points of [-1, 46, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(keywordAffinitySummary({ fit_points: points, fit_level: 3, business_line_name: "Software" }), null);
  }
  assert.equal(keywordAffinitySummary({ fit_points: 30, fit_level: 4, business_line_name: "Software" }), null);
});
