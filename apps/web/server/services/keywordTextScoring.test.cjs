const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, 'keywordTextScoring.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const exportsForTest = {};
vm.runInNewContext(compiled, { exports: exportsForTest });
const { keywordTextFitLateralSql } = exportsForTest;

test('keyword SQL binds the resolved profile and leaves no guessed source or documentary score', () => {
  const sql = keywordTextFitLateralSql(2, 'p.description');
  assert.match(sql, /cp\.id = \$2::uuid AND cp\.is_active = true/);
  assert.match(sql, /COALESCE\(p\.description, ''\)/);
  assert.match(sql, /bl\.profile_id = cp\.id AND bl\.is_active = true/);
  // Ante empate de puntos manda la línea prioritaria (sin favoritas el orden es el de siempre: por id).
  assert.match(sql, /ORDER BY keyword_points DESC, is_favorite DESC, id\s+LIMIT 1\s+\) fit ON true/);
  assert.doesNotMatch(sql, /cubso|econ_capacity|fit_score|seace_contracts|prod2_evaluations|worker_jobs|\b(?:INSERT|UPDATE|DELETE)\b/i);
});

test('the priority flag of the line is read with the line and returned next to it, without touching the score', () => {
  const sql = keywordTextFitLateralSql(1, 'p.description');
  assert.match(sql, /SELECT bl\.id, bl\.nombre, bl\.is_favorite, bl\.keyword_phrases, bl\.keyword_terms, cp\.company_keywords/);
  assert.match(sql, /SELECT el\.id, el\.nombre, el\.is_favorite,\s+LEAST\(45,/);
  assert.match(sql, /is_favorite AS business_line_favorite,\s+keyword_points, keyword_points AS fit_points/);
  // It is data about the line, not a signal: no points depend on it.
  assert.doesNotMatch(sql, /is_favorite\s*(?:\+|\*|THEN)|(?:\+|\*)\s*(?:el\.)?is_favorite|CASE WHEN (?:el\.)?is_favorite/);
  assert.doesNotMatch(sql, /LEAST\(45,[^]*is_favorite[^]*?\)::int AS keyword_points/);
});

test('invalid bind positions cannot become SQL fragments', () => {
  for (const value of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, '1 OR true']) {
    assert.throws(() => keywordTextFitLateralSql(value, 'p.description'), /positive integer/);
  }
  assert.throws(() => keywordTextFitLateralSql(1, '   '), /expression is required/);
});

// Opt in only with an isolated QA database. Fixtures live in temporary tables
// that shadow the public names; no public table or sequence is written.
test('PostgreSQL keyword affinity handles roots, deduplication, caps, profile scope and live edits', {
  skip: !process.env.DATABASE_URL,
}, async (t) => {
  const pg = require('pg');
  const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(`CREATE TEMP TABLE company_profiles (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
      company_keywords text[] NOT NULL DEFAULT '{}', is_active boolean NOT NULL DEFAULT true
    ) ON COMMIT DROP`);
    await client.query(`CREATE TEMP TABLE business_lines (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), profile_id uuid NOT NULL,
      nombre text NOT NULL, keyword_phrases text[] NOT NULL DEFAULT '{}',
      keyword_terms text[] NOT NULL DEFAULT '{}', is_active boolean NOT NULL DEFAULT true,
      is_favorite boolean NOT NULL DEFAULT false
    ) ON COMMIT DROP`);
    const tenantId = '00000000-0000-0000-0000-000000000001';
    const otherTenantId = '00000000-0000-0000-0000-000000000002';
    const makeProfile = async (tenant, keywords = []) => (await client.query(
      `INSERT INTO company_profiles (tenant_id, company_keywords)
       VALUES ($1, $2::text[]) RETURNING id`, [tenant, keywords],
    )).rows[0].id;
    const profileId = await makeProfile(tenantId, ['nube', 'software']);
    const otherId = await makeProfile(otherTenantId);
    const noLinesId = await makeProfile(tenantId);
    const makeLine = async (profile, name, phrases = [], terms = [], active = true) => (await client.query(
      `INSERT INTO business_lines (profile_id, nombre, keyword_phrases, keyword_terms, is_active)
       VALUES ($1, $2, $3::text[], $4::text[], $5) RETURNING id`, [profile, name, phrases, terms, active],
    )).rows[0].id;
    const lineId = await makeLine(profileId, 'Infraestructura', ['licencia de software', 'licencias de software'], ['servidor', 'servidores', 'software']);
    await makeLine(otherId, 'Línea de otra empresa', ['licencia de software', 'servicio de nube'], ['servidor', 'software', 'nube']);
    await makeLine(profileId, 'Línea inactiva', ['licencia de software', 'servicio de nube'], ['servidor', 'software', 'nube'], false);
    const score = async (description, profile = profileId) => (await client.query(
      `SELECT fit.* FROM (VALUES ($2::text)) p(description) ${keywordTextFitLateralSql(1, 'p.description')}`,
      [profile, description],
    )).rows[0];

    await t.test('inflections share roots, a phrase covers its terms, company bonus is independent', async () => {
      const fit = await score('Licencias de software, servidores y nube');
      assert.equal(fit.business_line_id, lineId);
      assert.equal(fit.keyword_points, 35);
      assert.equal(fit.fit_points, 35);
      assert.equal(fit.fit_level, 3);
      assert.deepEqual(fit.keyword_hits.map(hit => hit.match).sort(), ['company_keyword', 'exact_phrase', 'strong_term']);
      assert.equal(fit.keyword_hits.filter(hit => hit.match === 'exact_phrase').length, 1);
      assert.equal(fit.keyword_hits.filter(hit => hit.match === 'strong_term').length, 1);
      assert.equal('fit_score' in fit, false);
    });

    await t.test('separated phrase is not reassembled and lexical boundaries avoid substrings', async () => {
      const fit = await score('Licencia especializada de software');
      assert.ok(!fit.keyword_hits.some(hit => hit.match === 'exact_phrase'));
      assert.equal(fit.keyword_points, 10);
      assert.equal(fit.fit_level, 2);
      assert.ok(!(await score('Software de licencia')).keyword_hits.some(hit => hit.match === 'exact_phrase'));
      await client.query('UPDATE business_lines SET keyword_terms = ARRAY[\'legal\'] WHERE id = $1', [lineId]);
      assert.equal((await score('Actividades ilegales')).keyword_points, 0);
      assert.equal((await score('Asesorías legales')).keyword_points, 10);
      await client.query('UPDATE business_lines SET keyword_terms = $2::text[] WHERE id = $1', [lineId, ['servidor', 'servidores', 'software']]);
    });

    await t.test('profile with no lines or inactive profile is unknown; known line without hits is low', async () => {
      const noMatch = await score('Compra de útiles de oficina');
      assert.equal(noMatch.keyword_points, 0);
      assert.equal(noMatch.fit_level, 1);
      assert.deepEqual(noMatch.keyword_hits, []);
      assert.equal((await score('software', noLinesId)).business_line_id, null);
      assert.equal((await score('software', null)).fit_level, null);
      await client.query('UPDATE company_profiles SET is_active = false WHERE id = $1', [profileId]);
      assert.equal((await score('software')).business_line_id, null);
      await client.query('UPDATE company_profiles SET is_active = true WHERE id = $1', [profileId]);
    });

    await t.test('terms do not combine across lines; ties have stable identity order', async () => {
      await client.query('UPDATE company_profiles SET company_keywords = ARRAY[]::text[] WHERE id = $1', [profileId]);
      await client.query('UPDATE business_lines SET is_active = false WHERE id = $1', [lineId]);
      const first = await makeLine(profileId, 'Soporte', [], ['servidor']);
      const second = await makeLine(profileId, 'Software', [], ['software']);
      const fit = await score('Servidor y software');
      assert.equal(fit.keyword_points, 10);
      assert.equal(fit.fit_level, 2);
      assert.equal(fit.business_line_id, [first, second].sort()[0]);
      assert.equal(fit.keyword_hits.length, 1);
      await client.query('UPDATE business_lines SET is_active = false WHERE id = ANY($1::uuid[])', [[first, second]]);
    });

    await t.test('the priority flag travels with the chosen line, never adds points and only breaks ties between equal points', async () => {
      const redes = await makeLine(profileId, 'Redes', [], ['redes']);
      const nube = await makeLine(profileId, 'Nube', [], ['nube']);
      const [lowId, highId] = [redes, nube].sort();
      const read = async text => (await score(text));
      // Without a priority line the winner is the lowest id and the flag is false (same as before this column existed).
      let fit = await read('Redes y nube');
      assert.deepEqual([fit.business_line_id, fit.keyword_points, fit.business_line_favorite], [lowId, 10, false]);
      // Marking the other line moves the tie to it, with the same points.
      await client.query('UPDATE business_lines SET is_favorite = true WHERE id = $1', [highId]);
      fit = await read('Redes y nube');
      assert.deepEqual([fit.business_line_id, fit.keyword_points, fit.business_line_favorite], [highId, 10, true]);
      assert.equal(fit.business_line_name, highId === redes ? 'Redes' : 'Nube');
      // More points still win over a priority line: it never outbids a better match.
      await client.query('UPDATE business_lines SET keyword_terms = ARRAY[\'redes\', \'cableado\'] WHERE id = $1', [redes]);
      await client.query('UPDATE business_lines SET is_favorite = (id = $2) WHERE id = ANY($1::uuid[])', [[redes, nube], nube]);
      fit = await read('Redes, cableado y nube');
      assert.deepEqual([fit.business_line_id, fit.keyword_points, fit.business_line_favorite], [redes, 20, false]);
      // A priority line with no hits is still returned by the lateral (zero points), so callers must ask for points > 0.
      fit = await read('Compra de útiles de oficina');
      assert.deepEqual([fit.keyword_points, fit.business_line_favorite, fit.business_line_id], [0, true, nube]);
      // An inactive priority line and a priority line of another profile do not take part.
      await client.query('UPDATE business_lines SET is_active = false WHERE id = $1', [nube]);
      fit = await read('Redes y nube');
      assert.deepEqual([fit.business_line_id, fit.business_line_favorite], [redes, false]);
      await client.query('UPDATE business_lines SET is_active = true, is_favorite = false WHERE id = $1', [nube]);
      await client.query('UPDATE business_lines SET is_favorite = true WHERE profile_id = $1', [otherId]);
      fit = await read('Redes y nube');
      assert.equal(fit.business_line_favorite, false);
      await client.query('UPDATE business_lines SET is_active = false, is_favorite = false WHERE id = ANY($1::uuid[])', [[redes, nube]]);
    });

    await t.test('term and overall caps hold; edits affect the next read without a cache', async () => {
      await client.query('UPDATE business_lines SET is_active = true, keyword_phrases = ARRAY[]::text[], keyword_terms = $2::text[] WHERE id = $1',
        [lineId, ['software', 'servidor', 'nube', 'arbitraje']]);
      assert.equal((await score('Software, servidor, nube y arbitraje')).keyword_points, 30);
      await client.query('UPDATE business_lines SET keyword_phrases = $2::text[], keyword_terms = ARRAY[]::text[] WHERE id = $1',
        [lineId, ['asesoría legal', 'defensa judicial', 'derecho administrativo', 'arbitraje internacional']]);
      assert.equal((await score('Asesoría legal, defensa judicial, derecho administrativo y arbitraje internacional')).keyword_points, 45);
      await client.query('UPDATE business_lines SET keyword_phrases = ARRAY[]::text[], keyword_terms = ARRAY[]::text[] WHERE id = $1', [lineId]);
      await client.query('UPDATE company_profiles SET company_keywords = $2::text[] WHERE id = $1', [profileId, ['software', 'nube']]);
      const identityOnly = await score('Software y nube');
      assert.equal(identityOnly.keyword_points, 10);
      assert.equal(identityOnly.keyword_hits.length, 1);
      await client.query('UPDATE company_profiles SET company_keywords = ARRAY[]::text[] WHERE id = $1', [profileId]);
      assert.equal((await score('Software y nube')).keyword_points, 0);
    });
  } finally {
    await client.query('ROLLBACK');
    client.release();
    await pool.end();
  }
});
