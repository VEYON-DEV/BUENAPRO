import test from "node:test";
import assert from "node:assert/strict";
import pg from "pg";

const databaseUrl = process.env.DATABASE_URL;

test("guardadas son idempotentes y quedan aisladas por tenant", { skip: !databaseUrl }, async () => {
  const pool = new pg.Pool({ connectionString: databaseUrl });
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const tenantA = await client.query("INSERT INTO tenants (name) VALUES ('saved-test-a') RETURNING id");
    const tenantB = await client.query("INSERT INTO tenants (name) VALUES ('saved-test-b') RETURNING id");
    const contract = await client.query(`
      INSERT INTO seace_contracts (
        id_contrato,codigo,anio,objeto_codigo,estado_codigo,descripcion,
        valor_no_informado,raw_search_json,hash_search
      ) VALUES (-16001,'SAVED-TEST-16001',2026,2,2,'Fixture guardadas',true,'{}'::jsonb,'saved-test-16001')
      RETURNING id_contrato
    `);
    const idContrato = contract.rows[0].id_contrato;

    await client.query(
      `INSERT INTO saved_contracts (tenant_id,id_contrato) VALUES ($1,$2)
       ON CONFLICT (tenant_id,id_contrato) DO NOTHING`,
      [tenantA.rows[0].id, idContrato],
    );
    await client.query(
      `INSERT INTO saved_contracts (tenant_id,id_contrato) VALUES ($1,$2)
       ON CONFLICT (tenant_id,id_contrato) DO NOTHING`,
      [tenantA.rows[0].id, idContrato],
    );

    const counts = await client.query(
      `SELECT tenant_id,count(*)::int AS count
       FROM saved_contracts WHERE id_contrato=$1 GROUP BY tenant_id`,
      [idContrato],
    );
    assert.deepEqual(counts.rows, [{ tenant_id: tenantA.rows[0].id, count: 1 }]);
    assert.notEqual(tenantA.rows[0].id, tenantB.rows[0].id);

    await client.query(
      "DELETE FROM saved_contracts WHERE tenant_id=$1 AND id_contrato=$2",
      [tenantA.rows[0].id, idContrato],
    );
    const remaining = await client.query(
      "SELECT count(*)::int AS count FROM saved_contracts WHERE id_contrato=$1",
      [idContrato],
    );
    assert.equal(remaining.rows[0].count, 0);
  } finally {
    await client.query("ROLLBACK");
    client.release();
    await pool.end();
  }
});
