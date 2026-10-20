const { describe, test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const { commercial: api, commercialSql: sql } = require('./commercialClassification.test-support.cjs');
const dsn = process.env.BUENAPRO_COMMERCIAL_TEST_DATABASE_URL;
const plain = value => JSON.parse(JSON.stringify(value));

function classification(overrides = {}) {
  const value = { taxonomy_version: 'commercial_taxonomy_v1', primary_area: 'software_ti', secondary_areas: [], work_types: ['consultoria'],
    specialties: [], support_status: 'evidence_backed', ...overrides };
  return { ...value, evidence: [
    { axis: 'area', value: value.primary_area, citation: 'Servicio de consultoría en software.', page: 2 },
    ...value.secondary_areas.map(value => ({ axis: 'area', value, citation: 'Otro rubro expresamente citado.', page: 3 })),
    ...value.work_types.map(value => ({ axis: 'work_type', value, citation: 'Trabajo expresamente solicitado.', page: 4 })),
    ...value.specialties.map(value => ({ axis: 'specialty', value, citation: 'Especialidad expresamente solicitada.', page: 5 })),
  ] };
}
const extraction = (block, extra = {}) => ({ raw: { commercial_classification: block }, summary: {}, current: true, quality: 'auto', ...extra });
const ai = classification({ primary_area: 'inteligencia_artificial', secondary_areas: ['software_ti'], work_types: ['desarrollo_software'], specialties: ['chatbot', 'agentes_ia'] });
const fixtures = [
  [extraction(classification())], [extraction(ai)], [extraction(classification({ support_status: 'preliminary' }))],
  [extraction(classification({ support_status: 'needs_review' }))],
  [extraction(undefined, { raw: {}, summary: { commercial_classification: ai } })],
  [extraction(undefined)], [extraction({ taxonomy_version: 'old' })],
  [extraction(ai), extraction(undefined)], // latest current is untagged: no resurrection of an older reading
  [extraction(ai), extraction(classification(), { quality: 'failed' })],
  [extraction(ai, { current: false })],
  [extraction({ ...ai, support_status: 'future' }, { summary: { commercial_classification: classification() } })],
  [extraction(classification({ primary_area: 'sin_clasificar', work_types: [], support_status: 'needs_review' }))],
  [extraction({ ...ai, evidence: ai.evidence.slice(1) })],
  [extraction({ ...ai, secondary_areas: ['software_ti', 'software_ti'] })],
  [extraction({ ...ai, secondary_areas: ['inteligencia_artificial'] })],
  [extraction({ ...ai, specialties: ['chatbot', 'chatbot'] })],
  [extraction({ ...ai, secondary_areas: [null] })],
  [extraction({ ...ai, work_types: 'consultoria' })],
  [extraction('software_ti')], [extraction([])], [extraction({ ...ai, specialties: null })],
  ...[null, '2', 0, -1, 1.5, 9007199254740992, 1e100].map(page => [extraction({ ...ai, evidence: ai.evidence.map((value, index) => index ? value : { ...value, page }) })]),
  ...['', '  ', '\t\n'].map(citation => [extraction({ ...ai, evidence: ai.evidence.map((value, index) => index ? value : { ...value, citation }) })]),
  [extraction(ai, { raw: { commercial_classification: ai, retrieval_evidence_audit: { errors: [{ kind: 'unverified_commercial_citation', citation_index: 1 }] } } })],
  [extraction(ai, { raw: { commercial_classification: ai, retrieval_evidence_audit: { errors: [{ kind: 'unverified_facet_citation' }] } } })],
  [extraction(ai, { raw: { commercial_classification: ai, retrieval_evidence_audit: { errors: 'malformed' }, requires_human_review: true } })],
  [extraction(null, { summary: { commercial_classification: ai } })],
];

describe('commercial SQL against a disposable loopback PostgreSQL fixture', { skip: !dsn }, () => {
  let db;
  const schema = `commercial_${randomUUID().replaceAll('-', '')}`;
  const expected = fixtures.map(rows => {
    const row = rows.filter(row => row.current && row.quality !== 'failed').at(-1);
    return api.commercialClassificationFromExtraction(row?.raw, row?.summary);
  });
  before(async () => {
    const url = new URL(dsn);
    assert.equal(url.hostname, '127.0.0.1', 'only explicit loopback QA is allowed');
    assert.equal(url.pathname, '/commercial_taxonomy_qa', 'never connect to an application database');
    const { Client } = require('pg');
    db = new Client({ connectionString: dsn });
    await db.connect();
    await db.query(`CREATE SCHEMA ${schema}`);
    await db.query(`SET search_path TO ${schema}`);
    await db.query(`CREATE TABLE prod2_discoveries (id_convocatoria bigint PRIMARY KEY);
      CREATE TABLE prod2_document_extractions (id bigint PRIMARY KEY, id_convocatoria bigint, is_current boolean, quality text, created_at timestamptz, raw_extraction_json jsonb, summary_json jsonb);
      CREATE TABLE seace_contracts (id_contrato bigint PRIMARY KEY);
      CREATE TABLE contract_documents (id bigint PRIMARY KEY, id_contrato bigint);
      CREATE TABLE tdr_extractions (id bigint PRIMARY KEY, contract_document_id bigint, is_current boolean, quality text, created_at timestamptz, raw_extraction_json jsonb, summary_json jsonb)`);
    for (const [index, rows] of fixtures.entries()) {
      const id = index + 1;
      await db.query('INSERT INTO prod2_discoveries VALUES($1)', [id]);
      await db.query('INSERT INTO seace_contracts VALUES($1)', [id]);
      for (const [revision, row] of rows.entries()) {
        const extractionId = id * 10 + revision;
        await db.query('INSERT INTO prod2_document_extractions VALUES($1,$2,$3,$4,$5,$6,$7)',
          [extractionId, id, row.current, row.quality, new Date(Date.UTC(2026, 9, 20, 0, 0, revision)), row.raw, row.summary]);
        await db.query('INSERT INTO contract_documents VALUES($1,$2)', [extractionId, id]);
        await db.query('INSERT INTO tdr_extractions VALUES($1,$2,$3,$4,$5,$6,$7)',
          [extractionId, extractionId, row.current, row.quality, new Date(Date.UTC(2026, 9, 20, 0, 0, revision)), row.raw, row.summary]);
      }
    }
  });
  after(async () => {
    if (!db) return;
    try { await db.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`); } finally { await db.end(); }
  });

  const sources = [
    { name: 'PROD2', from: 'prod2_discoveries p', id: 'p.id_convocatoria', join: sql.PROD2_COMMERCIAL_JOIN },
    { name: 'menores', from: 'seace_contracts c', id: 'c.id_contrato', join: sql.PROD6_COMMERCIAL_JOIN },
  ];
  for (const source of sources) {
    test(`${source.name}: actual JSONB SQL matches strict normalization across ${fixtures.length} malformed/current/audit cases`, async () => {
      const result = await db.query(`SELECT ${source.id} AS id, ${sql.COMMERCIAL_SELECT} FROM ${source.from} ${source.join} ORDER BY ${source.id}`);
      assert.equal(result.rows.length, fixtures.length);
      for (const row of result.rows) assert.deepEqual(plain(api.normalizeCommercialClassification(row.commercial_classification)), plain(expected[Number(row.id) - 1]), `fixture ${row.id}`);
    });

    test(`${source.name}: OR within axes / AND across axes filters before count and pagination`, async () => {
      for (const query of [
        'commercial_area=software_ti,inteligencia_artificial&commercial_work=desarrollo_software&commercial_specialty=chatbot,agentes_ia&commercial_status=evidence_backed',
        'commercial_area=software_ti&commercial_status=preliminary,needs_review',
        'commercial_status=unclassified', 'commercial_area=sin_clasificar', 'commercial_specialty=plc_scada',
      ]) {
        const filter = api.parseCommercialFilters(new URLSearchParams(query)), values = [], where = [];
        sql.appendCommercialPredicates(filter, values, where);
        const matches = expected.flatMap((row, index) => (!filter.areas.length || filter.areas.some(value => value === row.primary_area || row.secondary_areas.includes(value)))
          && (!filter.workTypes.length || filter.workTypes.some(value => row.work_types.includes(value)))
          && (!filter.specialties.length || filter.specialties.some(value => row.specialties.includes(value)))
          && (!filter.statuses.length || filter.statuses.includes(row.support_status)) ? [index + 1] : []);
        for (const offset of [0, 1, 2]) {
          const result = await db.query(`SELECT ${source.id} AS id, count(*) OVER()::int AS total FROM ${source.from} ${source.join}
            WHERE ${where.join(' AND ')} ORDER BY ${source.id} LIMIT $${values.length + 1} OFFSET $${values.length + 2}`, [...values, 2, offset]);
          assert.deepEqual(result.rows.map(row => Number(row.id)), matches.slice(offset, offset + 2), query);
          for (const row of result.rows) assert.equal(row.total, matches.length);
        }
      }
    });
  }

  test('EXPLAIN accepts the same classification filter plans with no migration or extra persistent tables', async () => {
    for (const source of sources) {
      const values = [], where = [];
      sql.appendCommercialPredicates(api.parseCommercialFilters(new URLSearchParams('commercial_status=needs_review')), values, where);
      const result = await db.query(`EXPLAIN SELECT ${source.id} FROM ${source.from} ${source.join} WHERE ${where.join(' AND ')}`, values);
      assert.ok(result.rows.length > 0);
    }
  });

  test('historical rows without tags never execute the costly classification-validation subplans', async () => {
    for (const source of sources) {
      const result = await db.query(`EXPLAIN (ANALYZE,FORMAT JSON,TIMING FALSE) SELECT ${sql.COMMERCIAL_SELECT}
        FROM ${source.from} ${source.join} WHERE ${source.id} = 6`);
      const validationPlans = [];
      function visit(node) {
        if (node['Subplan Name']) validationPlans.push(node);
        for (const child of node.Plans || []) visit(child);
      }
      visit(result.rows[0]['QUERY PLAN'][0].Plan);
      assert.ok(validationPlans.length > 0, 'the optimizer retained the documentary validation subplans');
      assert.ok(validationPlans.every(plan => plan['Actual Loops'] === 0), `${source.name}: missing tags must short-circuit validation`);
    }
  });
});
