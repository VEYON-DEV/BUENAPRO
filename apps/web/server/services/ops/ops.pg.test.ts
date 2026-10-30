import test from "node:test";
import assert from "node:assert/strict";
import pg from "pg";
import { columnSteps, costsSql, countsSql, failureSteps, failuresSql, itemsSql, STEPS } from "./steps.ts";
import { MAJOR_QUERIES, MINOR_QUERIES } from "./traceQueries.ts";

/**
 * Ejecuta todo el SQL del panel contra una base real (por ejemplo, la copia local) dentro de una transacción
 * de solo lectura que siempre se deshace. Se omite sin DATABASE_URL.
 */
const databaseUrl = process.env.DATABASE_URL;

async function withReadOnly<T>(task: (client: pg.PoolClient) => Promise<T>): Promise<T> {
  const pool = new pg.Pool({ connectionString: databaseUrl, max: 1 });
  const client = await pool.connect();
  try {
    await client.query("BEGIN READ ONLY");
    return await task(client);
  } finally {
    await client.query("ROLLBACK").catch(() => undefined);
    client.release();
    await pool.end();
  }
}

const WINDOW = ["2026-07-01", "2026-10-30", null, 5] as const;

test("todo el SQL del panel se ejecuta contra PostgreSQL con los tipos de parámetros esperados", { skip: !databaseUrl }, async () => {
  await withReadOnly(async (client) => {
    const run = async (name: string, sql: string, values: unknown[]) => {
      try {
        return (await client.query(sql, values)).rows;
      } catch (error) {
        throw new Error(`${name}: ${error instanceof Error ? error.message : error}`);
      }
    };
    for (const source of ["menores", "mayores"] as const) {
      const counts = await run(`counts ${source}`, countsSql(columnSteps(source)), [...WINDOW]);
      for (const row of counts) assert.match(row.day, /^\d{4}-\d{2}-\d{2}$/);
      await run(`failures ${source}`, failuresSql(failureSteps(source)), [...WINDOW]);
    }
    await run("costs", costsSql(), [...WINDOW]);
    for (const step of STEPS) {
      const rows = await run(`items ${step.source}/${step.key}`, itemsSql(step), [...WINDOW]);
      assert.ok(rows.length <= 5, step.key);
    }
    for (const [name, sql] of Object.entries(MINOR_QUERIES)) await run(`trace menores.${name}`, sql, ["104188"]);
    for (const [name, sql] of Object.entries(MAJOR_QUERIES)) await run(`trace mayores.${name}`, sql, ["1034924"]);
  });
});

test("el mismo filtro de empresa reduce los pasos por empresa y no toca los compartidos", { skip: !databaseUrl }, async () => {
  await withReadOnly(async (client) => {
    const none = "00000000-0000-4000-8000-000000000000";
    const total = async (sql: string, company: string | null) =>
      (await client.query(sql, [WINDOW[0], WINDOW[1], company, 5])).rows.reduce((sum, row) => sum + Number(row.n), 0);
    const perCompany = countsSql(columnSteps("menores").filter((step) => step.key === "jev_rejected"));
    const shared = countsSql(columnSteps("menores").filter((step) => step.key === "read_ok"));
    assert.equal(await total(perCompany, none), 0);
    assert.equal(await total(shared, none), await total(shared, null));
  });
});

test("la base rechaza cualquier escritura dentro de la transacción del panel", { skip: !databaseUrl }, async () => {
  await withReadOnly(async (client) => {
    await assert.rejects(client.query("CREATE TEMP TABLE ops_no_deberia_existir (x int)"), /read-only transaction/);
  });
});
