import test from "node:test";
import assert from "node:assert/strict";
import pg from "pg";

const databaseUrl = process.env.DATABASE_URL;

test("PROD4 preliminary fit uses tenant segments and item text without guessing eligibility", { skip: !databaseUrl }, async () => {
  const pool = new pg.Pool({ connectionString: databaseUrl });
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const tenant = await client.query("INSERT INTO tenants (name) VALUES ('prod4-affinity-test') RETURNING id");
    const profile = await client.query(
      `INSERT INTO company_profiles (tenant_id, ruc, razon_social, company_keywords)
       VALUES ($1, '00000000000', 'Proveedor de prueba', ARRAY['nube']) RETURNING id`,
      [tenant.rows[0].id],
    );
    await client.query(
      `INSERT INTO business_lines (profile_id, nombre, cubso_segmentos, keyword_phrases, keyword_terms)
       VALUES ($1, 'Infraestructura TI', ARRAY['43'], ARRAY['licencia de software'], ARRAY['servidor'])`,
      [profile.rows[0].id],
    );
    const opportunity = await client.query(
      `INSERT INTO opportunities (object_type, procurement_method)
       VALUES ('good', 'selection_procedure') RETURNING id`,
    );
    const id = -Math.floor(Date.now() / 1000);
    await client.query(
      `INSERT INTO prod4_processes (id_procedimiento, opportunity_id, object_type, title, source_url, technology_relevant)
       VALUES ($1, $2, 'good', 'Adquisición de infraestructura', 'https://example.org/prod4', true)`,
      [id, opportunity.rows[0].id],
    );
    await client.query(
      `INSERT INTO prod4_items (id_procedimiento, nro_item, cubso_code, description)
       VALUES ($1, 1, '43210000', 'Licencias de software y servidores en la nube')`,
      [id],
    );

    const fit = await client.query(
      "SELECT * FROM profile_prod4_fit($1, $2)",
      [profile.rows[0].id, id],
    );
    assert.equal(fit.rows.length, 1);
    assert.equal(fit.rows[0].business_line_name, "Infraestructura TI");
    assert.equal(fit.rows[0].keyword_points, 35);
    assert.equal(fit.rows[0].fit_points, 35);
    assert.equal(fit.rows[0].fit_score, 85);
    assert.equal(fit.rows[0].fit_level, 3);
    assert.deepEqual(
      fit.rows[0].keyword_hits.map((hit: { match: string }) => hit.match).sort(),
      ["company_keyword", "exact_phrase", "strong_term"],
    );

    await client.query(
      "UPDATE prod4_items SET cubso_code = '81110000' WHERE id_procedimiento = $1",
      [id],
    );
    const unrelated = await client.query("SELECT * FROM profile_prod4_fit($1, $2)", [profile.rows[0].id, id]);
    assert.equal(unrelated.rows.length, 0);
  } finally {
    await client.query("ROLLBACK");
    client.release();
    await pool.end();
  }
});
