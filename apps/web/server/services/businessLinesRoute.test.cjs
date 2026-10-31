const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, mocks = {}) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, { exports, JSON, Set, String, Array, Response, require(name) {
    if (name in mocks) return mocks[name];
    throw new Error(`Unexpected import ${name}`);
  } });
  return exports;
}
const keywordSignals = load(path.join(__dirname, 'keywordSignals.ts'));
const routeFile = path.join(__dirname, '../../app/api/lines/[id]/route.ts');
const lineId = '00000000-0000-0000-0000-0000000000a1';
const profileId = '00000000-0000-0000-0000-000000000001';

// La ruta real con la base simulada: cada consulta queda registrada y el UPDATE devuelve la fila "guardada".
function lineRoute({ found = true, tenant = 'tenant-1' } = {}) {
  const statements = [];
  const route = load(routeFile, {
    'next/server': { NextResponse: { json: (data, options) => new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' }, ...options }) } },
    '@/server/auth/tenant': { requireTenantId: async () => tenant },
    '@/server/services/keywordSignals': keywordSignals,
    '@/server/db/client': { query: async (sql, values) => {
      statements.push({ sql, values: Array.from(values ?? []) });
      return { rows: sql.includes('UPDATE business_lines') && found ? [{ id: lineId, profile_id: profileId, is_favorite: values[12] ?? false }] : [] };
    } },
  });
  const patch = async body => {
    const response = await route.PATCH({ json: async () => body }, { params: Promise.resolve({ id: lineId }) });
    return { status: response.status, body: await response.json(), statements };
  };
  return { route, statements, patch };
}
const enqueued = statements => statements.filter(statement => statement.sql.includes('worker_jobs'));
const update = statements => statements.find(statement => statement.sql.includes('UPDATE business_lines'));

test('a body that only changes is_favorite saves the flag for the tenant and does not queue a profile match', async () => {
  for (const flag of [true, false]) {
    const { patch, statements } = lineRoute();
    const result = await patch({ is_favorite: flag });
    assert.equal(result.status, 200);
    assert.equal(result.body.data.is_favorite, flag);
    assert.equal(enqueued(statements).length, 0, 'no worker_jobs insert');
    assert.equal(statements.length, 1);
    const { sql, values } = update(statements);
    assert.match(sql, /is_favorite = COALESCE\(\$13, bl\.is_favorite\)/);
    assert.match(sql, /cp\.id = bl\.profile_id\s+AND cp\.tenant_id = \$1\s+AND bl\.id = \$2/);
    assert.deepEqual([values[0], values[1], values[12]], ['tenant-1', lineId, flag]);
    assert.deepEqual(values.slice(2, 12), Array(10).fill(null), 'nothing else is touched');
  }
});

test('every other edit keeps queueing the profile match, with or without is_favorite, and never resets the flag', async () => {
  for (const body of [{}, { nombre: 'Nueva' }, { keywords: ['software'] }, { keyword_phrases: ['licencia de software'] }, { is_active: false },
    { score_umbral: 0 }, { monto_min: 0 }, { cubso_segmentos: [] }, { ubigeos: [] },
    { nombre: 'Nueva', is_favorite: true }, { is_active: false, is_favorite: false }, { keywords: ['nube'], is_favorite: true }]) {
    const { patch, statements } = lineRoute();
    const result = await patch(body);
    assert.equal(result.status, 200, JSON.stringify(body));
    assert.equal(enqueued(statements).length, 1, JSON.stringify(body));
    assert.deepEqual(enqueued(statements)[0].values, [JSON.stringify({ profile_id: profileId }), `match_profile:${profileId}`]);
    // An edit that does not mention the flag binds null, which COALESCE turns into «keep what is stored».
    if (!('is_favorite' in body)) assert.equal(update(statements).values[12], null, JSON.stringify(body));
  }
});

test('null fields mean «no change» as before, so is_favorite with only nulls still counts as a favorite-only change', async () => {
  const { patch, statements } = lineRoute();
  const result = await patch({ is_favorite: true, nombre: null, keywords: null, is_active: null });
  assert.equal(result.status, 200);
  assert.equal(enqueued(statements).length, 0);
});

test('is_favorite must be a boolean: any other type is a 400 before touching the database', async () => {
  for (const value of ['true', 'false', 'yes', 1, 0, null, {}, [], [true], '']) {
    const { patch, statements } = lineRoute();
    const result = await patch({ is_favorite: value });
    assert.equal(result.status, 400, JSON.stringify(value));
    assert.equal(result.body.error, 'is_favorite must be a boolean');
    assert.equal(statements.length, 0, 'no query, no job');
  }
  // The same rule applies when other fields travel with it.
  const { patch, statements } = lineRoute();
  assert.equal((await patch({ nombre: 'Nueva', is_favorite: 'true' })).status, 400);
  assert.equal(statements.length, 0);
});

test('a line of another tenant is a 404 and queues nothing, also for the priority flag', async () => {
  const { patch, statements } = lineRoute({ found: false, tenant: 'otro-tenant' });
  const result = await patch({ is_favorite: true });
  assert.equal(result.status, 404);
  assert.equal(enqueued(statements).length, 0);
  assert.equal(update(statements).values[0], 'otro-tenant');
});

test('reading a line returns the flag with the rest of the row, and deleting still queues the match', async () => {
  const statements = [];
  const route = load(routeFile, {
    'next/server': { NextResponse: { json: (data, options) => new Response(JSON.stringify(data), options) } },
    '@/server/auth/tenant': { requireTenantId: async () => 'tenant-1' },
    '@/server/services/keywordSignals': keywordSignals,
    '@/server/db/client': { query: async (sql, values) => {
      statements.push({ sql, values: Array.from(values ?? []) });
      return { rows: [{ id: lineId, profile_id: profileId, is_favorite: true }] };
    } },
  });
  const context = { params: Promise.resolve({ id: lineId }) };
  const read = await (await route.GET({}, context)).json();
  assert.equal(read.data.is_favorite, true);
  assert.match(statements[0].sql, /SELECT bl\.\*/);
  await route.DELETE({}, context);
  assert.equal(enqueued(statements).length, 1);
});
