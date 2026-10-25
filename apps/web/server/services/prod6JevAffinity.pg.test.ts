import test from "node:test";
import assert from "node:assert/strict";
import pg from "pg";
import { jevByCompanySql, jevListLateralSql } from "./prod6JevAffinity.ts";

const databaseUrl = process.env.DATABASE_URL;

test("JEV affinity SQL picks the latest decision per contract and profile inside a rolled-back transaction", { skip: !databaseUrl }, async () => {
  const pool = new pg.Pool({ connectionString: databaseUrl });
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const tenant = (await client.query("INSERT INTO tenants (name) VALUES ('jev-affinity-test') RETURNING id")).rows[0].id;
    const otherTenant = (await client.query("INSERT INTO tenants (name) VALUES ('jev-affinity-other') RETURNING id")).rows[0].id;
    let ruc = 20000000000;
    const profile = async (tenantId: string, name: string, active = true) => (await client.query(
      "INSERT INTO company_profiles (tenant_id, ruc, razon_social, is_active) VALUES ($1, $2, $3, $4) RETURNING id::text",
      [tenantId, String(++ruc), name, active])).rows[0].id as string;
    const alpha = await profile(tenant, "Alfa SAC");
    const beta = await profile(tenant, "Beta SAC");
    const inactive = await profile(tenant, "Inactiva SAC", false);
    const foreign = await profile(otherTenant, "Ajena SAC");
    const contract = async (id: number) => client.query(
      `INSERT INTO seace_contracts (id_contrato,codigo,anio,objeto_codigo,estado_codigo,descripcion,valor_no_informado,raw_search_json,hash_search)
       VALUES ($1,$2,2026,2,2,'Fixture JEV',true,'{}'::jsonb,$2)`, [id, `JEV-TEST-${-id}`]);
    await contract(-17001);
    await contract(-17002);

    let policy = 0;
    const decide = (profileId: string, idContrato: number, decision: string, decidedBy: string, score: number | null, updatedAt: string,
      extra: { subcategory?: string; domain?: string; information?: string } = {}) => client.query(
      `INSERT INTO prod6_profile_decisions
         (id_contrato, profile_id, listing_json, contract_hash, profile_hash, policy_version, prefilter_passed, decision, decided_by,
          score, threshold, subcategory, domain, information, updated_at)
       VALUES ($1,$2,'{}'::jsonb,'c','p',$3,true,$4,$5,$6,60,$7,$8,$9,$10::timestamptz)`,
      [idContrato, profileId, `v${++policy}`, decision, decidedBy, score, extra.subcategory ?? null, extra.domain ?? null, extra.information ?? null, updatedAt]);

    // Alfa: an older JEV rejection, a newer JEV approval and an even newer prefilter row. JEV wins, then the newest JEV.
    await decide(alpha, -17001, "rejected", "jev", 40.5, "2026-10-25T10:00:00Z", { subcategory: "otro", domain: "otro", information: "claro" });
    await decide(alpha, -17001, "accepted", "jev", 80.25, "2026-10-25T11:00:00Z", { subcategory: "soporte_ti", domain: "informatica", information: "claro" });
    await decide(alpha, -17001, "rejected", "prefilter", null, "2026-10-25T12:00:00Z");
    // Beta: two JEV rows with the same timestamp; the higher id wins.
    await decide(beta, -17001, "rejected", "jev", 20, "2026-10-25T09:00:00Z");
    await decide(beta, -17001, "accepted", "jev", 65.75, "2026-10-25T09:00:00Z", { subcategory: "hardware_ti", domain: "informatica", information: "insuficiente" });
    // Never listed: inactive profile and other tenant.
    await decide(inactive, -17001, "accepted", "jev", 99, "2026-10-25T09:00:00Z");
    await decide(foreign, -17001, "accepted", "jev", 98, "2026-10-25T09:00:00Z");

    const listed = async (profileId: string | null, idContrato: number) => (await client.query(
      `SELECT c.id_contrato, jev.* FROM seace_contracts c ${jevListLateralSql(1)} WHERE c.id_contrato = $2`, [profileId, idContrato])).rows[0];

    const alphaRow = await listed(alpha, -17001);
    assert.equal(alphaRow.jev_score, 80.25, "numeric is returned as a number");
    assert.equal(alphaRow.jev_threshold, 60);
    assert.equal(alphaRow.jev_decision, "accepted");
    assert.equal(alphaRow.jev_subcategory, "soporte_ti");
    assert.ok(alphaRow.jev_decided_at instanceof Date);
    const betaRow = await listed(beta, -17001);
    assert.equal(betaRow.jev_score, 65.75);
    assert.equal(betaRow.jev_subcategory, "hardware_ti");

    // Without any decision, or without a selected profile, the contract still appears with null JEV fields.
    for (const row of [await listed(alpha, -17002), await listed(null, -17001)]) {
      assert.ok(row, "the LEFT JOIN keeps the contract");
      assert.deepEqual([row.jev_score, row.jev_decision, row.jev_subcategory, row.jev_threshold, row.jev_decided_at], [null, null, null, null, null]);
    }
    // A profile whose only row is a prefilter decision has no score and is not turned into zero.
    await decide(inactive, -17002, "rejected", "prefilter", null, "2026-10-25T09:00:00Z");
    assert.equal((await listed(inactive, -17002)).jev_score, null);

    const byCompany = async (idContrato: number, current: string | null) =>
      (await client.query(jevByCompanySql(1, 2, 3), [idContrato, tenant, current])).rows;

    const forAlpha = await byCompany(-17001, alpha);
    assert.deepEqual(forAlpha.map((row) => row.profile_name), ["Alfa SAC", "Beta SAC"], "only active profiles of the tenant");
    assert.equal(forAlpha[0].is_current_profile, true);
    assert.equal(forAlpha[0].score, 80.25);
    assert.equal(forAlpha[0].decided_by, "jev");
    assert.equal(forAlpha[0].information, "claro");
    assert.equal(forAlpha[1].is_current_profile, false);
    assert.equal(forAlpha[1].information, "insuficiente");

    // The selected profile goes first even with a lower score; the others follow by score.
    assert.deepEqual((await byCompany(-17001, beta)).map((row) => row.profile_name), ["Beta SAC", "Alfa SAC"]);
    assert.deepEqual((await byCompany(-17001, null)).map((row) => row.profile_name), ["Alfa SAC", "Beta SAC"]);
    assert.deepEqual(await byCompany(-17002, alpha), [], "a contract without decisions returns no rows");
  } finally {
    await client.query("ROLLBACK");
    client.release();
    await pool.end();
  }
});
