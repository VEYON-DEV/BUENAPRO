const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, requireModule) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(compiled, { exports, require: requireModule, Date, Intl, URLSearchParams, Response });
  return exports;
}
const schedule = load(`${__dirname}/../../lib/procurementSchedule.ts`, () => { throw new Error('Unexpected schedule import'); });
const prod2Analysis = load(`${__dirname}/../../lib/prod2Analysis.ts`, name => { if (name === '@/lib/procurementSchedule') return schedule; throw new Error(`Unexpected import ${name}`); });
const savedOpportunities = load(`${__dirname}/savedOpportunities.ts`, name => {
  if (name === '@/server/db/client') return { query: async () => { throw new Error('Unexpected bookmark query'); } };
  throw new Error(`Unexpected import ${name}`);
});
const calls = [];
const service = load(`${__dirname}/procurementTimeline.ts`, name => {
  if (name === '@/lib/procurementSchedule') return schedule;
  if (name === '@/lib/prod2Analysis') return prod2Analysis;
  if (name === './savedOpportunities') return savedOpportunities;
  if (name === '@/server/db/client') return { query: async (sql, values) => { calls.push({ sql, values }); return { rows: [] }; } };
  throw new Error(`Unexpected import ${name}`);
});

test('validation rejects invalid sources and oversized queries', () => {
  assert.equal(service.parseTimelineParams(new URLSearchParams()).source, 'prod6');
  assert.throws(() => service.parseTimelineParams(new URLSearchParams('source=other')), /source must/);
  assert.throws(() => service.parseTimelineParams(new URLSearchParams({ q: 'x'.repeat(121) })), /q must/);
});

test('both SQL branches isolate active tenant profiles and prefer current documentary evaluation', () => {
  for (const source of ['prod4', 'prod6']) {
    const sql = service.timelineSql(source);
    assert.match(sql, /cp.tenant_id = \$1 AND cp.is_active = true/g);
    assert.match(sql, /matched.verdict IN \('verde', 'ambar'\) OR \(matched.verdict IS NULL AND fit.fit_level >= 2\)/);
    assert.match(sql, /extraction.id IS NOT NULL/);
    assert.match(sql, /e.is_current = true AND e.quality <> 'failed'/);
    assert.doesNotMatch(sql, /LIMIT \$|OFFSET/);
    assert.match(sql, /ILIKE '%' \|\| \$2 \|\| '%'/);
  }
});

test('day precision survives, past offer deadlines excluded, unknown and same-day retained', () => {
  const base = { id: 'uuid', source_id: '124', code: 'CP-124', title: 'Software', entity: 'Entidad', object_type: 'service', fit_level: 3, fit_points: 35, score: null, verdict: null, schedule_fetched_at: null };
  const row = (code, date) => ({ ...base, code, official_schedule: [{ nombreEtapa: 'Presentación de ofertas', fechaInicio: date, fechaFin: date }] });
  const data = service.normalizeTimelineRows([row('past', '01/10/2026'), row('today', '02/10/2026'), row('future', '03/10/2026'), { ...base, code: 'unknown', official_schedule: [] }], 'prod4', new Date('2026-10-02T17:00:00Z'));
  assert.equal(data.length, 3);
  assert.equal(data[0].code, 'today');
  assert.equal(data[0].deadline.at, '2026-10-02');
  assert.equal(data[0].deadline.precision, 'day');
  assert.equal(data[0].detailHref, '/oportunidad/seace/124');
  assert.equal(data.at(-1).deadline, null);
});

test('minors include every official stage, retain no-hour and reject elapsed minute deadline', () => {
  const raw = [{ nomEtapaContrato: 'Consulta', fecIni: '01/10/2026', fecFin: '02/10/2026' }, { nomEtapaContrato: 'Cotización', fecIni: '02/10/2026 10:00', fecFin: '02/10/2026 13:00' }];
  const base = { id: 'minor-uuid', source_id: '321', code: 'CM', title: 'Software', entity: 'Entidad', object_type: 'good', fit_level: 2, fit_points: 15, score: 84, verdict: 'ambar', official_schedule: raw, schedule_fetched_at: '2026-10-02T15:00:00Z' };
  const data = service.normalizeTimelineRows([base], 'prod6', new Date('2026-10-02T17:00:00Z'));
  assert.equal(data.length, 1);
  assert.equal(data[0].schedule.length, 2);
  assert.equal(data[0].schedule[0].endPrecision, 'day');
  assert.equal(data[0].verdict, 'ambar');
  assert.equal(data[0].detailHref, '/oportunidad/321');
  assert.equal(service.normalizeTimelineRows([base], 'prod6', new Date('2026-10-02T19:00:00Z')).length, 0);
});

test('service calls DB once with bound tenant/query values and no pagination cap', async () => {
  const result = await service.getProcurementTimeline('tenant-a', { source: 'prod4', q: "%' OR 1=1" });
  assert.equal(calls.length, 1);
  assert.deepEqual(Array.from(calls[0].values), ['tenant-a', "%' OR 1=1"]);
  assert.equal(result.meta.count, 0);
  assert.equal(result.meta.timezone, 'America/Lima');
});

test('publication uses raw official precision, not synthetic DB timestamp', () => {
  const base = { id: 'a', source_id: '1', code: 'CM', title: 'Software', entity: 'Entidad', object_type: 'good', fit_level: 2, fit_points: 15, score: null, verdict: null, official_schedule: [], schedule_fetched_at: null };
  const data = service.normalizeTimelineRows([{ ...base, published_raw: '02/10/2026' }, { ...base, published_raw: '02/10/2026 09:08:37' }], 'prod6', new Date('2026-10-02T17:00:00Z'));
  assert.equal(data[0].schedule[0].startPrecision, 'day');
  assert.equal(data[1].schedule[0].startPrecision, 'second');
  assert.equal(data[0].deadline, null);
});

test('red/gray evaluations override high affinity and never leak into response', () => {
  const base = { id: 'a', source_id: '1', code: 'CM', title: '', entity: '', object_type: 'good', fit_level: 3, fit_points: 45, score: 15, official_schedule: [], schedule_fetched_at: null };
  const data = service.normalizeTimelineRows([{ ...base, verdict: 'rojo' }, { ...base, verdict: 'gris' }, { ...base, verdict: null, fit_level: 1 }], 'prod6');
  assert.equal(data.length, 0);
});

test('API requires tenant context before querying, binds tenant and disables shared cache', async () => {
  let authenticated = false;
  let queried = false;
  const route = load(`${__dirname}/../../app/api/cronograma/route.ts`, name => {
    if (name === 'next/server') return { NextResponse: { json: (body, init) => ({ body, init }) } };
    if (name === '@/server/auth/tenant') return { requireTenantId: async () => { if (!authenticated) throw new Response('Unauthorized', { status: 401 }); return 'tenant-private'; } };
    if (name === '@/server/services/procurementTimeline') return { parseTimelineParams: service.parseTimelineParams, getProcurementTimeline: async tenant => { queried = true; assert.equal(tenant, 'tenant-private'); return { data: [] }; } };
    throw new Error(`Unexpected import ${name}`);
  });
  const request = { nextUrl: { searchParams: new URLSearchParams('source=prod4') } };
  const rejected = await route.GET(request);
  assert.equal(rejected.init.status, 401);
  assert.equal(queried, false);
  authenticated = true;
  const result = await route.GET(request);
  assert.equal(result.init.headers['Cache-Control'], 'private, no-store');
});

test('Buscador SEACE timeline lists only the tenant saved procedures with their official stages', () => {
  const sql = service.prod2TimelineSql();
  assert.match(sql, /FROM saved_opportunities s/);
  assert.match(sql, /WHERE s.tenant_id = \$1/);
  assert.match(sql, /d.schedule_json AS official_schedule/);
  assert.equal(service.parseTimelineParams(new URLSearchParams('source=prod2')).source, 'prod2');
  const row = { id: 'opp', source_id: '987', code: 'CP-ABR-1', title: 'Licencias', entity: 'Entidad', object_type: 'service', fit_level: null, fit_points: null, score: null, verdict: null, schedule_fetched_at: null, saved: true,
    official_schedule: [{ stage: 'Presentación de ofertas', starts_at_text: '05/10/2026', ends_at_text: '10/10/2026' }] };
  const [item] = service.normalizeTimelineRows([row], 'prod2', new Date('2026-10-06T17:00:00Z'));
  assert.equal(item.status, 'saved');
  assert.equal(item.saved, true);
  assert.equal(item.detailHref, '/oportunidad/prod2/987');
  assert.equal(item.deadline.at, '2026-10-10');
  assert.equal(service.normalizeTimelineRows([row], 'prod2', new Date('2026-10-12T17:00:00Z')).length, 0, 'closed offers leave the Gantt');
});

test('saved contratos menores stay on the Gantt even without affinity', () => {
  assert.match(service.timelineSql('prod6'), /OR EXISTS \(SELECT 1 FROM saved_contracts sc WHERE sc.tenant_id = \$1/);
  const base = { id: 'uuid', source_id: '55', code: 'CM-55', title: 'Servicio', entity: 'Entidad', object_type: 'service', fit_level: 0, fit_points: 0, score: null, verdict: null, schedule_fetched_at: null, official_schedule: [] };
  assert.equal(service.normalizeTimelineRows([base], 'prod6').length, 0);
  const [saved] = service.normalizeTimelineRows([{ ...base, saved: true }], 'prod6');
  assert.equal(saved.status, 'saved');
});

test('missing saved_opportunities table yields an empty Buscador SEACE timeline', async () => {
  const missing = load(`${__dirname}/procurementTimeline.ts`, name => {
    if (name === '@/lib/procurementSchedule') return schedule;
    if (name === '@/lib/prod2Analysis') return prod2Analysis;
    if (name === './savedOpportunities') return savedOpportunities;
    if (name === '@/server/db/client') return { query: async () => { throw Object.assign(new Error('relation "saved_opportunities" does not exist'), { code: '42P01' }); } };
    throw new Error(`Unexpected import ${name}`);
  });
  const result = await missing.getProcurementTimeline('tenant', { source: 'prod2', q: '' });
  assert.equal(result.data.length, 0);
  await assert.rejects(missing.getProcurementTimeline('tenant', { source: 'prod6', q: '' }));
});

test('Buscador SEACE timeline does not conceal other missing relations', async () => {
  const error = Object.assign(new Error('relation "prod2_details" does not exist'), { code: '42P01' });
  const broken = load(`${__dirname}/procurementTimeline.ts`, name => {
    if (name === '@/lib/procurementSchedule') return schedule;
    if (name === '@/lib/prod2Analysis') return prod2Analysis;
    if (name === './savedOpportunities') return savedOpportunities;
    if (name === '@/server/db/client') return { query: async () => { throw error; } };
    throw new Error(`Unexpected import ${name}`);
  });
  await assert.rejects(broken.getProcurementTimeline('tenant', { source: 'prod2', q: '' }), actual => actual === error);
});
