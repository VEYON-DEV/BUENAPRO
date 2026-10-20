const { describe, test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { randomUUID, createHash } = require('node:crypto');
const { commercial: api, commercialSql: sql } = require('./commercialClassification.test-support.cjs');
const dsn = process.env.BUENAPRO_COMMERCIAL_TEST_DATABASE_URL;
const plain = value => JSON.parse(JSON.stringify(value));

function classification(overrides = {}) {
  const value = { taxonomy_version: 'commercial_taxonomy_v1', primary_area: 'software_ti', secondary_areas: [], work_types: ['consultoria'],
    specialties: [], support_status: 'evidence_backed', ...overrides };
  return { ...value, evidence: [
    { axis: 'area', value: value.primary_area, citation: 'Servicio de software e inteligencia artificial; equipos industriales y cloud.', page: 2 },
    ...value.secondary_areas.map(value => ({ axis: 'area', value, citation: 'Software e inteligencia artificial, industrial y cloud.', page: 3 })),
    ...value.work_types.map(value => ({ axis: 'work_type', value, citation: 'Trabajo expresamente solicitado.', page: 4 })),
    ...value.specialties.map(value => ({ axis: 'specialty', value, citation: 'Agentes de IA, chatbots, RPA y PLC/SCADA expresamente solicitados.', page: 5 })),
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
  [extraction(classification({ primary_area: 'industrial_electrica', work_types: ['suministro_equipos'], specialties: ['plc_scada'] }))],
  [extraction(classification({ primary_area: 'infraestructura_cloud', work_types: ['licencias_suscripciones'] }))],
  [extraction(classification({ work_types: ['licencias_suscripciones', 'desarrollo_software', 'soporte_mantenimiento'] }))],
  [extraction(classification({ secondary_areas: ['automatizacion_procesos'] }))],
  [extraction(classification({ primary_area: 'otro', specialties: ['robotica'] }))],
  [extraction(classification({ secondary_areas: ['industrial_electrica', 'infraestructura_cloud'], specialties: ['chatbot', 'plc_scada'],
    work_types: ['licencias_suscripciones', 'desarrollo_software', 'soporte_mantenimiento'] }))],
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
    await db.query(`CREATE TABLE prod2_discoveries (id_convocatoria bigint PRIMARY KEY, description text);
      CREATE TABLE prod2_document_extractions (id bigint PRIMARY KEY, id_convocatoria bigint, is_current boolean, quality text, created_at timestamptz, raw_extraction_json jsonb, summary_json jsonb);
      CREATE TABLE seace_contracts (id_contrato bigint PRIMARY KEY);
      CREATE TABLE contract_documents (id bigint PRIMARY KEY, id_contrato bigint);
      CREATE TABLE tdr_extractions (id bigint PRIMARY KEY, contract_document_id bigint, is_current boolean, quality text, created_at timestamptz, raw_extraction_json jsonb, summary_json jsonb)`);
    await db.query(`CREATE TABLE prod2_title_classifications (convocation_id bigint PRIMARY KEY, source_title text, source_title_sha256 text,
      request_hash text, categories text[], decision text, policy_version text);
      CREATE TABLE prod2_title_category_receipts (request_hash text PRIMARY KEY, status text, model text, policy_version text,
        result_json jsonb, request_json jsonb, normalized_title text, title_sha256 text, catalog_sha256 text, prompt_sha256 text)`);
    for (const [index, rows] of fixtures.entries()) {
      const id = index + 1;
      await db.query('INSERT INTO prod2_discoveries VALUES($1,$2)', [id, `Título documental ${id}`]);
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
        ...api.commercialCategories.categories.map(row => `commercial_category=${row.id}`),
        'commercial_category=industrial,ia_automatizacion',
        'commercial_category=licencias_suscripciones,desarrollo_software&commercial_area=infraestructura_cloud',
        'commercial_category=ia_automatizacion&commercial_status=unclassified',
      ]) {
        const filter = api.parseCommercialFilters(new URLSearchParams(query)), values = [], where = [];
        sql.appendCommercialPredicates(filter, values, where);
        const matches = expected.flatMap((row, index) => (!filter.categories.length || filter.categories.some(value => api.deriveCommercialCategories(row).includes(value)))
          && (!filter.areas.length || filter.areas.some(value => value === row.primary_area || row.secondary_areas.includes(value)))
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

  test('category filtering short-circuits historical rows instead of executing category or evidence subplans', async () => {
    for (const source of sources) {
      const values = [], where = [];
      sql.appendCommercialPredicates(api.parseCommercialFilters(new URLSearchParams('commercial_category=ia_automatizacion,industrial')), values, where);
      const result = await db.query(`EXPLAIN (ANALYZE,FORMAT JSON,TIMING FALSE) SELECT ${source.id}
        FROM ${source.from} ${source.join} WHERE ${source.id} = 6 AND ${where.join(' AND ')}`, values);
      const plans = [];
      function visit(node) {
        if (node['Subplan Name']) plans.push(node);
        for (const child of node.Plans || []) visit(child);
      }
      visit(result.rows[0]['QUERY PLAN'][0].Plan);
      assert.ok(plans.length > 0);
      assert.ok(plans.every(plan => plan['Actual Loops'] === 0), source.name);
    }
  });

  test('PROD2 title fallback validates current identity and receipts without changing documentary filters or category pagination', async () => {
    const sha = value => createHash('sha256').update(value).digest('hex');
    const policy = 'title_commercial_classification_v1', model = 'jev-1.13.0';
    const ids = api.commercialCategories.categories.map(row => row.id);
    const catalog = sha(JSON.stringify(ids));
    const title = '  ADQUISICIÓN\u00a0DE\tTABLEROS\nELÉCTRICOS  ';
    const normalized = title.trim().split(/\s+/).join(' ');
    const cases = [
      {}, { decision: 'other', categories: [] }, { decision: 'unclear', categories: [] },
      { source_title: 'Título antiguo' }, { source_title_sha256: '0'.repeat(64) }, { policy_version: 'old' },
      { receipt: { model: 'wrong' } }, { receipt: { status: 'running' } },
      { result: { categories: ['ia_automatizacion'] } }, { categories: ['unknown'] }, { categories: ['industrial', 'industrial'] },
      { categories: [null] },
      { documentary: classification({ work_types: ['licencias_suscripciones'] }) },
      { documentary: classification() },
      { documentary: { ...classification(), taxonomy_version: 'old' } },
      { request: { model: 'wrong' } }, { receipt: { catalog_sha256: '0'.repeat(64) } },
      { receipt: { title_sha256: '0'.repeat(64) } }, { result: { decision: 'unclear' } },
      { result: { basis: 'guess' } }, { result: { preliminary: false } }, { result: { policy_version: 'old' } },
      { request: { state: { titulo: 'Otro título' } } }, { receipt: { status: 'uncertain' } },
      { categories: ['industrial', 'soporte_mantenimiento'] },
      { receipt: { normalized_title: 'Otro título', title_sha256: sha('Otro título') }, request: { state: { titulo: 'Otro título' } } },
      { receipt: { prompt_sha256: '0'.repeat(64) } },
      { result: { derivation: null } }, { result: { derivation: 'unknown' } }, { omitDerivation: true },
    ];
    const expected = cases.map((row, index) => {
      if (index === 12) return ['document', 'classified', ['licencias_suscripciones']];
      if (index === 13) return ['document', 'other', []];
      if ([0, 14, 24].includes(index)) return ['title', 'classified', row.categories || ['industrial']];
      if (index === 1 || index === 2) return ['title', row.decision, []];
      return [null, 'unclassified', []];
    });
    for (const [index, patch] of cases.entries()) {
      const id = 501 + index, requestHash = sha(String(id));
      const categories = patch.categories || ['industrial'], decision = patch.decision || 'classified';
      const receipt = { status: 'done', model, policy_version: policy, normalized_title: normalized,
        title_sha256: sha(normalized), catalog_sha256: catalog,
        prompt_sha256: '641d7969735a8359de2bd53c09224fb1bfa9f916e3884f8f42968882dfd11e35', ...patch.receipt };
      const result = { categories, decision, policy_version: policy, derivation: 'title_category_projection_v2', basis: 'title_jev', preliminary: true, ...patch.result };
      if (patch.omitDerivation) delete result.derivation;
      const request = { model, state: { titulo: normalized }, ...patch.request };
      await db.query('INSERT INTO prod2_discoveries VALUES($1,$2)', [id, title]);
      await db.query('INSERT INTO prod2_title_classifications VALUES($1,$2,$3,$4,$5,$6,$7)',
        [id, patch.source_title || title, patch.source_title_sha256 || sha(title), requestHash, categories, decision, patch.policy_version || policy]);
      await db.query('INSERT INTO prod2_title_category_receipts VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)',
        [requestHash, receipt.status, receipt.model, receipt.policy_version, result, request, receipt.normalized_title, receipt.title_sha256, receipt.catalog_sha256, receipt.prompt_sha256]);
      if (patch.documentary) await db.query('INSERT INTO prod2_document_extractions VALUES($1,$2,true,\'auto\',now(),$3,$4)',
        [id * 10, id, { commercial_classification: patch.documentary }, {}]);
    }
    const from = `FROM prod2_discoveries p ${sql.PROD2_COMMERCIAL_JOIN} ${sql.PROD2_TITLE_CATEGORY_JOIN}`;
    const rows = await db.query(`SELECT p.id_convocatoria, ${sql.PROD2_COMMERCIAL_SELECT} ${from} WHERE p.id_convocatoria >= 501 ORDER BY p.id_convocatoria`);
    for (const [index, row] of rows.rows.entries()) assert.deepEqual([
      row.commercial_classification_source, row.commercial_classification_state, row.commercial_categories,
    ], expected[index], `title fixture ${index}`);
    const filter = api.parseCommercialFilters(new URLSearchParams('commercial_category=industrial'));
    const values = [], where = [];
    sql.appendCommercialPredicates(filter, values, where, 'prod2_effective');
    const matchingIds = expected.flatMap((row, index) => row[2].includes('industrial') ? [501 + index] : []);
    for (const offset of [0, 1, 2]) {
      const result = await db.query(`SELECT p.id_convocatoria, count(*) OVER()::int AS total ${from}
        WHERE p.id_convocatoria >= 501 AND ${where.join(' AND ')} ORDER BY p.id_convocatoria LIMIT $2 OFFSET $3`, [...values, 2, offset]);
      assert.deepEqual(result.rows.map(row => Number(row.id_convocatoria)), matchingIds.slice(offset, offset + 2));
      for (const row of result.rows) assert.equal(row.total, matchingIds.length);
    }
    const advanced = [], predicates = [];
    sql.appendCommercialPredicates(api.parseCommercialFilters(new URLSearchParams('commercial_category=industrial&commercial_work=suministro_equipos')), advanced, predicates, 'prod2_effective');
    assert.equal((await db.query(`SELECT p.id_convocatoria ${from} WHERE p.id_convocatoria >= 501 AND ${predicates.join(' AND ')}`, advanced)).rowCount, 0,
      'a title category never supplies work-type evidence to advanced documentary filters');
    await db.query('DELETE FROM prod2_document_extractions WHERE id_convocatoria >= 501');
    await db.query('DELETE FROM prod2_title_classifications');
    await db.query('DELETE FROM prod2_title_category_receipts');
    await db.query('DELETE FROM prod2_discoveries WHERE id_convocatoria >= 501');
  });
});
