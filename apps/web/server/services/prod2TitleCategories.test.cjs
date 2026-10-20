const test = require('node:test');
const assert = require('node:assert/strict');
const { commercial: api, commercialSql: sql, loadCommercialModule: load } = require('./commercialClassification.test-support.cjs');
const plain = value => JSON.parse(JSON.stringify(value));
const retained = (overrides = {}) => ({ commercial_categories: ['industrial'], commercial_classification_source: 'title', commercial_classification_state: 'classified', ...overrides });

test('title navigation labels never manufacture documentary tags, citations, scores or pages', () => {
  const result = api.prod2CommercialCategoryPresentation(null, retained());
  assert.deepEqual(plain(result), retained());
  assert.deepEqual(Object.keys(result).sort(), ['commercial_categories', 'commercial_classification_source', 'commercial_classification_state']);
  for (const state of ['other', 'unclear']) assert.equal(api.prod2CommercialCategoryPresentation(null, retained({ commercial_categories: [], commercial_classification_state: state })).commercial_classification_state, state);
  for (const patch of [{ commercial_categories: ['unknown'] }, { commercial_categories: ['industrial', 'industrial'] },
    { commercial_categories: null }, { commercial_categories: [] }, { commercial_classification_source: 'legacy' },
    { commercial_classification_state: 'approved' }, { commercial_classification_state: 'other' }]) {
    assert.equal(api.prod2CommercialCategoryPresentation(null, retained(patch)).commercial_classification_state, 'unclassified');
  }
});

test('valid documentary classification takes precedence, even when none of the six categories apply', () => {
  const doc = { taxonomy_version: 'commercial_taxonomy_v1', primary_area: 'software_ti', secondary_areas: [], work_types: [], specialties: [], support_status: 'evidence_backed',
    evidence: [{ axis: 'area', value: 'software_ti', citation: 'Servicio de tecnología.', page: 1 }] };
  assert.deepEqual(plain(api.prod2CommercialCategoryPresentation(doc, retained())), {
    commercial_categories: [], commercial_classification_source: 'document', commercial_classification_state: 'other',
  });
  doc.work_types = ['licencias_suscripciones'];
  doc.evidence.push({ axis: 'work_type', value: 'licencias_suscripciones', citation: 'Licencias.', page: 2 });
  assert.deepEqual(plain(api.prod2CommercialCategoryPresentation(doc, retained())).commercial_categories, ['licencias_suscripciones']);
});

test('PROD2 title SQL pins current exact title, hashes, done receipt, policy, model, catalog and parsed result', () => {
  const join = sql.PROD2_TITLE_CATEGORY_JOIN;
  for (const pattern of [/commercial\.classification IS NULL/, /tc\.convocation_id = p\.id_convocatoria/, /tc\.source_title = p\.description/,
    /tc\.source_title_sha256 = encode\(sha256\(convert_to\(p\.description/, /receipt\.status = 'done'/, /receipt\.model = 'jev-1\.13\.0'/,
    /title_commercial_classification_v1/, /receipt\.catalog_sha256/, /receipt\.result_json->'categories' = to_jsonb\(tc\.categories\)/,
    /receipt\.result_json->>'decision' = tc\.decision/, /array_position\(tc\.categories, NULL\) IS NULL/, /count\(DISTINCT category\)/,
    /receipt\.prompt_sha256 = '641d7969735a8359de2bd53c09224fb1bfa9f916e3884f8f42968882dfd11e35'/,
    /receipt\.normalized_title = btrim\(regexp_replace\(p\.description/]) assert.match(join, pattern);
  assert.doesNotMatch(sql.PROD6_COMMERCIAL_JOIN, /title_category|prod2_title/);
  const filters = api.parseCommercialFilters(new URLSearchParams('commercial_category=industrial&commercial_work=consultoria&commercial_status=needs_review'));
  const values = [], where = [];
  sql.appendCommercialPredicates(filters, values, where, 'prod2_effective');
  assert.match(where[0], /effective_commercial\.categories && \$1::text\[\]/);
  assert.match(where[1], /commercial\.classification->'work_types'/);
  assert.match(where[2], /commercial\.classification->>'support_status'/);
  assert.doesNotMatch(where.slice(1).join(' '), /effective_commercial|title_category/);
});

test('PROD2 category filtering uses the same effective SQL before count and pagination, preserving raw classification', async () => {
  const statements = [];
  const service = load('prod2Opportunities.ts', {
    '@/server/db/client': { query: async (text, values) => {
      statements.push({ text, values });
      if (text.includes('SELECT id::text, razon_social FROM company_profiles')) return { rows: [{ id: '00000000-0000-0000-0000-000000000001', razon_social: 'DIN QA' }] };
      return { rows: text.includes('count(*)::int AS total FROM prod2_discoveries') ? [{ total: 11 }] : text.includes('FROM prod2_discoveries')
        ? [{ id_convocatoria: '1', opportunity_id: 'op1', commercial_classification: null, ...retained() }] : [] };
    } },
    '@/lib/prod2Analysis': { normalizeProd2Schedule: () => [], prod2Affinity: () => null }, './prod2DocumentStorage': {}, './prod2OfficialDocument': {},
    './keywordTextScoring': { keywordTextFitLateralSql: () => '' }, './savedOpportunities': { savedOpportunityIds: async () => new Set() },
  });
  const params = service.parseProd2ListParams(new URLSearchParams('commercial_category=industrial&q=tableros&page=2&page_size=5'));
  const result = await service.listProd2OpportunitiesForTenant('tenant', params);
  assert.equal(result.total, 11); assert.equal(result.data[0].commercial_classification.support_status, 'unclassified');
  assert.deepEqual(plain(result.data[0].commercial_categories), ['industrial']);
  assert.equal(result.data[0].commercial_classification_source, 'title');
  const selections = statements.filter(row => row.text.includes('FROM prod2_discoveries'));
  assert.equal(selections.length, 2);
  for (const row of selections) {
    assert.match(row.text, /tc\.source_title = p\.description/);
    assert.match(row.text, /effective_commercial\.categories && \$3::text\[\]/);
    assert.match(row.text, /p\.nomenclature ILIKE \$2/);
  }
  assert.deepEqual(plain(selections[1].values.slice(-2)), [5, 5]);
  assert.ok(selections[1].text.indexOf('effective_commercial.categories && $3') < selections[1].text.lastIndexOf('LIMIT $4 OFFSET $5'));
});
