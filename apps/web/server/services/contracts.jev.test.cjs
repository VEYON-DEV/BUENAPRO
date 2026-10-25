const test = require('node:test');
const assert = require('node:assert/strict');
const { loadCommercialModule: load } = require('./commercialClassification.test-support.cjs');
const profileId = '00000000-0000-0000-0000-000000000001';
const plain = (value) => JSON.parse(JSON.stringify(value));

function service({ listRows = [], jevRows = [], profileRows = [{ id: profileId, razon_social: 'Empresa QA' }] } = {}) {
  const statements = [];
  const query = async (text, values) => {
    statements.push({ text, values });
    if (text.includes('SELECT id::text, razon_social FROM company_profiles')) return { rows: profileRows };
    if (text.includes('AS is_current_profile')) return { rows: jevRows };
    if (text.includes('FROM seace_contracts c')) return { rows: listRows };
    return { rows: [] };
  };
  const value = load('contracts.ts', {
    '@/server/db/client': { query },
    '@/server/services/crud': { pagination: () => ({ limit: 20, offset: 0, page: 1, pageSize: 20 }) },
    '@/server/services/keywordScoring': { keywordFitLateralSql: () => '' },
  });
  return { value, statements };
}

test('list adds the JEV fields of the selected profile without changing filters or order', async () => {
  const jev = { jev_score: 78.25, jev_decision: 'accepted', jev_subcategory: 'soporte_ti', jev_threshold: 60, jev_decided_at: '2026-10-25T00:00:00Z' };
  const { value, statements } = service({ listRows: [{ id_contrato: 1, total_count: 1, ...jev }, { id_contrato: 2, total_count: 1 }] });
  const result = await value.listContractsForTenant('tenant-qa', new URLSearchParams());
  assert.equal(result.data[0].jev_score, 78.25);
  assert.equal(result.data[0].jev_subcategory, 'soporte_ti');
  assert.equal(result.data[1].jev_score, undefined, 'a contract without a decision carries no JEV fields');
  const { text, values } = statements.find(({ text }) => text.includes('FROM seace_contracts c'));
  assert.match(text, /jev\.jev_score,\s+jev\.jev_decision,\s+jev\.jev_subcategory,\s+jev\.jev_threshold,\s+jev\.jev_decided_at/);
  assert.match(text, /LEFT JOIN LATERAL \(\s+SELECT d\.score::float8 AS jev_score/);
  assert.match(text, /FROM prod6_profile_decisions d\s+WHERE d\.id_contrato = c\.id_contrato AND d\.profile_id = \$2::uuid/);
  assert.equal(values[1], profileId);
  const order = text.slice(text.indexOf('ORDER BY\n      CASE WHEN m.id IS NOT NULL'), text.indexOf('LIMIT $'));
  assert.doesNotMatch(order, /jev/, 'JEV does not reorder the list in this MVP');
  assert.doesNotMatch(text, /jev\.[a-z_]+\s+(IS|=|>|<)/, 'JEV does not filter the list');
  assert.doesNotMatch(text, /INSERT|UPDATE|DELETE/);
});

test('latest decision per contract and profile prefers JEV rows, then updated_at and id', async () => {
  const { value, statements } = service({ listRows: [{ id_contrato: 123, extraction_id: null, total_count: 1 }] });
  await value.listContractsForTenant('tenant-qa', new URLSearchParams());
  await value.getContractForTenant('tenant-qa', 123);
  const sqls = statements.map(({ text }) => text).filter((text) => text.includes('FROM prod6_profile_decisions'));
  assert.equal(sqls.length, 2);
  for (const text of sqls) {
    assert.match(text, /ORDER BY \(d\.decided_by = 'jev'\) DESC, d\.updated_at DESC, d\.id DESC\s+LIMIT 1/);
    assert.doesNotMatch(text, /result_json|listing_json/, 'internal payloads are never exposed');
  }
});

test('without a selected profile the list requests no decision and still returns rows', async () => {
  const { value, statements } = service({ listRows: [{ id_contrato: 1, total_count: 1 }], profileRows: [] });
  const result = await value.listContractsForTenant('tenant-qa', new URLSearchParams());
  assert.equal(result.data.length, 1);
  assert.equal(statements.find(({ text }) => text.includes('FROM seace_contracts c')).values[1], null);
});

test('detail returns one row per active tenant profile, current profile first, and tolerates an empty table', async () => {
  const rows = [{ profile_id: profileId, profile_name: 'Empresa QA', is_current_profile: true, score: 80, decision: 'accepted' }];
  const withRows = service({ listRows: [{ id_contrato: 123, extraction_id: null }], jevRows: rows });
  const detail = await withRows.value.getContractForTenant('tenant-qa', 123);
  assert.deepEqual(plain(detail.jev_by_company), rows);
  const { text, values } = withRows.statements.find(({ text }) => text.includes('is_current_profile'));
  assert.match(text, /WHERE cp\.tenant_id = \$2 AND cp\.is_active = true/);
  assert.match(text, /ORDER BY is_current_profile DESC, d\.score DESC NULLS LAST/);
  assert.deepEqual(plain(values), [123, 'tenant-qa', profileId]);

  const empty = service({ listRows: [{ id_contrato: 123, extraction_id: null }] });
  assert.deepEqual(plain((await empty.value.getContractForTenant('tenant-qa', 123)).jev_by_company), []);
  const missing = service({ listRows: [] });
  assert.equal(await missing.value.getContractForTenant('tenant-qa', 123), null);
  assert.equal(missing.statements.some(({ text }) => text.includes('prod6_profile_decisions')), false, 'no extra query for a contract that does not exist');
});
