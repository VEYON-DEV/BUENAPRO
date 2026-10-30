const { test, describe, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { createHash, randomUUID } = require('node:crypto');
const { loadCommercialModule: load } = require('./commercialClassification.test-support.cjs');
const api = load('prod2CommercialAffinity.ts');
const sha = value => createHash('sha256').update(value).digest('hex');
const profile = '00000000-0000-0000-0000-000000000001', tenant = '00000000-0000-0000-0000-000000000002';
const profileHash = 'b'.repeat(64);
const currentDinProfileHash = '8764a9095aed779deb83799b6f2616a9';
const plain = value => JSON.parse(JSON.stringify(value));

test('commercial context normalizes exactly one current company without RUC, RNP, professionals or financial evidence', () => {
  const current = api.prod2CommercialContext({ profile_hash: profileHash, commercial_description: '  Ingeniería\u00a0industrial\n', active_business_lines: [' Tableros  eléctricos ', 'Cloud', 'Tableros eléctricos', ''] });
  assert.deepEqual(plain(current), { profileHash, commercialHash: sha(JSON.stringify([{ descripcion: 'Ingeniería industrial', lineas: ['Cloud', 'Tableros eléctricos'] }])) });
  assert.equal(current.commercialHash, '0b8879d8e6952dde9f640f437055fe03c55bd5fccfac160bced8c9f9602af102', 'Independent Python canonical JSON fixture');
  for (const value of [{}, { profile_hash: profileHash }, { profile_hash: 'old', commercial_description: 'a', active_business_lines: [] },
    { profile_hash: profileHash, commercial_description: '', active_business_lines: [] }]) assert.equal(api.prod2CommercialContext(value), null);
});

test('retains the actual current DIN 32-hex profile identity and rejects malformed versioned hashes', () => {
  const fields = { commercial_description: 'Industrial', active_business_lines: ['Tableros'] };
  const current = api.prod2CommercialContext({ ...fields, profile_hash: currentDinProfileHash });
  assert.equal(current.profileHash, currentDinProfileHash, 'Never recalculate or expand an existing profile hash');
  const values = [profile];
  const sql = api.prod2CommercialAffinityJoin(values, current, tenant);
  assert.deepEqual(values, [profile, tenant, currentDinProfileHash, current.commercialHash]);
  assert.match(sql, /profile_hash' = \$3::text/);
  for (const malformed of ['a'.repeat(31), 'a'.repeat(33), 'a'.repeat(63), 'a'.repeat(65), 'g'.repeat(32), 'z'.repeat(64), ' '+currentDinProfileHash]) {
    const rejected = api.prod2CommercialContext({ ...fields, profile_hash: malformed });
    assert.equal(rejected, null);
    assert.match(api.prod2CommercialAffinityJoin([profile], rejected, tenant), /NULL::double precision/);
  }
});

test('strict commercial JEV SQL binds current profile/tenant/version/title and never becomes a documentary score predicate', () => {
  const values = [profile, 'existing-filter'];
  const sql = api.prod2CommercialAffinityJoin(values, { profileHash, commercialHash: 'c'.repeat(64) }, tenant);
  assert.deepEqual(values, [profile, 'existing-filter', tenant, profileHash, 'c'.repeat(64)]);
  for (const pattern of [/ev\.profile_id = \$1::uuid/, /ev\.policy_version = 'din_title_commercial_affinity_v1'/,
    /tenant_id' = \$3::text/, /profile_hash' = \$4::text/, /commercial_profile_hash' = \$5::text/,
    /source_row_hash' = p\.row_hash/, /source_description_sha256.*sha256\(convert_to\(p\.description/,
    /canonical_binding_hash' = ev\.request_hash/, /ev\.input_tokens >= 0/, /BETWEEN 0 AND 100/]) assert.match(sql, pattern);
  assert.doesNotMatch(sql, /opportunity_matches|gemini_match|score_validated/);
  const none = [profile];
  assert.match(api.prod2CommercialAffinityJoin(none, null, tenant), /NULL::double precision/);
  assert.deepEqual(none, [profile]);
});

test('list orders by the strict 32/64-hex bound JEV affinity but neither list nor detail publish it', async () => {
  for (const currentHash of [currentDinProfileHash, profileHash]) {
  const statements = [];
  const service = load('prod2Opportunities.ts', {
    '@/server/db/client': { query: async (text, values) => {
      statements.push({ text, values });
      if (text.includes('SELECT id::text, razon_social FROM company_profiles')) return { rows: [{ id: profile, razon_social: 'DIN' }] };
      if (text.includes('SELECT cp.profile_hash')) return { rows: [{ profile_hash: currentHash, commercial_description: 'Industrial', active_business_lines: ['Tableros'] }] };
      if (text.includes('count(*)::int AS total FROM prod2_discoveries')) return { rows: [{ total: 1 }] };
      if (text.includes('FROM prod2_discoveries')) return { rows: [{ id_convocatoria: '123', opportunity_id: 'op', match_id: null, match_score: null }] };
      return { rows: [] };
    } },
    '@/lib/prod2Analysis': { normalizeProd2Schedule: () => [], buildProd2Analysis: () => ({ match: null }) },
    './prod2DocumentStorage': { prod2StorageAvailable: () => false }, './prod2OfficialDocument': {},
    './keywordTextScoring': { keywordTextFitLateralSql: () => '' }, './savedOpportunities': { savedOpportunityIds: async () => new Set() },
  });
  const result = await service.listProd2OpportunitiesForTenant(tenant, { object: null, q: '', page: 1, pageSize: 5, profileId: profile, withScore: true });
  const detail = await service.getProd2OpportunityForTenant(tenant, '123', profile);
  for (const row of [result.data[0], detail.process]) {
    for (const key of ['commercial_affinity', 'affinity', 'commercial_jev_result', 'commercial_affinity_index']) assert.equal(key in row, false, key);
    assert.equal(row.match_score, null); assert.equal(row.provisional_score, null);
  }
  const selections = statements.filter(row => row.text.includes('FROM prod2_discoveries'));
  for (const row of selections.slice(0, 2)) assert.match(row.text, /matched\.id IS NOT NULL AND matched\.score BETWEEN 0 AND 100/);
  assert.doesNotMatch(selections[0].text, /commercial_jev/);
  assert.ok(selections[1].values.includes(currentHash), 'Strict page query must retain the current profile identity');
  assert.doesNotMatch(selections[1].text, /commercial_jev\.result_json|AS commercial_affinity_index/, 'the affinity only orders; it is never selected');
  assert.doesNotMatch(selections[2].text, /commercial_jev/, 'the detail does not read the internal affinity');
  assert.match(selections[1].text, /matched\.score ELSE NULL END DESC NULLS LAST, jev_union\.affinity_index DESC NULLS LAST, commercial_jev\.affinity_index DESC NULLS LAST/);
  assert.deepEqual(plain(selections[1].values.slice(-2)), [5, 0]);
  }
});

describe('strict commercial JEV actual SQL in disposable loopback PG', { skip: !process.env.BUENAPRO_COMMERCIAL_TEST_DATABASE_URL }, () => {
  let db;
  const schema = 'affinity_'+randomUUID().replaceAll('-', '');
  before(async () => {
    const dsn = new URL(process.env.BUENAPRO_COMMERCIAL_TEST_DATABASE_URL);
    assert.equal(dsn.hostname, '127.0.0.1'); assert.equal(dsn.pathname, '/commercial_taxonomy_qa');
    const { Client } = require('pg'); db = new Client({ connectionString: dsn.href }); await db.connect();
    await db.query(`CREATE SCHEMA ${schema}; SET search_path TO ${schema};
      CREATE TABLE prod2_discoveries (id_convocatoria bigint PRIMARY KEY, description text, row_hash text);
      CREATE TABLE prod2_evaluations (id bigserial, id_convocatoria bigint, kind text, status text, profile_id uuid,
        model text, policy_version text, result_json jsonb, request_hash text, input_tokens int, output_tokens int,
        cost_usd numeric, completed_at timestamptz)`);
  });
  after(async () => { if (db) { try { await db.query(`DROP SCHEMA ${schema} CASCADE`); } finally { await db.end(); } } });
  test('rejects legacy union, other/stale profiles, changed source, missing/uncertain usage and invalid numbers while retaining zero', async () => {
    const context = { profileHash, commercialHash: 'c'.repeat(64) };
    const title = 'ADQUISICIÓN DE TABLEROS';
    const cases = [{}, { score: 0 }, { policy: 'prod2_commercial_union_v1' }, { profile_id: null }, { profile_id: tenant },
      { meta: { tenant_id: profile } }, { meta: { profile_hash: 'old' } }, { meta: { commercial_profile_hash: 'old' } },
      { meta: { source_row_hash: 'old' } }, { meta: { source_description_sha256: 'old' } }, { meta: { id_convocatoria: 'other' } },
      { meta: { scope: 'commercial_union' } }, { meta: { analysis_basis: 'pdf' } }, { meta: { canonical_binding_hash: 'old' } },
      { status: 'uncertain' }, { input_tokens: null }, { model: 'other' }, { result: { policy_version: 'old' } },
      { score: -1 }, { score: 101 }, { score: '90' }, { score: null }, { score: { fabricated: 90 } }];
    for (const [index, patch] of cases.entries()) {
      const id = 4001+index;
      const meta = { scope: 'din_title_commercial_affinity_v1', analysis_basis: 'title', profile_id: profile, tenant_id: tenant,
        profile_hash: profileHash, commercial_profile_hash: context.commercialHash, id_convocatoria: String(id),
        source_row_hash: 'current-row', source_description_sha256: sha(title), canonical_binding_hash: 'current-request', ...patch.meta };
      const result = { affinity_index: 'score' in patch ? patch.score : 75, model: 'jev-1.13.0', policy_version: 'prod2_commercial_union_v1', not_qualification_or_win_probability: true, meta, ...patch.result };
      await db.query('INSERT INTO prod2_discoveries VALUES($1,$2,$3)', [id, title, 'current-row']);
      await db.query(`INSERT INTO prod2_evaluations(id_convocatoria,kind,status,profile_id,model,policy_version,result_json,request_hash,input_tokens,output_tokens,cost_usd,completed_at)
        VALUES($1,'jev_affinity',$2,$3,$4,$5,$6,'current-request',$7,0,0.00001,now())`,
        [id, patch.status || 'done', 'profile_id' in patch ? patch.profile_id : profile, patch.model || 'jev-1.13.0', patch.policy || 'din_title_commercial_affinity_v1', result, 'input_tokens' in patch ? patch.input_tokens : 100]);
    }
    const values = [profile], join = api.prod2CommercialAffinityJoin(values, context, tenant);
    const result = await db.query(`SELECT p.id_convocatoria, commercial_jev.affinity_index FROM prod2_discoveries p ${join} ORDER BY p.id_convocatoria`, values);
    assert.deepEqual(result.rows.filter(row => row.affinity_index != null).map(row => [Number(row.id_convocatoria), row.affinity_index]), [[4001, 75], [4002, 0]]);
    const ordered = await db.query(`SELECT p.id_convocatoria FROM prod2_discoveries p ${join} ORDER BY commercial_jev.affinity_index DESC NULLS LAST,p.id_convocatoria LIMIT 2`, values);
    assert.deepEqual(ordered.rows.map(row => Number(row.id_convocatoria)), [4001, 4002]);
  });
  test('actual DIN 32-hex profile context admits its strictly bound canonical SQL result and suppresses a stale hash', async () => {
    const context = api.prod2CommercialContext({ profile_hash: currentDinProfileHash, commercial_description: 'Industrial', active_business_lines: ['Tableros'] });
    const id = 5001, title = 'TABLEROS INDUSTRIALES', requestHash = 'current-din-32-request';
    const result = { model: 'jev-1.13.0', policy_version: 'prod2_commercial_union_v1', affinity_index: 62.75,
      not_qualification_or_win_probability: true, meta: { scope: 'din_title_commercial_affinity_v1', analysis_basis: 'title',
        profile_id: profile, tenant_id: tenant, profile_hash: currentDinProfileHash, commercial_profile_hash: context.commercialHash,
        id_convocatoria: String(id), source_row_hash: 'current-din-row', source_description_sha256: sha(title), canonical_binding_hash: requestHash } };
    await db.query('INSERT INTO prod2_discoveries VALUES($1,$2,$3)', [id, title, 'current-din-row']);
    await db.query(`INSERT INTO prod2_evaluations(id_convocatoria,kind,status,profile_id,model,policy_version,result_json,request_hash,input_tokens,output_tokens,cost_usd,completed_at)
      VALUES($1,'jev_affinity','done',$2,'jev-1.13.0','din_title_commercial_affinity_v1',$3,$4,1533,106,0.000064386,now())`, [id, profile, result, requestHash]);
    for (const [boundContext, expected] of [[context, 62.75], [{ ...context, profileHash }, null]]) {
      const values = [profile], join = api.prod2CommercialAffinityJoin(values, boundContext, tenant);
      const readback = await db.query(`SELECT commercial_jev.affinity_index FROM prod2_discoveries p ${join} WHERE p.id_convocatoria=5001`, values);
      assert.equal(readback.rows[0].affinity_index, expected);
    }
  });
});
