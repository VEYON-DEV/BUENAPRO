import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as crypto from "node:crypto";
import vm from "node:vm";
import ts from "typescript";
import { prod6QuotationWindow } from "../../lib/procurementSchedule.ts";

// Run the actual service with isolated DB/HTTP dependencies, never a real DB or SEACE.
const compiled = ts.transpileModule(readFileSync(new URL("./seaceDetail.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function harness(detail: unknown, options: { fresh?: boolean; fails?: boolean } = {}) {
  const updates: Array<{ sql: string; values: unknown[] }> = [];
  let requests = 0;
  const exports: { refreshContractDetailIfStale?: (id: number) => Promise<void> } = {};
  const query = async (sql: string, values: unknown[]) => {
    if (sql.includes("SELECT detail_fetched_at")) return { rows: [{ missing: false, detail_fetched_at: options.fresh ? new Date() : new Date(0) }] };
    updates.push({ sql, values });
    return { rows: [] };
  };
  vm.runInNewContext(compiled, {
    exports,
    require: (id: string) => {
      if (id === "node:crypto") return crypto;
      if (id === "@/server/db/client") return { query };
      if (id === "@/lib/procurementSchedule") return { prod6QuotationWindow };
      throw new Error(`Unexpected service dependency: ${id}`);
    },
    process: { env: { SEACE_BASE_URL: "https://example.invalid/qa" } },
    AbortController, setTimeout, clearTimeout,
    fetch: async () => {
      requests += 1;
      if (options.fails) return { ok: false };
      return { ok: true, json: async () => detail };
    },
  });
  return { refresh: exports.refreshContractDetailIfStale!, updates, requests: () => requests };
}

test("web refresh writes extended quotation deadline and raw schedule atomically", async () => {
  const detail = { uitContratoEtapaProjectionList: [{ idEtapaContrato: 2, nomEtapaContrato: "ETAPA DE COTIZACIÓN", fecIni: "01/10/2026 08:00:00", fecFin: "06/10/2026 17:30:00" }] };
  const run = harness(detail);
  await run.refresh(9600001);
  assert.equal(run.requests(), 1);
  assert.equal(run.updates.length, 1);
  const update = run.updates[0];
  assert.match(update.sql, /fec_ini_cotizacion = \$11::timestamptz/);
  assert.match(update.sql, /fec_fin_cotizacion = \$12::timestamptz/);
  assert.equal(update.values[10], "2026-10-01T13:00:00.000Z");
  assert.equal(update.values[11], "2026-10-06T22:30:00.000Z");
  assert.deepEqual(JSON.parse(String(update.values[7])), { etapas: detail.uitContratoEtapaProjectionList });
  assert.deepEqual(JSON.parse(String(update.values[9])), detail);
});

test("missing or date-only quotation dates never retain an old exact deadline", async () => {
  for (const detail of [{}, { uitContratoEtapaProjectionList: [{ idEtapaContrato: 2, fecIni: "01/10/2026", fecFin: "02/10/2026" }] }]) {
    const run = harness(detail);
    await run.refresh(9600001);
    assert.equal(run.updates[0].values[10], null);
    assert.equal(run.updates[0].values[11], null);
  }
});

test("fresh cache makes no official request and no DB update", async () => {
  const run = harness({}, { fresh: true });
  await run.refresh(9600001);
  assert.equal(run.requests(), 0);
  assert.equal(run.updates.length, 0);
});

test("failed official request leaves cached detail untouched", async () => {
  const run = harness({}, { fails: true });
  await run.refresh(9600001);
  assert.equal(run.requests(), 1);
  assert.equal(run.updates.length, 0);
});
