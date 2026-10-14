const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, mocks) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, { exports, Response, require(name) {
    if (name in mocks) return mocks[name];
    throw new Error(`Unexpected import ${name}`);
  } });
  return exports;
}
const serviceFile = `${__dirname}/savedOpportunities.ts`;
const routeFile = `${__dirname}/../../app/api/prod2/opportunities/[id]/saved/route.ts`;
const missingTable = () => Object.assign(new Error('relation "saved_opportunities" does not exist'), { code: '42P01' });

test('the missing-table fallback applies only to saved_opportunities', () => {
  const service = load(serviceFile, { '@/server/db/client': {} });
  for (const error of [missingTable(),
    { code: '42P01', message: 'relation "public.saved_opportunities" does not exist' },
    { code: '42P01', table: 'saved_opportunities' }]) {
    assert.equal(service.isMissingSavedOpportunitiesTable(error), true);
  }
  for (const error of [null, {}, { code: '42P01' },
    { code: '42P01', message: 'relation "prod2_details" does not exist' },
    { code: '42P01', message: 'relation "not_saved_opportunities" does not exist' },
    { code: '42P01', table: 'opportunities' },
    { code: '42501', message: 'relation "saved_opportunities" does not exist' }]) {
    assert.equal(service.isMissingSavedOpportunitiesTable(error), false);
  }
});

test('save and unsave bind the workspace, convocatoria and session actor without client-selected ownership', async () => {
  const calls = [];
  const created = new Date('2026-10-14T10:00:00Z');
  const service = load(serviceFile, { '@/server/db/client': { query: async (sql, values) => {
    calls.push({ sql, values: Array.from(values) });
    return { rows: sql.includes('INSERT INTO') ? [{ opportunity_id: 'canonical-id', created_at: created }] : [] };
  } } });
  const first = await service.saveProd2OpportunityForTenant('tenant-a', '123', 'actor-a');
  const second = await service.saveProd2OpportunityForTenant('tenant-a', '123', 'actor-b');
  assert.equal(first.status, 200);
  assert.equal(first.data.is_saved, true);
  assert.equal(first.data.opportunity_id, 'canonical-id');
  assert.equal(first.data.saved_at, created);
  assert.equal(second.data.saved_at, first.data.saved_at, 'repeated saves retain their original bookmark timestamp');
  assert.deepEqual(calls[0].values, ['tenant-a', '123', 'actor-a']);
  assert.match(calls[0].sql, /ON CONFLICT \(tenant_id, opportunity_id\)/);
  assert.match(calls[0].sql, /saved_by = COALESCE\(saved_opportunities.saved_by, EXCLUDED.saved_by\)/);
  assert.doesNotMatch(calls[0].sql, /SET created_at/);
  const removed = await service.unsaveProd2OpportunityForTenant('tenant-b', '123');
  assert.equal(removed.data.is_saved, false);
  assert.deepEqual(calls[2].values, ['tenant-b', '123']);
  assert.match(calls[2].sql, /s.tenant_id = \$1/);
  assert.match(calls[2].sql, /s.opportunity_id = p.opportunity_id/);
});

test('a convocatoria absent from discovery returns 404, while repeated removal succeeds', async () => {
  const service = load(serviceFile, { '@/server/db/client': { query: async () => ({ rows: [] }) } });
  const missing = await service.saveProd2OpportunityForTenant('tenant', '987');
  assert.equal(missing.status, 404);
  assert.equal(missing.error, 'Opportunity not found');
  const first = await service.unsaveProd2OpportunityForTenant('tenant', '987');
  const second = await service.unsaveProd2OpportunityForTenant('tenant', '987');
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal(second.data.is_saved, false);
});

test('saved lookup is scoped to tenant and requested canonical IDs and skips empty pages', async () => {
  const calls = [];
  const service = load(serviceFile, { '@/server/db/client': { query: async (sql, values) => {
    calls.push({ sql, values }); return { rows: [{ opportunity_id: 'opp-b' }] };
  } } });
  assert.equal((await service.savedOpportunityIds('tenant', [])).size, 0);
  assert.equal(calls.length, 0);
  const result = await service.savedOpportunityIds('tenant', ['opp-a', 'opp-b']);
  assert.deepEqual(Array.from(result), ['opp-b']);
  assert.equal(calls[0].values[0], 'tenant');
  assert.deepEqual(Array.from(calls[0].values[1]), ['opp-a', 'opp-b']);
  assert.match(calls[0].sql, /tenant_id = \$1 AND opportunity_id = ANY\(\$2::uuid\[\]\)/);
});

test('lookup degrades only for missing bookmarks and surfaces unrelated DB failures', async () => {
  let error = missingTable();
  const service = load(serviceFile, { '@/server/db/client': { query: async () => { throw error; } } });
  assert.equal((await service.savedOpportunityIds('tenant', ['opp'])).size, 0);
  for (const next of [Object.assign(new Error('relation "other" does not exist'), { code: '42P01' }),
    Object.assign(new Error('permission denied'), { code: '42501' })]) {
    error = next;
    await assert.rejects(service.savedOpportunityIds('tenant', ['opp']), actual => actual === next);
  }
});

function routeHarness({ authError, dbError, found = true } = {}) {
  const calls = [];
  const service = load(serviceFile, { '@/server/db/client': { query: async (sql, values) => {
    calls.push({ sql, values: Array.from(values) });
    if (dbError) throw dbError;
    return { rows: sql.includes('INSERT INTO') && found ? [{ opportunity_id: 'canonical', created_at: new Date('2026-10-14T10:00:00Z') }] : [] };
  } } });
  const route = load(routeFile, {
    'next/server': { NextResponse: { json: (body, options) => new Response(JSON.stringify(body), options) } },
    '@/server/auth/tenant': {
      requireTenantId: async () => { if (authError) throw authError; return 'session-tenant'; },
      currentUserId: async () => 'session-actor',
    },
    '@/server/services/savedOpportunities': service,
  });
  return { route, calls };
}
const context = id => ({ params: Promise.resolve({ id }) });
const request = { json: async () => ({ tenant_id: 'foreign', saved_by: 'foreign-actor' }) };

test('PUT/DELETE preserve unauthenticated 401 without reaching the database', async () => {
  for (const method of ['PUT', 'DELETE']) {
    const { route, calls } = routeHarness({ authError: new Response('Missing tenant context', { status: 401 }) });
    const result = await route[method](request, context('123'));
    assert.equal(result.status, 401);
    assert.equal(calls.length, 0);
  }
});

test('PUT/DELETE reject malformed and overflowing BIGINT identities before database access', async () => {
  for (const method of ['PUT', 'DELETE']) {
    const { route, calls } = routeHarness();
    for (const id of ['0', '-1', '01', '1.5', '1e6', '123 OR 1=1', '9223372036854775808', '9999999999999999999', '1'.repeat(100)]) {
      assert.equal((await route[method](request, context(id))).status, 400, `${method} ${id}`);
    }
    assert.equal(calls.length, 0);
    assert.equal((await route[method](request, context('9223372036854775807'))).status, 200);
    assert.equal(calls[0].values[1], '9223372036854775807', 'BIGINT max remains an exact decimal string');
  }
});

test('PUT/DELETE use session ownership and return their stored bookmark state', async () => {
  const { route, calls } = routeHarness();
  const saved = await route.PUT(request, context('123'));
  assert.equal(saved.status, 200);
  assert.equal((await saved.json()).data.is_saved, true);
  assert.deepEqual(calls[0].values, ['session-tenant', '123', 'session-actor']);
  const removed = await route.DELETE(request, context('123'));
  assert.equal(removed.status, 200);
  assert.equal((await removed.json()).data.is_saved, false);
  assert.deepEqual(calls[1].values, ['session-tenant', '123']);
});

test('PUT returns 404 for an unknown convocatoria; DELETE remains idempotent', async () => {
  const { route } = routeHarness({ found: false });
  assert.equal((await route.PUT(request, context('123'))).status, 404);
  assert.equal((await route.DELETE(request, context('123'))).status, 200);
  assert.equal((await route.DELETE(request, context('123'))).status, 200);
});

test('PUT/DELETE return safe 503 only for missing saved_opportunities', async () => {
  for (const method of ['PUT', 'DELETE']) {
    const { route } = routeHarness({ dbError: missingTable() });
    const response = await route[method](request, context('123'));
    assert.equal(response.status, 503);
    assert.equal((await response.json()).error, 'Guardar del Buscador SEACE aún no está habilitado.');
    const unrelated = Object.assign(new Error('relation "prod2_discoveries" does not exist'), { code: '42P01' });
    const broken = routeHarness({ dbError: unrelated });
    await assert.rejects(broken.route[method](request, context('123')), error => error === unrelated);
  }
});
