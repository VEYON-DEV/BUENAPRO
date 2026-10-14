import { test } from "node:test";
import assert from "node:assert/strict";
import { enqueueProd4Analysis, getProd4AnalysisJob } from "../apps/web/server/services/prod4MatchQueue.ts";

const eligible = { profile_id: "profile", extraction_id: 8, document_analysis_status: "extracted", technology_relevant: true, quality: "good", requires_human_review: false, facet_count: 2, adequate_requirements: true };
function db(rows) {
  const calls = [];
  return { calls, async query(sql, values) { calls.push({ sql, values }); return { rows: rows.shift() ?? [] }; } };
}
for (const force of [false, true]) {
  test(`retired PROD4 rejects manual analysis before database access (force=${force})`, async () => {
    const client = db([[eligible]]);
    await assert.rejects(() => enqueueProd4Analysis(client, "tenant", "123", force), e =>
      e.code === "source_retired" && e.status === 410 && /PROD4 fue retirado/.test(e.message));
    assert.equal(client.calls.length, 0);
  });
}
test("status is scoped to tenant and procedure and errors are sanitized", async () => {
  const client = db([[{ job_id: "4", status: "dead" }]]);
  const result = await getProd4AnalysisJob(client, "tenant", "123", "4");
  assert.match(result.error, /No se pudo/);
  assert.match(client.calls[0].sql, /cp.tenant_id=\$1/);
  assert.deepEqual(client.calls[0].values, ["tenant", "4", "123"]);
});
test("another tenant's unknown job returns not found", async () => {
  await assert.rejects(() => getProd4AnalysisJob(db([[]]), "other", "123", "4"), e => e.status === 404);
});
test("completed worker without a current matching extraction is skipped", async () => {
  const client = db([[{ job_id: "4", status: "done", profile_active: true, match_current: false }]]);
  const result = await getProd4AnalysisJob(client, "tenant", "123", "4");
  assert.equal(result.status, "skipped");
  assert.match(result.error, /bases cambiaron/);
  assert.match(client.calls[0].sql, /m\.breakdown_json->'meta'->>'extraction_id'=j\.payload->>'extraction_id'/);
  assert.match(client.calls[0].sql, /m\.updated_at >= j\.created_at/);
});
test("completed worker whose profile was deactivated is skipped", async () => {
  const result = await getProd4AnalysisJob(db([[{ job_id: "4", status: "done", profile_active: false, match_current: true }]]), "tenant", "123", "4");
  assert.equal(result.status, "skipped");
  assert.match(result.error, /perfil/);
});
test("completed worker with current match is done", async () => {
  const result = await getProd4AnalysisJob(db([[{ job_id: "4", status: "done", profile_active: true, match_current: true }]]), "tenant", "123", "4");
  assert.deepEqual(result, { job_id: "4", status: "done" });
});
