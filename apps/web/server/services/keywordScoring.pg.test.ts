import test from "node:test";
import assert from "node:assert/strict";
import pg from "pg";
import { keywordFitLateralSql } from "./keywordScoring.ts";

const databaseUrl = process.env.DATABASE_URL;

test("PostgreSQL aplica raíces españolas sin separar frases", { skip: !databaseUrl }, async () => {
  const pool = new pg.Pool({ connectionString: databaseUrl });
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const tenant = await client.query("INSERT INTO tenants (name) VALUES ('keyword-scoring-test') RETURNING id");
    const profile = await client.query(
      `INSERT INTO company_profiles (tenant_id, ruc, razon_social, company_keywords)
       VALUES ($1, '00000000000', 'Test', ARRAY['abogado','legal']) RETURNING id`,
      [tenant.rows[0].id],
    );
    await client.query(
      `INSERT INTO business_lines (profile_id, nombre, cubso_segmentos, keywords, keyword_phrases, keyword_terms)
       VALUES ($1, 'Servicios profesionales', ARRAY['80'], ARRAY['asesoría legal','arbitraje'], ARRAY['asesoría legal'], ARRAY['arbitraje'])`,
      [profile.rows[0].id],
    );

    const score = async (description: string, segment = "80") => {
      const result = await client.query(
        `SELECT fit.*
         FROM (VALUES ($2::text, $3::text)) c(descripcion, cubso_segmento)
         ${keywordFitLateralSql(1)}`,
        [tenant.rows[0].id, description, segment],
      );
      return result.rows[0] ?? null;
    };

    const inflected = await score("Abogadas brindan asesorías legales y arbitrajes");
    assert.equal(inflected.keyword_points, 35);
    assert.deepEqual(inflected.keyword_hits.map((hit: { match: string }) => hit.match).sort(), ["company_keyword", "exact_phrase", "strong_term"]);

    const separated = await score("Abogadas brindan asesoría especializada en asuntos legales y arbitraje");
    assert.equal(separated.keyword_points, 20);
    assert.ok(!separated.keyword_hits.some((hit: { match: string }) => hit.match === "exact_phrase"));

    assert.equal((await score("Actividades ilegales"))?.keyword_points, 0);
    assert.equal((await score("Abogadas brindan asesorías legales", "81"))?.business_line_id, null);
  } finally {
    await client.query("ROLLBACK");
    client.release();
    await pool.end();
  }
});
