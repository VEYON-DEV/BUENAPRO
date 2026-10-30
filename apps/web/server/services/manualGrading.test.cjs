const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, mocks = {}, globals = {}) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(path.resolve(__dirname, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports, Object, Set, Map, Date, JSON, Number, Promise, Response, Error, SyntaxError, console,
    require(name) {
      if (Object.hasOwn(mocks, name)) return mocks[name];
      throw new Error(`Unexpected module: ${name}`);
    },
    ...globals,
  });
  return exports;
}

const TENANT = '11111111-1111-4111-8111-111111111111';
const OTHER_TENANT = '22222222-2222-4222-8222-222222222222';
const PROFILE = '33333333-3333-4333-8333-333333333333';
const READ_OK = { extraction_id: 10, quality: 'ok', requires_human_review: false };
const ACTIVE = ['pending', 'reading', 'evaluating'];
const tick = () => new Promise(resolve => setImmediate(resolve));

/** El módulo real de mayores (sus fragmentos SQL y resolveProfile) con el resto de sus dependencias reemplazadas. */
function loadProd2(dbClient) {
  return load('prod2Opportunities.ts', {
    '@/server/db/client': dbClient,
    '@/lib/prod2Analysis': {},
    './prod2DocumentStorage': {},
    './prod2OfficialDocument': {},
    './savedOpportunities': {},
    './prod2CommercialAffinity': {},
    './keywordTextScoring': { keywordTextFitLateralSql: () => 'LEFT JOIN LATERAL (SELECT 1) fit ON true' },
  }, { process: { env: {} } });
}

/**
 * Base de datos en memoria que reconoce las consultas del servicio por su texto.
 * El lock consultivo es real: la segunda transacción del mismo tenant espera el COMMIT/ROLLBACK de la primera.
 */
function makeDb({ contracts = {}, convocatorias = {}, profile = PROFILE, rows = [], analyze, env = {}, failMarks = false } = {}) {
  const processEnv = { ...env };
  const db = { rows: [...rows], events: [], queries: [], nextId: 100, locks: new Map(), analyzeCalls: [], processEnv };
  const todayRows = tenant => db.rows.filter(row => row.tenant_id === tenant && row.lima_day === 'today');
  const age = row => (row.stale ? 10 * 60 : 1);

  function lock(tenant) {
    const previous = db.locks.get(tenant) ?? Promise.resolve();
    let release;
    const held = new Promise(resolve => { release = resolve; });
    db.locks.set(tenant, previous.then(() => held));
    return previous.then(() => release);
  }

  async function run(sql, values, session) {
    values = Array.from(values);
    db.queries.push({ sql, values, tx: Boolean(session) });
    if (/FROM company_profiles/.test(sql)) {
      return { rows: profile ? [{ id: values[1] ?? profile }] : [] };
    }
    if (/FROM prod2_discoveries/.test(sql)) {
      db.events.push('FACTS');
      const found = convocatorias[values[1]];
      return { rows: found ? [{ jev_score: found.jev ?? null, closed: found.closed ?? false, graded: found.graded ?? false }] : [] };
    }
    if (/FROM seace_contracts/.test(sql)) {
      const contract = contracts[values[0]];
      return { rows: contract ? [{ id_contrato: values[0], ...contract }] : [] };
    }
    if (/pg_advisory_xact_lock/.test(sql)) {
      db.events.push('LOCK');
      session.release = await lock(values[1]);
      return { rows: [] };
    }
    if (/reason='evaluation_interrupted'/.test(sql) && /^\s*UPDATE manual_grading_requests/.test(sql)) {
      db.events.push('EXPIRE');
      for (const row of db.rows) {
        if (row.tenant_id === values[0] && row.source_id === values[2] && row.mode === 'direct' && ACTIVE.includes(row.status) && age(row) > values[5]) {
          Object.assign(row, { status: 'failed', reason: 'evaluation_interrupted' });
        }
      }
      return { rows: [] };
    }
    if (/count\(\*\)/.test(sql)) {
      db.events.push('COUNT');
      const used = todayRows(values[0]).length;
      await tick(); // the answer is stale by the time it arrives unless a lock serializes the transactions
      return { rows: [{ used }] };
    }
    if (/^\s*SELECT id, status, mode, reason, created_at, updated_at FROM manual_grading_requests/.test(sql)) {
      db.events.push('FIND_ACTIVE');
      const found = db.rows.filter(row => row.tenant_id === values[0] && row.source_key === values[1] && row.source_id === values[2]
        && row.profile_id === values[3] && values[4].includes(row.status));
      return { rows: found.slice(-1) };
    }
    if (/^\s*INSERT INTO manual_grading_requests/.test(sql)) {
      db.events.push('INSERT');
      const [tenant_id, profile_id, source_key, source_id, requested_by, mode, status] = values;
      const row = { id: String(db.nextId++), tenant_id, profile_id, source_key, source_id, requested_by, mode, status, reason: null,
        lima_day: 'today', created_at: new Date(), updated_at: new Date() };
      db.rows.push(row);
      return { rows: [{ ...row }] };
    }
    if (/^\s*UPDATE manual_grading_requests SET status=\$2/.test(sql)) {
      if (failMarks) throw new Error('connection reset');
      db.events.push(`MARK_${values[1].toUpperCase()}`);
      Object.assign(db.rows.find(row => row.id === values[0]), { status: values[1], reason: values[2] });
      return { rows: [] };
    }
    if (/^\s*DELETE FROM manual_grading_requests/.test(sql)) {
      db.events.push('DELETE');
      db.rows = db.rows.filter(row => row.id !== values[0]);
      return { rows: [] };
    }
    if (/AS stale/.test(sql)) {
      const found = db.rows.filter(row => row.tenant_id === values[0] && row.source_key === values[1] && row.source_id === values[2] && row.profile_id === values[5]);
      const latest = found.at(-1);
      return { rows: latest ? [{ ...latest, stale: latest.mode === 'direct' && ACTIVE.includes(latest.status) && age(latest) > values[4] }] : [] };
    }
    throw new Error(`Unexpected SQL: ${sql}`);
  }

  const client = () => {
    const session = { release: null };
    return {
      async query(sql, values = []) {
        if (/^\s*BEGIN/.test(sql)) { db.events.push('BEGIN'); return { rows: [] }; }
        if (/^\s*(COMMIT|ROLLBACK)/.test(sql)) {
          db.events.push(sql.trim());
          if (session.release) { session.release(); session.release = null; }
          return { rows: [] };
        }
        return run(sql, values, session);
      },
      release() { db.events.push('RELEASE'); },
    };
  };

  const analyzeMatchDirect = async input => {
    db.analyzeCalls.push(input);
    db.events.push('ANALYZE');
    return analyze ? analyze(input) : { match_id: 7, verdict: 'ambar', score: 62 };
  };
  const dbClient = { pool: { connect: async () => client() }, query: (sql, values = []) => run(sql, values, null) };
  const prod2 = loadProd2(dbClient);
  const service = load('manualGrading.ts', {
    '@/server/db/client': dbClient,
    './matchAnalysis': { analyzeMatchDirect },
    './prod2Opportunities': prod2,
  }, { process: { env: processEnv } });
  return Object.assign(db, { service, prod2 });
}

const request = (db, extra = {}) => db.service.requestMinorGrading({ tenantId: TENANT, idContrato: 501, actorId: 'user-1', force: false, ...extra });
const withReading = { 501: READ_OK, 502: READ_OK, 503: READ_OK };
const usedBy = (db, tenant = TENANT) => db.rows.filter(row => row.tenant_id === tenant && row.lima_day === 'today').length;
const seed = (count, extra = {}) => Array.from({ length: count }, (_, index) => ({
  id: `seed-${index}`, tenant_id: TENANT, profile_id: PROFILE, source_key: 'seace_prod6', source_id: 9000 + index, requested_by: 'u',
  mode: 'queued', status: 'done', reason: null, lima_day: 'today', created_at: new Date(), updated_at: new Date(), ...extra,
}));

test('the daily limit comes from MANUAL_GRADING_DAILY_LIMIT and falls back to 100', () => {
  assert.equal(makeDb().service.manualGradingDailyLimit(), 100);
  assert.equal(makeDb({ env: { MANUAL_GRADING_DAILY_LIMIT: '25' } }).service.manualGradingDailyLimit(), 25);
  for (const invalid of ['', '0', '-3', 'abc', '2.5']) {
    assert.equal(makeDb({ env: { MANUAL_GRADING_DAILY_LIMIT: invalid } }).service.manualGradingDailyLimit(), 100, invalid);
  }
});

test('a valid reading is graded directly: request evaluating, analysis with force, then done', async () => {
  const db = makeDb({ contracts: withReading });
  const result = await request(db, { force: true });
  assert.equal(result.status, 'done');
  assert.deepEqual(JSON.parse(JSON.stringify(result.data)), { match_id: 7, verdict: 'ambar', score: 62 });
  assert.equal(db.analyzeCalls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(db.analyzeCalls[0])), { tenantId: TENANT, idContrato: 501, profileId: PROFILE, actorId: 'user-1', force: true });
  const [row] = db.rows;
  assert.equal(row.mode, 'direct');
  assert.equal(row.status, 'done');
  assert.equal(row.source_key, 'seace_prod6');
  assert.equal(row.source_id, 501);
  assert.equal(row.profile_id, PROFILE);
  assert.equal(row.requested_by, 'user-1');
  const insertIndex = db.queries.findIndex(call => /INSERT INTO manual_grading_requests/.test(call.sql));
  assert.equal(db.queries[insertIndex].values[6], 'evaluating', 'the row is evaluating before the analysis starts');
  assert.ok(db.events.indexOf('INSERT') < db.events.indexOf('ANALYZE') && db.events.indexOf('ANALYZE') < db.events.indexOf('MARK_DONE'));
});

test('force is passed through unchanged, including false', async () => {
  const db = makeDb({ contracts: withReading });
  await request(db, { force: false });
  await request(db, { force: undefined, idContrato: 502 });
  assert.deepEqual(db.analyzeCalls.map(call => call.force), [false, false]);
});

test('without a valid reading the request is queued as pending and nothing is analyzed', async () => {
  const unreadable = {
    601: { extraction_id: null, quality: null, requires_human_review: null },
    602: { extraction_id: 4, quality: 'failed', requires_human_review: false },
    603: { extraction_id: 5, quality: 'ok', requires_human_review: true },
  };
  for (const idContrato of [601, 602, 603]) {
    const db = makeDb({ contracts: unreadable });
    const result = await request(db, { idContrato, force: true });
    assert.equal(result.status, 'queued', String(idContrato));
    assert.equal(result.request.status, 'pending');
    assert.equal(result.request.mode, 'queued');
    assert.equal(db.analyzeCalls.length, 0);
    assert.equal(db.rows.length, 1);
    assert.deepEqual([db.rows[0].mode, db.rows[0].status, db.rows[0].source_id], ['queued', 'pending', idContrato]);
  }
});

test('an active request is returned without a new row, a count or an analysis, even at the limit', async () => {
  for (const status of ACTIVE) {
    const active = seed(1, { id: 'active-1', source_id: 501, status, mode: 'queued' });
    const db = makeDb({ contracts: withReading, rows: [...active, ...seed(2)], env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
    const result = await request(db);
    assert.equal(result.status, 'queued', status);
    assert.equal(result.request.id, 'active-1');
    assert.equal(result.request.status, status);
    assert.equal(db.rows.length, 3, 'no new row');
    assert.ok(!db.events.includes('INSERT') && !db.events.includes('COUNT') && !db.events.includes('ANALYZE'));
    assert.equal(db.events.at(-2), 'COMMIT');
  }
});

test('only an active request of the same contract and profile deduplicates', async () => {
  const otherContract = seed(1, { id: 'a', source_id: 777, status: 'pending' });
  const otherProfile = seed(1, { id: 'b', source_id: 501, status: 'pending', profile_id: '44444444-4444-4444-8444-444444444444' });
  const finished = seed(1, { id: 'c', source_id: 501, status: 'done' });
  const db = makeDb({ contracts: withReading, rows: [...otherContract, ...otherProfile, ...finished] });
  const result = await request(db);
  assert.equal(result.status, 'done');
  assert.equal(db.rows.length, 4);
});

test('the limit is enforced with a typed error and the real number, without inserting or analyzing', async () => {
  const db = makeDb({ contracts: withReading, rows: seed(3), env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
  await assert.rejects(request(db), error => {
    assert.equal(error.code, 'daily_limit_reached');
    assert.equal(error.name, 'ManualGradingError');
    assert.equal(error.message, 'Se alcanzó el límite diario de 3 calificaciones manuales de tu empresa.');
    assert.equal(error.limit, 3);
    assert.equal(error.used, 3);
    return true;
  });
  assert.equal(db.rows.length, 3);
  assert.equal(db.analyzeCalls.length, 0);
  assert.ok(db.events.includes('ROLLBACK') && !db.events.includes('COMMIT') && !db.events.includes('INSERT'));
  assert.equal(db.events.at(-1), 'RELEASE');
});

test('the default limit of 100 applies and the message names it', async () => {
  const db = makeDb({ contracts: withReading, rows: seed(100) });
  await assert.rejects(request(db), { code: 'daily_limit_reached', message: 'Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.' });
  const below = makeDb({ contracts: withReading, rows: seed(99) });
  assert.equal((await request(below)).status, 'done');
  assert.equal(usedBy(below), 100);
});

test('usage counts both sources and every state, but only the tenant and the Lima day of today', async () => {
  const rows = [
    ...seed(1, { source_key: 'seace_prod2', source_id: 1 }),
    ...seed(1, { status: 'failed', reason: 'reading_failed' }),
    ...seed(1, { status: 'pending', mode: 'queued', source_id: 5 }),
    ...seed(1, { tenant_id: OTHER_TENANT }),
    ...seed(1, { lima_day: 'yesterday' }),
  ];
  const db = makeDb({ contracts: withReading, rows, env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
  await assert.rejects(request(db), { code: 'daily_limit_reached' });
  const countCall = db.queries.find(call => /count\(\*\)/.test(call.sql));
  assert.match(countCall.sql, /lima_day=\(now\(\) AT TIME ZONE 'America\/Lima'\)::date/);
  assert.doesNotMatch(countCall.sql, /source_key/, 'the count is not restricted to one source');
  assert.deepEqual(countCall.values, [TENANT]);
});

test('re-grading with force counts like any other grading', async () => {
  const db = makeDb({ contracts: withReading, env: { MANUAL_GRADING_DAILY_LIMIT: '2' } });
  assert.equal((await request(db, { force: false })).status, 'done');
  assert.equal(usedBy(db), 1);
  assert.equal((await request(db, { force: true })).status, 'done');
  assert.equal(usedBy(db), 2, 'the forced re-grade consumed a slot');
  await assert.rejects(request(db, { force: true }), { code: 'daily_limit_reached' });
  assert.equal(db.analyzeCalls.length, 2, 'the third attempt never reached the model');
  assert.deepEqual(db.analyzeCalls.map(call => call.force), [false, true]);
});

test('a failed analysis marks the row failed with a reason, still counts and propagates the error', async () => {
  const cases = [
    ['La licitación necesita una lectura documental válida antes de analizar su afinidad.', 'reading_requires_review'],
    ['No se encontró un perfil activo para esta empresa.', 'profile_inactive'],
    ['No se encontró la licitación.', 'contract_not_found'],
    ['Gemini devolvió una respuesta vacía.', 'invalid_model_output'],
    ['El análisis de Gemini fue truncado.', 'invalid_model_output'],
    ['La lectura no contiene requisitos sustantivos para evaluar capacidad.', 'no_requirements'],
    ['Gemini respondió HTTP 503.', 'evaluation_failed'],
  ];
  for (const [message, reason] of cases) {
    const failure = new Error(message);
    const db = makeDb({ contracts: withReading, analyze: async () => { throw failure; } });
    await assert.rejects(request(db), error => error === failure, message);
    assert.deepEqual([db.rows[0].status, db.rows[0].reason], ['failed', reason], message);
    assert.equal(usedBy(db), 1, 'a failed attempt is not refunded');
  }
  const syntax = makeDb({ contracts: withReading, analyze: async () => { throw new SyntaxError('Unexpected token'); } });
  await assert.rejects(request(syntax), SyntaxError);
  assert.equal(syntax.rows[0].reason, 'invalid_model_output');
});

test('an error while closing the request neither hides the original failure nor discards a finished grading', async () => {
  const original = console.error;
  const logged = [];
  console.error = (...args) => logged.push(args);
  try {
    const finished = makeDb({ contracts: withReading, failMarks: true });
    assert.equal((await request(finished)).status, 'done');
    const failure = new Error('Gemini respondió HTTP 500.');
    const failed = makeDb({ contracts: withReading, failMarks: true, analyze: async () => { throw failure; } });
    await assert.rejects(request(failed), error => error === failure);
    assert.equal(logged.length, 2);
  } finally {
    console.error = original;
  }
});

test('an unchanged analysis costs nothing: the row is removed and the grading is still reported done', async () => {
  const db = makeDb({ contracts: withReading, analyze: async () => ({ skipped: 'unchanged', match_id: 9 }) });
  const result = await request(db, { force: false });
  assert.equal(result.status, 'done');
  assert.equal(result.data.skipped, 'unchanged');
  assert.equal(db.rows.length, 0);
  assert.ok(db.events.includes('DELETE') && !db.events.includes('MARK_DONE'));
});

test('missing profile or contract fails before opening a transaction or touching the counter', async () => {
  const noProfile = makeDb({ contracts: withReading, profile: null });
  await assert.rejects(request(noProfile), { code: 'profile_not_found', message: 'No se encontró un perfil activo para esta empresa.' });
  assert.deepEqual(noProfile.events, []);

  const noContract = makeDb({ contracts: {} });
  await assert.rejects(request(noContract), { code: 'contract_not_found' });
  assert.deepEqual(noContract.events, []);
  assert.equal(noContract.rows.length, 0);
});

test('the profile is resolved like analyzeMatchDirect: active, tenant-scoped, optional explicit id', async () => {
  const db = makeDb({ contracts: withReading });
  await request(db, { profileId: PROFILE });
  const profileCall = db.queries.find(call => /FROM company_profiles/.test(call.sql));
  assert.match(profileCall.sql, /tenant_id=\$1 AND is_active=true AND \(\$2::uuid IS NULL OR id=\$2::uuid\)\s+ORDER BY updated_at DESC LIMIT 1/);
  assert.deepEqual(profileCall.values, [TENANT, PROFILE]);
  const implicit = makeDb({ contracts: withReading });
  await request(implicit);
  assert.deepEqual(implicit.queries.find(call => /FROM company_profiles/.test(call.sql)).values, [TENANT, null]);
});

test('the lock is taken before the active lookup and the count, inside one transaction', async () => {
  const db = makeDb({ contracts: { 501: { extraction_id: null } } });
  await request(db);
  const lockCall = db.queries.find(call => /pg_advisory_xact_lock/.test(call.sql));
  assert.deepEqual(lockCall.values, ['manual_grading_requests', TENANT]);
  assert.deepEqual(db.events, ['BEGIN', 'LOCK', 'EXPIRE', 'FIND_ACTIVE', 'COUNT', 'INSERT', 'COMMIT', 'RELEASE']);
  assert.ok(db.queries.filter(call => call.tx).every(call => !/seace_contracts|company_profiles/.test(call.sql)));
});

test('simultaneous requests of one tenant cannot exceed the limit', async () => {
  const unread = { 701: {}, 702: {}, 703: {}, 704: {}, 705: {} };
  const db = makeDb({ contracts: unread, rows: seed(2), env: { MANUAL_GRADING_DAILY_LIMIT: '4' } });
  const outcomes = await Promise.allSettled([701, 702, 703, 704, 705].map(idContrato => request(db, { idContrato })));
  assert.equal(outcomes.filter(outcome => outcome.status === 'fulfilled').length, 2);
  const rejected = outcomes.filter(outcome => outcome.status === 'rejected');
  assert.equal(rejected.length, 3);
  assert.ok(rejected.every(outcome => outcome.reason.code === 'daily_limit_reached'));
  assert.equal(usedBy(db), 4);
});

test('simultaneous requests for the same contract create a single request', async () => {
  const db = makeDb({ contracts: { 501: {} } });
  const outcomes = await Promise.all([request(db), request(db), request(db)]);
  assert.equal(db.rows.length, 1);
  assert.equal(new Set(outcomes.map(outcome => outcome.request.id)).size, 1);
  assert.ok(outcomes.every(outcome => outcome.status === 'queued'));
});

test('limits are per tenant: another tenant at its limit does not block this one', async () => {
  const db = makeDb({ contracts: { 501: {} }, rows: seed(2, { tenant_id: OTHER_TENANT }), env: { MANUAL_GRADING_DAILY_LIMIT: '2' } });
  assert.equal((await request(db)).status, 'queued');
});

test('a direct request left active by a crashed process expires and the contract can be graded again', async () => {
  const stale = seed(1, { id: 'stuck', source_id: 501, mode: 'direct', status: 'evaluating', stale: true });
  const db = makeDb({ contracts: withReading, rows: stale });
  const result = await request(db);
  assert.equal(result.status, 'done');
  assert.deepEqual([db.rows[0].status, db.rows[0].reason], ['failed', 'evaluation_interrupted']);
  assert.equal(db.rows.length, 2);
  assert.equal(db.rows[1].status, 'done');
});

test('status returns the latest request and today usage', async () => {
  const older = seed(1, { id: 'old', source_id: 501, status: 'failed', reason: 'reading_failed' });
  const newest = seed(1, { id: 'new', source_id: 501, status: 'reading', mode: 'queued' });
  const db = makeDb({ contracts: withReading, rows: [...older, ...newest, ...seed(1, { source_id: 999 })] });
  const status = await db.service.minorGradingStatus({ tenantId: TENANT, idContrato: 501, profileId: PROFILE });
  assert.deepEqual([status.request.status, status.request.mode, status.request.reason], ['reading', 'queued', null]);
  assert.ok(status.request.created_at && status.request.updated_at);
  assert.deepEqual(JSON.parse(JSON.stringify(status.usage)), { used: 3, limit: 100 });
  assert.equal(Object.hasOwn(status.request, 'stale'), false);
});

test('status without requests is null, reports usage and never writes', async () => {
  const db = makeDb({ contracts: withReading, rows: seed(5), env: { MANUAL_GRADING_DAILY_LIMIT: '10' } });
  const status = await db.service.minorGradingStatus({ tenantId: TENANT, idContrato: 501 });
  assert.equal(status.request, null);
  assert.deepEqual(JSON.parse(JSON.stringify(status.usage)), { used: 5, limit: 10 });
  assert.deepEqual(db.events, ['COUNT']);
});

test('status without an active profile still reports usage', async () => {
  const db = makeDb({ contracts: withReading, rows: seed(2), profile: null });
  const status = await db.service.minorGradingStatus({ tenantId: TENANT, idContrato: 501 });
  assert.equal(status.request, null);
  assert.equal(status.usage.used, 2);
});

test('status reports an orphaned direct request as failed instead of in progress forever', async () => {
  const stuck = seed(1, { source_id: 501, mode: 'direct', status: 'evaluating', stale: true });
  const db = makeDb({ contracts: withReading, rows: stuck });
  const status = await db.service.minorGradingStatus({ tenantId: TENANT, idContrato: 501, profileId: PROFILE });
  assert.deepEqual([status.request.status, status.request.reason], ['failed', 'evaluation_interrupted']);
});

// ---- Mayores (PROD2) -----------------------------------------------------------------------------------------

const OPEN = { 123: { jev: 72 }, 124: { jev: 60 }, 125: { jev: 99 }, 126: { jev: 80 } };
const requestMajor = (db, extra = {}) => db.service.requestMajorGrading({ tenantId: TENANT, idConvocatoria: '123', actorId: 'user-1', ...extra });
const assertUntouched = (db) => {
  assert.ok(!db.events.some(event => ['BEGIN', 'LOCK', 'COUNT', 'INSERT', 'COMMIT', 'ROLLBACK'].includes(event)), `no transaction nor quota: ${db.events}`);
};

test('a mayores request is always queued and pending, for the Lima day, and nothing is ever analyzed by the web', async () => {
  const db = makeDb({ convocatorias: OPEN });
  const result = await requestMajor(db);
  assert.equal(result.status, 'queued');
  assert.deepEqual([result.request.status, result.request.mode, result.request.reason], ['pending', 'queued', null]);
  assert.equal(db.analyzeCalls.length, 0);
  assert.equal(db.rows.length, 1);
  const [row] = db.rows;
  assert.deepEqual([row.source_key, row.source_id, row.mode, row.status, row.profile_id, row.requested_by, row.tenant_id],
    ['seace_prod2', '123', 'queued', 'pending', PROFILE, 'user-1', TENANT]);
  assert.equal(row.lima_day, 'today');
});

test('mayores reuses the lock, the dedupe and the count: preconditions first, then one transaction', async () => {
  const db = makeDb({ convocatorias: OPEN });
  await requestMajor(db);
  assert.deepEqual(db.events, ['FACTS', 'BEGIN', 'LOCK', 'EXPIRE', 'FIND_ACTIVE', 'COUNT', 'INSERT', 'COMMIT', 'RELEASE']);
  const lockCall = db.queries.find(call => /pg_advisory_xact_lock/.test(call.sql));
  assert.deepEqual(lockCall.values, ['manual_grading_requests', TENANT], 'the same lock key as menores');
  const insert = db.queries.find(call => /INSERT INTO manual_grading_requests/.test(call.sql));
  assert.deepEqual(insert.values.slice(5), ['queued', 'pending']);
});

test('the profile is resolved like the mayores service: active, tenant-scoped, optional explicit id', async () => {
  const db = makeDb({ convocatorias: OPEN });
  await requestMajor(db, { profileId: PROFILE });
  const profileCall = db.queries.find(call => /FROM company_profiles/.test(call.sql));
  assert.match(profileCall.sql, /tenant_id = \$1 AND is_active = true AND \(\$2::uuid IS NULL OR id = \$2::uuid\)\s+ORDER BY created_at, id LIMIT 1/);
  assert.deepEqual(profileCall.values, [TENANT, PROFILE]);
  const implicit = makeDb({ convocatorias: OPEN });
  await requestMajor(implicit);
  assert.deepEqual(implicit.queries.find(call => /FROM company_profiles/.test(call.sql)).values, [TENANT, null]);
});

test('the preconditions are read with the exact SQL fragments of the mayores list and detail', async () => {
  const db = makeDb({ convocatorias: OPEN });
  await requestMajor(db, { profileId: PROFILE });
  const facts = db.queries.find(call => /FROM prod2_discoveries/.test(call.sql));
  for (const name of ['PUBLISHED_FROM', 'PUBLISHED_VISIBILITY', 'CLOSED', 'JEV_UNION_JOIN', 'MATCH_JOIN']) {
    assert.equal(typeof db.prod2[name], 'string', name);
    assert.ok(facts.sql.includes(db.prod2[name]), `${name} is reused, not copied`);
  }
  assert.equal(db.prod2.JEV_ACCEPTANCE_THRESHOLD, 60);
  assert.match(db.prod2.MATCH_JOIN, /prod2_match_provenance/, 'a match counts only with provenance');
  assert.deepEqual(facts.values, [PROFILE, '123']);
  assert.ok(!facts.tx, 'the check runs before and outside the quota transaction');
});

test('a missing, unpublished or foreign convocatoria is a 404-style error and touches nothing', async () => {
  const db = makeDb({ convocatorias: OPEN });
  await assert.rejects(requestMajor(db, { idConvocatoria: '999' }), error => {
    assert.equal(error.code, 'contract_not_found');
    assert.equal(error.name, 'ManualGradingError');
    assert.equal(error.message, 'No se encontró la convocatoria.');
    return true;
  });
  assert.deepEqual(db.events, ['FACTS']);
  assert.equal(db.rows.length, 0);
});

test('a JEV below 60 or without a valid score is rejected without consuming quota', async () => {
  for (const jev of [59.99, 0, 12, null, undefined, 101, Number.NaN]) {
    const db = makeDb({ convocatorias: { 123: { jev } } });
    await assert.rejects(requestMajor(db), error => {
      assert.equal(error.code, 'jev_below_60', String(jev));
      assert.equal(error.message, 'La convocatoria no alcanzó la afinidad mínima para ser calificada.');
      return true;
    });
    assert.deepEqual(db.events, ['FACTS'], String(jev));
    assert.equal(db.rows.length, 0);
  }
  for (const jev of [60, 60.01, 100]) {
    assert.equal((await requestMajor(makeDb({ convocatorias: { 123: { jev } } }))).status, 'queued', String(jev));
  }
});

test('a closed convocatoria is rejected without consuming quota', async () => {
  const db = makeDb({ convocatorias: { 123: { jev: 80, closed: true } } });
  await assert.rejects(requestMajor(db), { code: 'convocatoria_cerrada', message: 'La convocatoria ya cerró; no se puede calificar.' });
  assert.deepEqual(db.events, ['FACTS']);
  assert.equal(db.rows.length, 0);
});

test('a convocatoria that already has a current published match is rejected with the agreed message and no quota', async () => {
  const db = makeDb({ convocatorias: { 123: { jev: 80, graded: true } } });
  await assert.rejects(requestMajor(db), { code: 'already_graded', message: 'Esta convocatoria ya tiene una calificación para tu empresa.' });
  assert.deepEqual(db.events, ['FACTS']);
  assert.equal(db.rows.length, 0);
});

test('preconditions are checked in order: affinity, then closing, then an existing grade', async () => {
  const all = { 123: { jev: 10, closed: true, graded: true }, 124: { jev: 80, closed: true, graded: true }, 125: { jev: 80, graded: true } };
  const expected = { 123: 'jev_below_60', 124: 'convocatoria_cerrada', 125: 'already_graded' };
  for (const id of Object.keys(all)) {
    await assert.rejects(requestMajor(makeDb({ convocatorias: all }), { idConvocatoria: id }), { code: expected[id] }, id);
  }
});

test('preconditions win over the daily limit and never consume quota even at the limit', async () => {
  const rows = seed(3);
  const closed = makeDb({ convocatorias: { 123: { jev: 80, closed: true } }, rows, env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
  await assert.rejects(requestMajor(closed), { code: 'convocatoria_cerrada' });
  const low = makeDb({ convocatorias: { 123: { jev: 30 } }, rows, env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
  await assert.rejects(requestMajor(low), { code: 'jev_below_60' });
  for (const db of [closed, low]) {
    assertUntouched(db);
    assert.equal(db.rows.length, 3);
  }
});

test('mayores without an active profile fails before reading the convocatoria or opening a transaction', async () => {
  const none = makeDb({ convocatorias: OPEN, profile: null });
  await assert.rejects(requestMajor(none), { code: 'profile_not_found', message: 'No se encontró un perfil activo para esta empresa.' });
  assert.deepEqual(none.events, []);
  const foreign = makeDb({ convocatorias: OPEN, profile: null });
  await assert.rejects(requestMajor(foreign, { profileId: PROFILE }), { code: 'profile_not_found' });
  assert.deepEqual(foreign.events, []);
});

test('an active mayores request is returned as is: no new row, no count, even at the limit', async () => {
  for (const status of ACTIVE) {
    const active = seed(1, { id: 'active-2', source_key: 'seace_prod2', source_id: '123', status, mode: 'queued' });
    const db = makeDb({ convocatorias: OPEN, rows: [...active, ...seed(2)], env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
    const result = await requestMajor(db);
    assert.equal(result.status, 'queued', status);
    assert.equal(result.request.id, 'active-2');
    assert.equal(result.request.status, status);
    assert.equal(db.rows.length, 3, 'no new row');
    assert.ok(!db.events.includes('INSERT') && !db.events.includes('COUNT'));
    assert.equal(db.events.at(-2), 'COMMIT');
  }
});

test('only an active request of the same source, convocatoria and profile deduplicates', async () => {
  const sameIdOtherSource = seed(1, { id: 'a', source_key: 'seace_prod6', source_id: '123', status: 'pending' });
  const otherConvocatoria = seed(1, { id: 'b', source_key: 'seace_prod2', source_id: '777', status: 'pending' });
  const otherProfile = seed(1, { id: 'c', source_key: 'seace_prod2', source_id: '123', status: 'pending', profile_id: '44444444-4444-4444-8444-444444444444' });
  const finished = [
    ...seed(1, { id: 'd', source_key: 'seace_prod2', source_id: '123', status: 'done' }),
    ...seed(1, { id: 'e', source_key: 'seace_prod2', source_id: '123', status: 'failed', reason: 'reading_failed' }),
  ];
  const db = makeDb({ convocatorias: OPEN, rows: [...sameIdOtherSource, ...otherConvocatoria, ...otherProfile, ...finished] });
  const result = await requestMajor(db);
  assert.equal(result.request.status, 'pending');
  assert.notEqual(result.request.id, 'a');
  assert.equal(db.rows.length, 6, 'a fresh request was inserted');
  assert.ok(db.events.includes('INSERT'));
});

test('the daily limit is one count shared by menores and mayores', async () => {
  const minorRows = seed(3);
  const blocked = makeDb({ convocatorias: OPEN, rows: minorRows, env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
  await assert.rejects(requestMajor(blocked), error => {
    assert.equal(error.code, 'daily_limit_reached');
    assert.equal(error.message, 'Se alcanzó el límite diario de 3 calificaciones manuales de tu empresa.');
    assert.deepEqual([error.used, error.limit], [3, 3]);
    return true;
  });
  assert.equal(blocked.rows.length, 3);
  assert.ok(blocked.events.includes('ROLLBACK') && !blocked.events.includes('INSERT'));

  const db = makeDb({ convocatorias: OPEN, contracts: { 501: {}, 502: {} }, env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
  assert.equal((await requestMajor(db)).status, 'queued');
  assert.equal((await request(db)).status, 'queued', 'a menores request after a mayores one');
  assert.equal(usedBy(db), 2);
  assert.equal((await requestMajor(db, { idConvocatoria: '124' })).status, 'queued');
  await assert.rejects(requestMajor(db, { idConvocatoria: '125' }), { code: 'daily_limit_reached' });
  await assert.rejects(request(db, { idContrato: 502 }), { code: 'daily_limit_reached' }, 'the mayores requests filled the shared quota');
  assert.equal(usedBy(db), 3);
});

test('failed and finished mayores requests keep counting toward the limit', async () => {
  const rows = [
    ...seed(1, { source_key: 'seace_prod2', source_id: '1', status: 'failed', reason: 'payment_uncertain' }),
    ...seed(1, { source_key: 'seace_prod2', source_id: '2', status: 'done' }),
  ];
  const db = makeDb({ convocatorias: OPEN, rows, env: { MANUAL_GRADING_DAILY_LIMIT: '2' } });
  await assert.rejects(requestMajor(db), { code: 'daily_limit_reached' });
  const below = makeDb({ convocatorias: OPEN, rows, env: { MANUAL_GRADING_DAILY_LIMIT: '3' } });
  assert.equal((await requestMajor(below)).status, 'queued');
  assert.equal(usedBy(below), 3);
});

test('simultaneous mayores requests of one tenant cannot exceed the limit', async () => {
  const many = Object.fromEntries(Array.from({ length: 5 }, (_, index) => [801 + index, { jev: 70 }]));
  const db = makeDb({ convocatorias: many, rows: seed(2), env: { MANUAL_GRADING_DAILY_LIMIT: '4' } });
  const outcomes = await Promise.allSettled(Object.keys(many).map(idConvocatoria => requestMajor(db, { idConvocatoria })));
  assert.equal(outcomes.filter(outcome => outcome.status === 'fulfilled').length, 2);
  const rejected = outcomes.filter(outcome => outcome.status === 'rejected');
  assert.equal(rejected.length, 3);
  assert.ok(rejected.every(outcome => outcome.reason.code === 'daily_limit_reached'));
  assert.equal(usedBy(db), 4);
});

test('simultaneous mayores requests for the same convocatoria create a single request', async () => {
  const db = makeDb({ convocatorias: OPEN });
  const outcomes = await Promise.all([requestMajor(db), requestMajor(db), requestMajor(db)]);
  assert.equal(db.rows.length, 1);
  assert.equal(new Set(outcomes.map(outcome => outcome.request.id)).size, 1);
});

test('mayores limits are per tenant', async () => {
  const db = makeDb({ convocatorias: OPEN, rows: seed(2, { tenant_id: OTHER_TENANT }), env: { MANUAL_GRADING_DAILY_LIMIT: '2' } });
  assert.equal((await requestMajor(db)).status, 'queued');
});

test('mayores status returns the latest request of that convocatoria and the usage of all sources', async () => {
  const older = seed(1, { id: 'old', source_key: 'seace_prod2', source_id: '123', status: 'failed', reason: 'reading_failed' });
  const newest = seed(1, { id: 'new', source_key: 'seace_prod2', source_id: '123', status: 'reading', mode: 'queued' });
  const minorSameId = seed(1, { id: 'minor', source_key: 'seace_prod6', source_id: '123', status: 'done' });
  const db = makeDb({ convocatorias: OPEN, rows: [...older, ...newest, ...minorSameId] });
  const status = await db.service.majorGradingStatus({ tenantId: TENANT, idConvocatoria: '123', profileId: PROFILE });
  assert.deepEqual([status.request.status, status.request.mode, status.request.reason], ['reading', 'queued', null]);
  assert.ok(status.request.created_at && status.request.updated_at);
  assert.deepEqual(JSON.parse(JSON.stringify(status.usage)), { used: 3, limit: 100 });
  assert.equal(Object.hasOwn(status.request, 'stale'), false);
  const statusCall = db.queries.find(call => /AS stale/.test(call.sql));
  assert.equal(statusCall.values[1], 'seace_prod2');
  assert.equal(statusCall.values[2], '123');
});

test('mayores status without requests or without a profile reports usage and never writes', async () => {
  const empty = makeDb({ convocatorias: OPEN, rows: seed(5), env: { MANUAL_GRADING_DAILY_LIMIT: '10' } });
  const none = await empty.service.majorGradingStatus({ tenantId: TENANT, idConvocatoria: '123' });
  assert.equal(none.request, null);
  assert.deepEqual(JSON.parse(JSON.stringify(none.usage)), { used: 5, limit: 10 });
  assert.deepEqual(empty.events, ['COUNT']);
  const noProfile = makeDb({ convocatorias: OPEN, rows: seed(2), profile: null });
  for (const profileId of [null, PROFILE]) {
    const status = await noProfile.service.majorGradingStatus({ tenantId: TENANT, idConvocatoria: '123', profileId });
    assert.equal(status.request, null);
    assert.equal(status.usage.used, 2);
  }
});

test('the facts and blocker helpers expose the same decision the page uses to offer «Calificar»', async () => {
  const db = makeDb({ convocatorias: { 123: { jev: 72, closed: false }, 124: { jev: 72, closed: true }, 125: { jev: null } } });
  const open = await db.service.majorGradingFacts({ idConvocatoria: '123', profileId: PROFILE });
  assert.deepEqual(JSON.parse(JSON.stringify(open)), { jevScore: 72, closed: false, graded: false });
  assert.equal(db.service.majorGradingBlocker(open), null);
  assert.equal(db.service.majorGradingBlocker(await db.service.majorGradingFacts({ idConvocatoria: '124', profileId: PROFILE })), 'convocatoria_cerrada');
  assert.equal(db.service.majorGradingBlocker(await db.service.majorGradingFacts({ idConvocatoria: '125', profileId: PROFILE })), 'jev_below_60');
  assert.equal(await db.service.majorGradingFacts({ idConvocatoria: '999', profileId: null }), null);
});

test('every typed error has an HTTP status shared by both routes', () => {
  const { MANUAL_GRADING_ERROR_STATUS: status } = makeDb().service;
  assert.deepEqual(JSON.parse(JSON.stringify(status)), {
    daily_limit_reached: 429, profile_not_found: 409, contract_not_found: 404, jev_below_60: 409, convocatoria_cerrada: 409, already_graded: 409,
  });
});

// ---- Ruta HTTP -----------------------------------------------------------------------------------------------

// Una sola instancia: la ruta comprueba `instanceof ManualGradingError` contra la clase de este módulo.
const idleDb = { pool: {}, query: async () => ({ rows: [] }) };
const sharedService = load('manualGrading.ts', {
  '@/server/db/client': idleDb,
  './matchAnalysis': { analyzeMatchDirect: async () => ({}) },
  './prod2Opportunities': loadProd2(idleDb),
}, { process: { env: {} } });

function loadRoute({ requestResult, statusResult, usage = { used: 4, limit: 100 }, tenant = async () => TENANT } = {}) {
  const calls = { request: [], status: [] };
  const route = load('../../app/api/contracts/[id]/grade/route.ts', {
    'next/server': { NextResponse: { json: (body, init) => Response.json(body, init) } },
    '@/server/auth/tenant': { requireTenantId: tenant, currentUserId: async () => 'user-1' },
    '@/server/services/manualGrading': {
      ...sharedService,
      manualGradingUsage: async () => usage,
      requestMinorGrading: async input => { calls.request.push(input); return requestResult(input); },
      minorGradingStatus: async input => { calls.status.push(input); return statusResult(input); },
    },
  });
  return { route, calls, service: sharedService };
}
const params = id => ({ params: Promise.resolve({ id }) });
const post = (route, id, body) => route.POST({ json: async () => body }, params(id));
const get = (route, id, query = '') => route.GET({ nextUrl: new URL(`http://localhost/api/contracts/${id}/grade${query}`) }, params(id));

test('POST answers 200 with the result when graded directly and 202 when queued', async () => {
  const done = loadRoute({ requestResult: async () => ({ status: 'done', data: { match_id: 7, verdict: 'verde', score: 90 } }) });
  const ok = await post(done.route, '501', { force: true, profile_id: PROFILE });
  assert.equal(ok.status, 200);
  assert.deepEqual(await ok.json(), { data: { status: 'done', result: { match_id: 7, verdict: 'verde', score: 90 }, usage: { used: 4, limit: 100 } } });
  assert.deepEqual(JSON.parse(JSON.stringify(done.calls.request[0])), { tenantId: TENANT, idContrato: 501, profileId: PROFILE, actorId: 'user-1', force: true });

  const queued = loadRoute({ requestResult: async () => ({ status: 'queued', request: { id: '1', status: 'pending', mode: 'queued', reason: null } }) });
  const accepted = await post(queued.route, '501', {});
  assert.equal(accepted.status, 202);
  const body = await accepted.json();
  assert.equal(body.data.status, 'queued');
  assert.equal(body.data.request.status, 'pending');
  assert.equal(queued.calls.request[0].force, false);
});

test('POST maps the typed errors to 429, 409 and 404 and evaluation failures to 422 with a reason', async () => {
  const fail = error => loadRoute({ requestResult: async () => { throw error; } });
  const { service } = loadRoute({ requestResult: async () => ({}) });
  const limit = new service.ManualGradingError('daily_limit_reached', 'Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.', { limit: 100, used: 100 });
  const limited = await post(fail(limit).route, '501', {});
  assert.equal(limited.status, 429);
  assert.deepEqual(await limited.json(), {
    error: 'Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.', code: 'daily_limit_reached', usage: { used: 100, limit: 100 },
  });
  const noProfile = await post(fail(new service.ManualGradingError('profile_not_found', 'No se encontró un perfil activo para esta empresa.')).route, '501', {});
  assert.equal(noProfile.status, 409);
  const missing = await post(fail(new service.ManualGradingError('contract_not_found', 'No se encontró la licitación.')).route, '501', {});
  assert.equal(missing.status, 404);
  const failed = await post(fail(new Error('Gemini devolvió una respuesta vacía.')).route, '501', {});
  assert.equal(failed.status, 422);
  assert.deepEqual(await failed.json(), { error: 'Gemini devolvió una respuesta vacía.', code: 'grading_failed', reason: 'invalid_model_output' });
});

test('POST validates the contract id and the profile id before calling the service', async () => {
  const unused = loadRoute({ requestResult: async () => { throw new Error('must not be called'); } });
  for (const id of ['abc', '0', '-4', '1.5', '9007199254740993']) {
    assert.equal((await post(unused.route, id, {})).status, 400, id);
  }
  for (const profile_id of ['nope', 12, '33333333-3333-0333-8333-333333333333']) {
    assert.equal((await post(unused.route, '501', { profile_id })).status, 400, String(profile_id));
  }
  assert.equal(unused.calls.request.length, 0);
});

test('POST and GET pass authentication failures through', async () => {
  const denied = loadRoute({ tenant: async () => { throw new Response('Missing tenant context', { status: 401 }); }, requestResult: async () => ({}), statusResult: async () => ({}) });
  assert.equal((await post(denied.route, '501', {})).status, 401);
  assert.equal((await get(denied.route, '501')).status, 401);
});

test('GET returns the status payload and validates its inputs', async () => {
  const payload = { request: { status: 'reading', mode: 'queued', reason: null, created_at: '2026-10-30T10:00:00.000Z', updated_at: '2026-10-30T10:01:00.000Z' }, usage: { used: 4, limit: 100 } };
  const ok = loadRoute({ statusResult: async () => payload });
  const response = await get(ok.route, '501', `?profile_id=${PROFILE}`);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), { data: payload });
  assert.deepEqual(JSON.parse(JSON.stringify(ok.calls.status[0])), { tenantId: TENANT, idContrato: 501, profileId: PROFILE });

  const implicit = await get(ok.route, '501');
  assert.equal(implicit.status, 200);
  assert.equal(ok.calls.status[1].profileId, null);
  assert.equal((await get(ok.route, 'x')).status, 400);
  assert.equal((await get(ok.route, '501', '?profile_id=bad')).status, 400);
  assert.equal(ok.calls.status.length, 2);
});

// ---- Ruta HTTP de mayores --------------------------------------------------------------------------------------

function loadMajorRoute({ requestResult, statusResult, usage = { used: 4, limit: 100 }, tenant = async () => TENANT } = {}) {
  const calls = { request: [], status: [] };
  const route = load('../../app/api/prod2/opportunities/[id]/grade/route.ts', {
    'next/server': { NextResponse: { json: (body, init) => Response.json(body, init) } },
    '@/server/auth/tenant': { requireTenantId: tenant, currentUserId: async () => 'user-1' },
    '@/server/services/manualGrading': {
      ...sharedService,
      manualGradingUsage: async () => usage,
      requestMajorGrading: async input => { calls.request.push(input); return requestResult(input); },
      majorGradingStatus: async input => { calls.status.push(input); return statusResult(input); },
    },
  });
  return { route, calls };
}
const majorGet = (route, id, query = '') => route.GET({ nextUrl: new URL(`http://localhost/api/prod2/opportunities/${id}/grade${query}`) }, params(id));
const queuedResult = async () => ({ status: 'queued', request: { id: '1', status: 'pending', mode: 'queued', reason: null } });

test('mayores POST answers 202 queued with the request and the usage, never 200', async () => {
  const queued = loadMajorRoute({ requestResult: queuedResult });
  const accepted = await post(queued.route, '123', { profile_id: PROFILE, force: true });
  assert.equal(accepted.status, 202);
  assert.deepEqual(await accepted.json(), { data: { status: 'queued', request: { id: '1', status: 'pending', mode: 'queued', reason: null }, usage: { used: 4, limit: 100 } } });
  assert.deepEqual(JSON.parse(JSON.stringify(queued.calls.request[0])), { tenantId: TENANT, idConvocatoria: '123', profileId: PROFILE, actorId: 'user-1' });
  await post(queued.route, '9223372036854775807', {});
  assert.equal(queued.calls.request[1].idConvocatoria, '9223372036854775807', 'the id stays text so a bigint never loses precision');
  assert.equal(queued.calls.request[1].profileId, undefined);
});

test('mayores POST maps the typed errors: 429 with usage, 404, and 409 with a stable code', async () => {
  const { service } = { service: sharedService };
  const fail = error => loadMajorRoute({ requestResult: async () => { throw error; } });
  const limit = new service.ManualGradingError('daily_limit_reached', 'Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.', { limit: 100, used: 100 });
  const limited = await post(fail(limit).route, '123', {});
  assert.equal(limited.status, 429);
  assert.deepEqual(await limited.json(), {
    error: 'Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.', code: 'daily_limit_reached', usage: { used: 100, limit: 100 },
  });
  const cases = [
    ['contract_not_found', 'No se encontró la convocatoria.', 404],
    ['profile_not_found', 'No se encontró un perfil activo para esta empresa.', 409],
    ['jev_below_60', 'La convocatoria no alcanzó la afinidad mínima para ser calificada.', 409],
    ['convocatoria_cerrada', 'La convocatoria ya cerró; no se puede calificar.', 409],
    ['already_graded', 'Esta convocatoria ya tiene una calificación para tu empresa.', 409],
  ];
  for (const [code, message, status] of cases) {
    const response = await post(fail(new service.ManualGradingError(code, message)).route, '123', {});
    assert.equal(response.status, status, code);
    assert.deepEqual(await response.json(), { error: message, code }, code);
  }
});

test('mayores POST hides unexpected errors behind a generic 500', async () => {
  const boom = loadMajorRoute({ requestResult: async () => { throw new Error('connection to 10.0.0.5 refused'); } });
  const response = await post(boom.route, '123', {});
  assert.equal(response.status, 500);
  assert.deepEqual(await response.json(), { error: 'No se pudo pedir la calificación.', code: 'request_failed' });
});

test('mayores POST validates the convocatoria id and the profile id before calling the service', async () => {
  const unused = loadMajorRoute({ requestResult: async () => { throw new Error('must not be called'); } });
  for (const id of ['abc', '0', '-4', '1.5', '012', '12345678901234567890', ' 1', '9223372036854775808', '9999999999999999999']) {
    assert.equal((await post(unused.route, id, {})).status, 400, id);
  }
  for (const profile_id of ['nope', 12, '33333333-3333-0333-8333-333333333333']) {
    assert.equal((await post(unused.route, '123', { profile_id })).status, 400, String(profile_id));
  }
  assert.equal(unused.calls.request.length, 0);
});

test('mayores POST and GET pass authentication failures through', async () => {
  const denied = loadMajorRoute({ tenant: async () => { throw new Response('Missing tenant context', { status: 401 }); }, requestResult: async () => ({}), statusResult: async () => ({}) });
  assert.equal((await post(denied.route, '123', {})).status, 401);
  assert.equal((await majorGet(denied.route, '123')).status, 401);
});

test('mayores GET returns the status payload without caching and validates its inputs', async () => {
  const payload = { request: { status: 'reading', mode: 'queued', reason: null, created_at: '2026-10-30T10:00:00.000Z', updated_at: '2026-10-30T10:01:00.000Z' }, usage: { used: 4, limit: 100 } };
  const ok = loadMajorRoute({ statusResult: async () => payload });
  const response = await majorGet(ok.route, '123', `?profile_id=${PROFILE}`);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), { data: payload });
  assert.deepEqual(JSON.parse(JSON.stringify(ok.calls.status[0])), { tenantId: TENANT, idConvocatoria: '123', profileId: PROFILE });
  assert.equal((await majorGet(ok.route, '123')).status, 200);
  assert.equal(ok.calls.status[1].profileId, null);
  assert.equal((await majorGet(ok.route, 'x')).status, 400);
  assert.equal((await majorGet(ok.route, '123', '?profile_id=bad')).status, 400);
  assert.equal(ok.calls.status.length, 2);
});

test('mayores GET never leaks internal errors', async () => {
  const boom = loadMajorRoute({ statusResult: async () => { throw new Error('relation does not exist'); } });
  const response = await majorGet(boom.route, '123');
  assert.equal(response.status, 500);
  assert.deepEqual(await response.json(), { error: 'No se pudo consultar el estado de la calificación.' });
});
