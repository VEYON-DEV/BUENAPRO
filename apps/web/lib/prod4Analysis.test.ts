import test from "node:test";
import assert from "node:assert/strict";
import { buildProd4Analysis, type Prod4Extraction, type Prod4DocumentMatch } from "./prod4Analysis.ts";

const extraction: Prod4Extraction = {
  id: "12", codigo_alfresco: "official-document", summary_json: { resumen: "Bases leídas" },
  raw_extraction_json: {}, model: "configured-model", prompt_version: "v1", schema_version: "v1",
  quality: "auto", requires_human_review: false, facet_count: 1, created_at: "2026-10-02T12:00:00Z",
};
const match: Prod4DocumentMatch = {
  id: "34", profile_id: "tenant-active-profile", score: 84, verdict: "ambar",
  breakdown_json: { resumen: "Según tu perfil", requisitos: [{ requisito: "Experiencia", estado: "accionable" }] },
  missing_actions_json: [{ accion: "Acreditar contratos" }],
  matched_at: "2026-10-02T12:01:00Z", updated_at: "2026-10-02T12:01:00Z",
};
const base = { sourceStatus: null, reason: null, checkedAt: null, extraction: null, facets: [], match: null };

test("missing document analysis is pending, never a fabricated score", () => {
  const result = buildProd4Analysis(base);
  assert.equal(result.status, "pending");
  assert.equal(result.match, null);
  assert.equal(result.extraction, null);
});

test("access-denied diagnosis survives and is not a completed evaluation", () => {
  const result = buildProd4Analysis({ ...base, sourceStatus: "skipped", reason: "official_document_access_denied" });
  assert.equal(result.status, "skipped");
  assert.equal(result.reason, "official_document_access_denied");
});

test("extracted document without tenant match is distinguishable", () => {
  const result = buildProd4Analysis({ ...base, extraction });
  assert.equal(result.status, "extracted");
  assert.deepEqual(result.extraction?.summary_json, { resumen: "Bases leídas" });
  assert.equal(result.match, null);
});

test("current document-backed match preserves requirements and missing actions", () => {
  const result = buildProd4Analysis({ ...base, extraction, match });
  assert.equal(result.status, "matched");
  assert.equal(result.match?.score, 84);
  assert.deepEqual(result.match?.breakdown_json, match.breakdown_json);
  assert.deepEqual(result.match?.missing_actions_json, match.missing_actions_json);
});

test("older match and failed extraction never imply current eligibility", () => {
  assert.equal(buildProd4Analysis({ ...base, extraction, match: { ...match, matched_at: "2026-10-01T12:00:00Z" } }).match, null);
  const result = buildProd4Analysis({ ...base, extraction: { ...extraction, quality: "failed" }, match });
  assert.equal(result.status, "failed");
  assert.equal(result.match, null);
  assert.deepEqual(result.facets, []);
});

test("worker extraction ID linkage takes precedence over timestamps", () => {
  const forDifferentDocument = { ...match, breakdown_json: { meta: { extraction_id: 11 } } };
  assert.equal(buildProd4Analysis({ ...base, extraction, match: forDifferentDocument }).match, null);
  const forCurrentDocument = { ...match, breakdown_json: { meta: { extraction_id: 12 } } };
  assert.equal(buildProd4Analysis({ ...base, extraction, match: forCurrentDocument }).status, "matched");
});
