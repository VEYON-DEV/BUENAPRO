const test = require('node:test');
const assert = require('node:assert/strict');
const { commercial: api, commercialSql: sql, loadCommercialModule: load } = require('./commercialClassification.test-support.cjs');
const profileId = '00000000-0000-0000-0000-000000000001';
const plain = value => JSON.parse(JSON.stringify(value));

function classified(overrides = {}) {
  const value = { taxonomy_version: 'commercial_taxonomy_v1', primary_area: 'inteligencia_artificial', secondary_areas: ['software_ti'],
    work_types: ['desarrollo_software', 'implementacion_integracion'], specialties: ['chatbot', 'agentes_ia'], support_status: 'evidence_backed', ...overrides };
  return { ...value, evidence: overrides.evidence ?? [
    { axis: 'area', value: value.primary_area, citation: 'Diseño de agentes de inteligencia artificial y chatbots.', page: 3 },
    ...value.secondary_areas.map(value => ({ axis: 'area', value, citation: 'Desarrollo e integración de software.', page: 3 })),
    ...value.work_types.map(value => ({ axis: 'work_type', value, citation: 'Desarrollo e integración de software.', page: 4 })),
    ...value.specialties.map(value => ({ axis: 'specialty', value, citation: 'Diseño de agentes de inteligencia artificial y chatbots.', page: 3 })),
  ] };
}

function service(file, rows = [], profileRows = [{ id: profileId, razon_social: 'Empresa QA' }]) {
  const statements = [];
  const query = async (text, values) => {
    statements.push({ text, values });
    if (text.includes('SELECT id::text, razon_social FROM company_profiles')) return { rows: profileRows };
    if (text.includes('count(*)::int AS total FROM prod2_discoveries')) return { rows: [{ total: 17 }] };
    if (text.includes('FROM prod2_discoveries') || text.includes('FROM seace_contracts c') || text.includes('FROM matches m\n    JOIN company_profiles')) return { rows };
    return { rows: [] };
  };
  const value = load(file, {
    '@/server/db/client': { query }, '@/lib/prod2Analysis': { normalizeProd2Schedule: () => [], prod2Affinity: () => null, buildProd2Analysis: () => ({}) },
    './prod2DocumentStorage': { prod2StorageAvailable: () => false }, './prod2OfficialDocument': {}, './keywordTextScoring': { keywordTextFitLateralSql: () => '' },
    './savedOpportunities': { savedOpportunityIds: async () => new Set() },
    '@/server/services/crud': { pagination: params => {
      const page = Number(params.get('page') || 1), pageSize = Number(params.get('page_size') || 20);
      return { page, pageSize, limit: pageSize, offset: (page - 1) * pageSize };
    } }, '@/server/services/keywordScoring': { keywordFitLateralSql: () => '' },
  });
  return { value, statements };
}

test('public catalog remains the canonical shared JSON and filter axes are explicit', () => {
  assert.deepEqual(plain(api.commercialTaxonomy), require('../../../../workers/seace/src/buenapro_worker/extraction/commercial_taxonomy.v1.json'));
  assert.deepEqual(plain(api.COMMERCIAL_FILTER_KEYS), ['commercial_category', 'commercial_area', 'commercial_work', 'commercial_specialty', 'commercial_status']);
  assert.deepEqual(plain(api.ADVANCED_COMMERCIAL_FILTER_KEYS), ['commercial_area', 'commercial_work', 'commercial_specialty', 'commercial_status']);
});

test('CSV and repeated query values are OR within an axis, trimmed and deduplicated', () => {
  const params = new URLSearchParams('commercial_area=software_ti,inteligencia_artificial&commercial_area=software_ti&commercial_work=desarrollo_software, consultoria&commercial_specialty=chatbot,agentes_ia&commercial_status=preliminary,unclassified');
  assert.deepEqual(plain(api.parseCommercialFilters(params)), {
    categories: [], areas: ['software_ti', 'inteligencia_artificial'], workTypes: ['desarrollo_software', 'consultoria'],
    specialties: ['chatbot', 'agentes_ia'], statuses: ['preliminary', 'unclassified'],
  });
  assert.deepEqual(plain(api.parseCommercialFilters(new URLSearchParams('commercial_area='))), { categories: [], areas: [], workTypes: [], specialties: [], statuses: [] });
});

test('six commercial categories use the shared deterministic rules and stable ordering', () => {
  assert.deepEqual(plain(api.commercialCategories), require('../../../../workers/seace/src/buenapro_worker/extraction/commercial_categories.v1.json'));
  assert.equal(api.commercialCategories.classification_taxonomy_version, api.commercialTaxonomy.version);
  assert.deepEqual(api.commercialCategories.categories.map(row => row.id), ['licencias_suscripciones', 'desarrollo_software', 'ia_automatizacion', 'industrial', 'cloud_infraestructura', 'soporte_mantenimiento']);
  const catalogAxes = { areas: new Set(api.commercialTaxonomy.areas.map(row => row.id)), work_types: new Set(api.commercialTaxonomy.work_types.map(row => row.id)), specialties: new Set(api.commercialTaxonomy.specialties.map(row => row.id)) };
  for (const category of api.commercialCategories.categories) {
    for (const [axis, values] of Object.entries(category.match_any)) for (const id of values) assert.ok(catalogAxes[axis].has(id));
  }
  const all = classified({ primary_area: 'software_ti', secondary_areas: ['inteligencia_artificial', 'industrial_electrica', 'infraestructura_cloud'],
    work_types: ['licencias_suscripciones', 'desarrollo_software', 'soporte_mantenimiento'], specialties: ['chatbot', 'plc_scada'] });
  assert.deepEqual(plain(api.deriveCommercialCategories(all)), api.commercialCategories.categories.map(row => row.id));
  assert.deepEqual(plain(api.deriveCommercialCategories(classified())), ['desarrollo_software', 'ia_automatizacion']);
});

test('each category can match its intended area, secondary area, work, or specialty without broad inference', () => {
  const cases = [
    [{ work_types: ['licencias_suscripciones'] }, ['licencias_suscripciones']],
    [{ work_types: ['desarrollo_software'] }, ['desarrollo_software']],
    [{ primary_area: 'inteligencia_artificial' }, ['ia_automatizacion']],
    [{ secondary_areas: ['automatizacion_procesos'] }, ['ia_automatizacion']],
    ...['chatbot', 'agentes_ia', 'rpa'].map(specialty => [{ specialties: [specialty] }, ['ia_automatizacion']]),
    [{ primary_area: 'industrial_electrica' }, ['industrial']],
    ...['plc_scada', 'instrumentacion', 'tableros_electricos', 'robotica', 'equipamiento_electromecanico'].map(specialty => [{ specialties: [specialty] }, ['industrial']]),
    [{ secondary_areas: ['infraestructura_cloud'] }, ['cloud_infraestructura']],
    [{ work_types: ['soporte_mantenimiento'] }, ['soporte_mantenimiento']],
    [{}, []],
  ];
  for (const [patch, expected] of cases) {
    const value = classified({ primary_area: 'software_ti', secondary_areas: [], work_types: ['consultoria'], specialties: [], ...patch });
    assert.deepEqual(plain(api.deriveCommercialCategories(value)), expected, JSON.stringify(patch));
  }
});

test('missing, malformed, historic or unclassified blocks never acquire a category from old summaries/titles', () => {
  for (const value of [null, {}, { title: 'Licencias y automatización industrial con IA' }, { ...classified(), taxonomy_version: 'old' },
    { ...classified(), support_status: 'invalid' }, { ...classified(), evidence: [] }, { ...classified(), primary_area: 'sin_clasificar' }]) {
    assert.deepEqual(plain(api.deriveCommercialCategories(value)), []);
  }
  const raw = classified(), before = JSON.stringify(raw);
  const downgraded = api.normalizeCommercialClassification(raw, { errors: [{ kind: 'unverified_commercial_citation' }] });
  assert.deepEqual(plain(api.deriveCommercialCategories(downgraded)), ['desarrollo_software', 'ia_automatizacion']);
  assert.equal(JSON.stringify(raw), before);
});

test('category API permits CSV OR, keeps advanced filters, and rejects unknown labels with 400', () => {
  const parsed = api.parseCommercialFilters(new URLSearchParams('commercial_category=industrial,ia_automatizacion&commercial_category=industrial&commercial_area=industrial_electrica&commercial_status=needs_review'));
  assert.deepEqual(plain(parsed.categories), ['industrial', 'ia_automatizacion']);
  assert.deepEqual(plain(parsed.areas), ['industrial_electrica']);
  assert.deepEqual(plain(api.parseCommercialFilters(new URLSearchParams('commercial_category=')).categories), []);
  for (const category of ['Todas', 'IA y automatización', 'industrial,', "industrial') OR true --", 'unknown']) {
    assert.throws(() => api.parseCommercialFilters(new URLSearchParams({ commercial_category: category })), error => error.status === 400);
  }
});

test('unknown, malformed, injection and mixed blank filter values fail with 400', () => {
  for (const [key, value] of [
    ['commercial_area', 'Software y TI'], ['commercial_area', 'software_ti,'], ['commercial_area', 'unknown'],
    ['commercial_work', "consultoria') OR true --"], ['commercial_specialty', 'llm'],
    ['commercial_status', 'approved'], ['commercial_status', 'evidence_backed,false'], ['commercial_status', 'needs_review, '],
  ]) assert.throws(() => api.parseCommercialFilters(new URLSearchParams({ [key]: value })), error => error.status === 400);
});

test('classification preserves only complete current catalog tags and cited pages without changing score', () => {
  const value = classified();
  assert.deepEqual(plain(api.normalizeCommercialClassification(value)), value);
  for (const status of ['evidence_backed', 'preliminary', 'needs_review']) {
    assert.equal(api.normalizeCommercialClassification(classified({ support_status: status })).support_status, status);
  }
  assert.equal(api.normalizeCommercialClassification({ ...value, score: 99 }).score, undefined);
});

test('missing, historical, unknown, duplicate and malformed metadata remain explicitly unclassified', () => {
  for (const value of [null, {}, [], 'industrial', { rubro: 'Industrial' }, classified({ taxonomy_version: 'commercial_taxonomy_v0' }),
    classified({ primary_area: 'future_area' }), classified({ support_status: 'approved' }), classified({ support_status: null }),
    classified({ secondary_areas: ['industrial_electrica', 'industrial_electrica'] }), classified({ secondary_areas: ['inteligencia_artificial'] }),
    classified({ secondary_areas: ['sin_clasificar'] }), classified({ work_types: ['consultoria', 'consultoria'] }),
    classified({ specialties: ['chatbot', 'chatbot'] }), { ...classified(), work_types: 'consultoria' },
    { ...classified(), secondary_areas: [null] }, classified({ primary_area: 'sin_clasificar', secondary_areas: [], work_types: [], specialties: [], support_status: 'needs_review' }),
  ]) {
    const normalized = api.normalizeCommercialClassification(value);
    assert.equal(normalized.primary_area, 'sin_clasificar');
    assert.equal(normalized.support_status, 'unclassified');
    assert.deepEqual(plain(normalized.work_types), []);
  }
});

test('each tag requires its own literal citation and positive safe integer page', () => {
  for (const evidence of [[], null, {}, [{ axis: 'area', value: 'inteligencia_artificial', citation: 'Solo una cita.', page: 2 }],
    ...[null, '3', 0, -1, 1.5, 9007199254740992, NaN, Infinity].map(page => classified().evidence.map((row, index) => index ? row : { ...row, page })),
    ...['', '  ', '\t\n'].map(citation => classified().evidence.map((row, index) => index ? row : { ...row, citation })),
    [...classified().evidence, { axis: 'specialty', value: 'robotica', citation: 'No es una etiqueta seleccionada.', page: 3 }],
    [...classified().evidence, { axis: 'unknown', value: 'software_ti', citation: 'Eje inexistente.', page: 3 }],
  ]) assert.equal(api.normalizeCommercialClassification({ ...classified(), evidence }).support_status, 'unclassified');
});

test('raw block takes precedence over summary; malformed raw never borrows valid summary tags', () => {
  const raw = classified({ primary_area: 'industrial_electrica', secondary_areas: [], work_types: ['suministro_equipos'], specialties: ['plc_scada'] });
  const summary = classified();
  assert.equal(api.commercialClassificationFromExtraction({ commercial_classification: raw }, { commercial_classification: summary }).primary_area, 'industrial_electrica');
  assert.equal(api.commercialClassificationFromExtraction({ commercial_classification: { ...raw, support_status: 'bad' } }, { commercial_classification: summary }).support_status, 'unclassified');
  for (const extraction of [null, {}, { commercial_classification: null }]) {
    assert.equal(api.commercialClassificationFromExtraction(extraction, { commercial_classification: summary }).primary_area, 'inteligencia_artificial');
  }
});

test('only explicit unverified commercial citation audit downgrades retained raw classification', () => {
  const original = classified();
  const raw = { commercial_classification: original, retrieval_evidence_audit: { errors: [{ kind: 'unverified_commercial_citation', citation_index: 0 }] } };
  const result = api.commercialClassificationFromExtraction(raw, { commercial_classification: original });
  assert.equal(result.support_status, 'needs_review');
  assert.equal(result.primary_area, original.primary_area);
  assert.deepEqual(plain(result.evidence), original.evidence);
  assert.equal(original.support_status, 'evidence_backed', 'paid raw is not mutated');
  for (const audit of [undefined, {}, { errors: [] }, { errors: [{ kind: 'unverified_facet_citation' }] }, { errors: 'malformed' }]) {
    assert.equal(api.commercialClassificationFromExtraction({ commercial_classification: original, retrieval_evidence_audit: audit, requires_human_review: true }, {}).support_status, 'evidence_backed');
  }
  assert.match(sql.PROD2_COMMERCIAL_JOIN, /unverified_commercial_citation/);
  assert.match(sql.PROD6_COMMERCIAL_JOIN, /jsonb_build_object\('support_status', 'needs_review'\)/);
});

test('SQL binds request values and combines all axes with AND, including secondary-area matches', () => {
  const filters = api.parseCommercialFilters(new URLSearchParams('commercial_area=software_ti,inteligencia_artificial&commercial_work=consultoria&commercial_specialty=chatbot&commercial_status=evidence_backed,unclassified'));
  const values = ['tenant', profileId], where = [];
  sql.appendCommercialPredicates(filters, values, where);
  assert.deepEqual(plain(values.slice(2)), [['software_ti', 'inteligencia_artificial'], ['consultoria'], ['chatbot'], ['evidence_backed', 'unclassified']]);
  assert.equal(where.length, 4);
  assert.match(where[0], /primary_area.*ANY\(\$3::text\[\]\)/);
  assert.match(where[0], /OR EXISTS.*secondary_areas/s);
  assert.match(where[1], /work_types.*\?\| \$4::text\[\]/);
  assert.match(where[2], /specialties.*\?\| \$5::text\[\]/);
  assert.match(where[3], /'unclassified'.*ANY\(\$6::text\[\]\)/);
  assert.doesNotMatch(where.join(' AND '), /inteligencia_artificial|consultoria|chatbot/);
});

test('category SQL reads only validated tags, binds category IDs, and keeps the legacy axes as AND constraints', () => {
  const filters = api.parseCommercialFilters(new URLSearchParams('commercial_category=industrial,ia_automatizacion&commercial_work=consultoria&commercial_status=needs_review'));
  const values = ['tenant'], where = [];
  sql.appendCommercialPredicates(filters, values, where);
  assert.equal(where.length, 3);
  assert.deepEqual(plain(values), ['tenant', ['industrial', 'ia_automatizacion'], ['consultoria'], ['needs_review']]);
  assert.match(where[0], /CASE WHEN commercial\.classification IS NULL THEN false ELSE EXISTS/);
  assert.match(where[0], /category\.id = ANY\(\$2::text\[\]\)/);
  assert.match(where[0], /secondary_areas.*\?\| category\.areas/);
  assert.match(where[1], /work_types.*\?\| \$3::text\[\]/);
  assert.match(where[2], /support_status.*ANY\(\$4::text\[\]\)/);
  assert.equal(sql.hasCommercialFilters({ ...api.parseCommercialFilters(new URLSearchParams()), categories: ['industrial'] }), true);
});

test('current extraction is selected before tags, excludes failed/noncurrent and never falls back to older tagged rows', () => {
  for (const join of [sql.PROD2_COMMERCIAL_JOIN, sql.PROD6_COMMERCIAL_JOIN]) {
    const source = join.slice(0, join.indexOf('cc_source ON true'));
    assert.match(source, /is_current = true AND \w+\.quality <> 'failed'/);
    assert.match(source, /ORDER BY \w+\.created_at DESC, \w+\.id DESC LIMIT 1/);
    assert.doesNotMatch(source, /commercial_classification|taxonomy_version|support_status/);
    assert.match(join, /COALESCE\(NULLIF\(cc_source\.raw_extraction_json->'commercial_classification', 'null'::jsonb\),\s+cc_source\.summary_json->'commercial_classification'\)/);
    assert.match(join, /GROUP BY tag\.value HAVING count\(\*\) > 1/);
    assert.match(join, /BETWEEN 1 AND 9007199254740991/);
    assert.match(join, /CASE WHEN jsonb_typeof\(evidence\.value->'page'\) = 'number'/);
    assert.match(join, /SELECT CASE WHEN jsonb_typeof\(cc_input\.value\) = 'object' THEN CASE\s+WHEN cc_input\.value->>'taxonomy_version'/,
      'an outer CASE short-circuits expensive raw JSON validation when historical rows have no classification');
    assert.doesNotMatch(join, /INSERT|UPDATE|DELETE|title|description|score|jev/);
  }
});

test('PROD2 commercial predicates apply identically before count/page and retain tenant-owned profile/provenance', async () => {
  const { value, statements } = service('prod2Opportunities.ts', [{ id_convocatoria: '123', opportunity_id: '1', commercial_classification: classified(), match_id: '9', match_score: 0 }]);
  const params = value.parseProd2ListParams(new URLSearchParams({ profile_id: profileId, q: 'software', page: '3', page_size: '5', analysis: 'done',
    commercial_area: 'software_ti,inteligencia_artificial', commercial_work: 'desarrollo_software', commercial_specialty: 'chatbot', commercial_status: 'evidence_backed' }));
  const result = await value.listProd2OpportunitiesForTenant('tenant-qa', params);
  assert.equal(result.total, 17); assert.equal(result.data[0].match_score, 0);
  assert.equal(result.data[0].commercial_classification.primary_area, 'inteligencia_artificial');
  assert.deepEqual(plain(statements[0].values), ['tenant-qa', profileId]);
  const selections = statements.filter(item => item.text.includes('FROM prod2_discoveries'));
  assert.equal(selections.length, 2);
  for (const selection of selections) {
    assert.match(selection.text, /m\.profile_id = \$1::uuid/);
    assert.match(selection.text, /provenance\.id_convocatoria = p\.id_convocatoria/);
    assert.match(selection.text, /COALESCE\(commercial\.classification->>'primary_area', 'sin_clasificar'\) = ANY\(\$3::text\[\]\)/);
    assert.match(selection.text, /work_types.*\?\| \$4::text\[\]/);
    assert.match(selection.text, /specialties.*\?\| \$5::text\[\]/);
    assert.match(selection.text, /'unclassified'\) = ANY\(\$6::text\[\]\)/);
  }
  assert.deepEqual(plain(selections[0].values), [profileId, '%software%', ['software_ti', 'inteligencia_artificial'], ['desarrollo_software'], ['chatbot'], ['evidence_backed']]);
  assert.deepEqual(plain(selections[1].values.slice(-2)), [5, 10]);
  assert.ok(selections[1].text.lastIndexOf('ANY($6::text[])') < selections[1].text.lastIndexOf('LIMIT $7 OFFSET $8'));
});

test('PROD2 commercial selection cannot borrow a profile belonging to another tenant', async () => {
  const { value, statements } = service('prod2Opportunities.ts', [], []);
  const params = value.parseProd2ListParams(new URLSearchParams({ profile_id: profileId, commercial_status: 'unclassified' }));
  await assert.rejects(value.listProd2OpportunitiesForTenant('foreign-tenant', params), error => error.status === 404);
  assert.equal(statements.length, 1);
  assert.match(statements[0].text, /tenant_id = \$1/);
});

test('category filtering reaches PROD2 count/page before pagination and list/detail expose the same derived IDs', async () => {
  const { value, statements } = service('prod2Opportunities.ts', [{ id_convocatoria: '123', opportunity_id: '1', commercial_classification: classified(), match_score: 0 }]);
  const params = value.parseProd2ListParams(new URLSearchParams({ profile_id: profileId, commercial_category: 'ia_automatizacion,industrial', commercial_status: 'needs_review', page: '2', page_size: '5' }));
  const result = await value.listProd2OpportunitiesForTenant('tenant-qa', params);
  const detail = await value.getProd2OpportunityForTenant('tenant-qa', '123', profileId);
  assert.deepEqual(plain(result.data[0].commercial_categories), ['desarrollo_software', 'ia_automatizacion']);
  assert.deepEqual(plain(detail.process.commercial_categories), plain(result.data[0].commercial_categories));
  const selections = statements.filter(row => row.text.includes('FROM prod2_discoveries'));
  for (const row of selections.slice(0, 2)) {
    assert.match(row.text, /effective_commercial\.categories && \$2::text\[\]/);
    assert.match(row.text, /'unclassified'\) = ANY\(\$3::text\[\]\)/);
  }
  assert.deepEqual(plain(selections[0].values), [profileId, ['ia_automatizacion', 'industrial'], ['needs_review']]);
  assert.deepEqual(plain(selections[1].values.slice(-2)), [5, 5]);
});

test('minor list and detail expose the same normalized classification independently of stored score', async () => {
  const { value, statements } = service('contracts.ts', [{ id_contrato: 123, extraction_id: '7', commercial_classification: classified(), score: 0, total_count: 17 }]);
  const result = await value.listContractsForTenant('tenant-qa', new URLSearchParams({ profile_id: profileId, commercial_area: 'software_ti',
    commercial_work: 'desarrollo_software', commercial_specialty: 'chatbot', commercial_status: 'evidence_backed', page: '2', page_size: '5' }));
  const detail = await value.getContractForTenant('tenant-qa', 123, profileId);
  assert.deepEqual(plain(result.data[0].commercial_classification), plain(detail.contract.commercial_classification));
  assert.deepEqual(plain(result.data[0].commercial_categories), ['desarrollo_software', 'ia_automatizacion']);
  assert.deepEqual(plain(detail.contract.commercial_categories), plain(result.data[0].commercial_categories));
  assert.equal(result.data[0].score, 0); assert.equal(result.meta.total, 17);
  const selected = statements.filter(item => item.text.includes('FROM seace_contracts c'));
  assert.equal(selected.length, 2);
  assert.match(selected[0].text, /sc\.tenant_id = \$1/);
  assert.match(selected[0].text, /cp\.tenant_id = \$1.*m\.id_contrato/s);
  assert.match(selected[0].text, /cp\.id = \$2::uuid/);
  assert.match(selected[0].text, /count\(\*\) OVER\(\)::int AS total_count/);
  assert.ok(selected[0].text.indexOf("COALESCE(commercial.classification->>'primary_area'") < selected[0].text.lastIndexOf('LIMIT $7'));
  assert.deepEqual(plain(selected[0].values), ['tenant-qa', profileId, ['software_ti'], ['desarrollo_software'], ['chatbot'], ['evidence_backed'], 5, 5]);
  assert.match(selected[1].text, /sc\.tenant_id = \$2/);
});

test('missing current tags in either source stay unclassified without suppressing historic stored scores', async () => {
  for (const raw of [undefined, null, { ...classified(), support_status: 'future_status' }]) {
    const p2 = service('prod2Opportunities.ts', [{ id_convocatoria: '123', opportunity_id: '1', commercial_classification: raw, match_score: 84 }]);
    const p2Result = await p2.value.listProd2OpportunitiesForTenant('tenant-qa', { object: null, q: '', page: 1, pageSize: 10, profileId: null });
    assert.equal(p2Result.data[0].commercial_classification.support_status, 'unclassified');
    assert.deepEqual(plain(p2Result.data[0].commercial_categories), []);
    assert.equal(p2Result.data[0].match_score, 84);
    const minor = service('contracts.ts', [{ id_contrato: 123, commercial_classification: raw, score: 84 }]);
    const minorResult = await minor.value.listContractsForTenant('tenant-qa', new URLSearchParams());
    assert.equal(minorResult.data[0].commercial_classification.support_status, 'unclassified');
    assert.deepEqual(plain(minorResult.data[0].commercial_categories), []);
    assert.equal(minorResult.data[0].score, 84);
  }
});

test('invalid commercial API filters reject before querying profiles or opportunities', async () => {
  const p2 = service('prod2Opportunities.ts');
  assert.throws(() => p2.value.parseProd2ListParams(new URLSearchParams('commercial_status=approved')), error => error.status === 400);
  assert.equal(p2.statements.length, 0);
  for (const file of ['contracts.ts', 'feed.ts']) {
    const tested = service(file);
    await assert.rejects(file === 'contracts.ts' ? tested.value.listContractsForTenant('tenant-qa', new URLSearchParams('commercial_specialty=LLM'))
      : tested.value.getFeed('tenant-qa', new URLSearchParams('commercial_specialty=LLM')), error => error.status === 400);
    assert.equal(tested.statements.length, 0);
  }
});

test('feed supports the same normalized fields and parameterized filters before window count/pagination', async () => {
  const { value, statements } = service('feed.ts', [{ id_contrato: 123, commercial_classification: classified(), score: 72, total_count: 3 }]);
  const result = await value.getFeed('tenant-qa', new URLSearchParams('commercial_status=evidence_backed,unclassified&commercial_area=software_ti&page=2&page_size=1'));
  assert.equal(result.data[0].commercial_classification.primary_area, 'inteligencia_artificial');
  assert.equal(result.data[0].score, 72);
  assert.deepEqual(plain(result.data[0].commercial_categories), ['desarrollo_software', 'ia_automatizacion']);
  assert.match(statements[0].text, /WHERE cp\.tenant_id = \$1/);
  assert.match(statements[0].text, /count\(\*\) OVER\(\)::int AS total_count/);
  assert.deepEqual(plain(statements[0].values), ['tenant-qa', ['software_ti'], ['evidence_backed', 'unclassified'], 1, 1]);
  assert.ok(statements[0].text.lastIndexOf('ANY($3::text[])') < statements[0].text.lastIndexOf('LIMIT $4'));
});

test('minor and feed category requests bind the same OR category selection without changing scores', async () => {
  for (const file of ['contracts.ts', 'feed.ts']) {
    const { value, statements } = service(file, [{ id_contrato: 123, commercial_classification: classified(), score: 72, total_count: 3 }]);
    const params = new URLSearchParams('commercial_category=ia_automatizacion,industrial&page_size=5&page=2');
    const result = await (file === 'contracts.ts' ? value.listContractsForTenant('tenant-qa', params) : value.getFeed('tenant-qa', params));
    assert.equal(result.data[0].score, 72);
    assert.deepEqual(plain(result.data[0].commercial_categories), ['desarrollo_software', 'ia_automatizacion']);
    const page = statements.find(row => row.text.includes('FROM seace_contracts c') || row.text.includes('FROM matches m\n    JOIN company_profiles'));
    const categoryIndex = file === 'contracts.ts' ? 3 : 2;
    assert.match(page.text, new RegExp(`category\\.id = ANY\\(\\$${categoryIndex}::text\\[\\]\\)`));
    assert.deepEqual(plain(page.values[categoryIndex - 1]), ['ia_automatizacion', 'industrial']);
    assert.deepEqual(plain(page.values.slice(-2)), [5, 5]);
    assert.ok(page.text.indexOf(`category.id = ANY($${categoryIndex}::text[])`) < page.text.lastIndexOf('LIMIT $'));
  }
});

test('feed route exposes malformed classification filters as 400 and preserves auth responses', async () => {
  const route = load('../../app/api/feed/route.ts', {
    'next/server': { NextResponse: { json: (data, init) => Response.json(data, init) } },
    '@/server/auth/tenant': { requireTenantId: async () => 'tenant-qa' },
    '@/server/services/feed': { getFeed: async () => { throw new api.CommercialFilterError('Invalid commercial_status'); } },
  });
  const response = await route.GET({ nextUrl: { searchParams: new URLSearchParams('commercial_status=bad') } });
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: 'Invalid commercial_status' });
  const unauthorized = load('../../app/api/feed/route.ts', {
    'next/server': {}, '@/server/auth/tenant': { requireTenantId: async () => { throw new Response('Unauthorized', { status: 401 }); } }, '@/server/services/feed': {},
  });
  assert.equal((await unauthorized.GET({})).status, 401);
});
