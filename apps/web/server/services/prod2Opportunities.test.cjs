const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const { commercial, commercialSql } = require('./commercialClassification.test-support.cjs');

function load(file, mocks = {}, environment = {}) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, { exports, URL, URLSearchParams, Response, Buffer, Date, AbortSignal, TextDecoder,
    fetch: async () => { throw new Error('Unexpected network call in offline test'); }, process: { env: environment },
    require(name) { return name in mocks ? mocks[name] : name === '@/lib/commercialTaxonomy' ? commercial
      : name === './commercialClassification' ? commercialSql : name === './prod2OfficialDocument' ? official
      : name === './seaceEgress' ? offlineEgress
      : name === './prod2CommercialAffinity' ? load('prod2CommercialAffinity.ts')
      : name === './prod2DocumentStorage' ? defaultStorage
      : name === './keywordTextScoring' ? load('keywordTextScoring.ts')
      : name === './savedOpportunities' ? load('savedOpportunities.ts', mocks) : require(name); } });
  return exports;
}
const offlineEgress = { seaceFetch: async () => { throw new Error('Unexpected SEACE request in offline test'); } };
const schedule = load('../../lib/procurementSchedule.ts');
const analysis = load('../../lib/prod2Analysis.ts', { '@/lib/procurementSchedule': schedule });
const official = load('prod2OfficialDocument.ts');
const defaultStorage = load('prod2DocumentStorage.ts');
const profileId = '00000000-0000-0000-0000-000000000001';
const documentHash = 'a'.repeat(64), profileHash = 'b'.repeat(64);
const documentCode = '00000000-0000-0000-0000-000000000010';

test('PROD2 chronology preserves date-only precision and unknown dates', () => {
  const stages = analysis.normalizeProd2Schedule([{ stage: 'Presentación de ofertas', starts_at_text: '05/10/2026', ends_at_text: '10/10/2026' },
    { stage: 'Absolución de consultas', starts_at_text: '', ends_at_text: 'No informado' }]);
  assert.equal(stages[0].endsAt, '2026-10-10');
  assert.equal(stages[0].endPrecision, 'day');
  assert.equal(stages[0].kind, 'proposals');
  assert.equal(stages[1].kind, 'answers');
  assert.equal(stages[1].endsAt, null);
  assert.equal(schedule.scheduleTemporalState(stages[0], new Date('2026-10-10T15:00:00Z')), 'unknown');
});

test('same numeric extraction ID from a different source or revision never exposes a match', () => {
  const input = { idConvocatoria: '123', profileHash, extraction: { id: '7', quality: 'auto', sha256_original: documentHash }, facets: [],
    match: { breakdown_json: { meta: { source_key: 'seace_prod2', id_convocatoria: '123', extraction_id: '7', document_sha256: documentHash, profile_hash: profileHash } } } };
  assert.equal(analysis.buildProd2Analysis(input).status, 'matched');
  for (const [key, value] of Object.entries({ source_key: 'seace_prod4', id_convocatoria: '456', extraction_id: '8', document_sha256: 'c'.repeat(64), profile_hash: 'd'.repeat(64) })) {
    const changed = structuredClone(input);
    changed.match.breakdown_json.meta[key] = value;
    assert.equal(analysis.buildProd2Analysis(changed).match, null, key);
  }
  const failed = structuredClone(input); failed.extraction.quality = 'failed';
  assert.equal(analysis.buildProd2Analysis(failed).match, null);
});

test('profile selection verifies tenant ownership before reading evaluations', async () => {
  const statements = [];
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => { statements.push({ sql, values }); return { rows: [] }; } },
    '@/lib/prod2Analysis': analysis, './prod2DocumentStorage': { prod2StorageAvailable: () => false } });
  await assert.rejects(service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId }), error => error.status === 404);
  assert.equal(statements.length, 1);
  assert.match(statements[0].sql, /tenant_id = \$1/);
  assert.deepEqual(Array.from(statements[0].values), ['tenant', profileId]);
});

test('analysis list parser accepts only an explicit done filter without changing default browsing', () => {
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => { throw new Error('Parser must not query'); } }, '@/lib/prod2Analysis': analysis });
  assert.equal(service.parseProd2ListParams(new URLSearchParams()).analysis, null);
  assert.equal(service.parseProd2ListParams(new URLSearchParams('analysis=')).analysis, null);
  assert.equal(service.parseProd2ListParams(new URLSearchParams('analysis=done')).analysis, 'done');
  for (const invalid of ['pending', 'false', 'all', 'done OR true']) {
    assert.throws(() => service.parseProd2ListParams(new URLSearchParams({ analysis: invalid })), error => error.status === 400);
  }
});

test('pagination uses convocatoria identity and preserves source provenance without legacy queries', async () => {
  const statements = [];
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values });
    return { rows: sql.includes('FROM company_profiles') ? [{ id: profileId, razon_social: 'Empresa QA' }] : sql.includes('count(*)::int AS total') ? [{ total: 21 }] : [] };
  } }, '@/lib/prod2Analysis': analysis, './prod2DocumentStorage': { prod2StorageAvailable: () => false } });
  const result = await service.listProd2OpportunitiesForTenant('tenant', { object: 'service', q: 'software', page: 2, pageSize: 10, profileId: null });
  assert.equal(result.total, 21); assert.equal(result.page, 2);
  const selection = statements.find(statement => statement.sql.includes('LIMIT $'));
  assert.deepEqual(Array.from(selection.values.slice(-2)), [10, 10]);
  assert.match(selection.sql, /'seace_prod2'::text AS source_key/);
  assert.match(selection.sql, /prod2_match_provenance/);
  assert.match(selection.sql, /document_sha256/);
  assert.match(selection.sql, /ORDER BY \(fit\.business_line_favorite AND fit\.keyword_points > 0\) DESC NULLS LAST, CASE WHEN matched\.id IS NOT NULL AND matched\.score BETWEEN 0 AND 100 THEN matched\.score ELSE NULL END DESC NULLS LAST, jev_union\.affinity_index DESC NULLS LAST, commercial_jev\.affinity_index DESC NULLS LAST, fit\.keyword_points DESC NULLS LAST/);
  assert.ok(selection.sql.indexOf('ORDER BY (fit.business_line_favorite AND fit.keyword_points > 0) DESC NULLS LAST, CASE WHEN matched.id IS NOT NULL AND matched.score BETWEEN') < selection.sql.lastIndexOf('LIMIT $'));
  assert.doesNotMatch(selection.sql, /prod4_processes|seace_contracts|worker_jobs/);
  const count = statements.find(statement => statement.sql.includes('count(*)::int AS total FROM prod2_discoveries'));
  assert.doesNotMatch(count.sql, /matched\.id IS NOT NULL|prod2_match_provenance/, 'default totals retain every enriched opportunity');
  assert.throws(() => service.parseProd2ListParams(new URLSearchParams('page_size=101')));
  assert.throws(() => service.parseProd2ListParams(new URLSearchParams('profile_id=not-a-uuid')));
});

test('deadline parser accepts open, active, closed and all, and keeps the API default unfiltered', () => {
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => { throw new Error('Parser must not query'); } }, '@/lib/prod2Analysis': analysis });
  assert.equal(service.parseProd2ListParams(new URLSearchParams()).deadline, null);
  assert.equal(service.parseProd2ListParams(new URLSearchParams('deadline=')).deadline, null);
  for (const value of ['open', 'active', 'closed', 'all']) assert.equal(service.parseProd2ListParams(new URLSearchParams({ deadline: value })).deadline, value);
  for (const invalid of ['week', 'OPEN', 'ACTIVE', 'open OR true', 'active OR true']) {
    assert.throws(() => service.parseProd2ListParams(new URLSearchParams({ deadline: invalid })), error => error.status === 400);
  }
});

test('mayores lists keep only JEV-accepted rows in every deadline view and count; "all" lists open procedures first and the history after', async () => {
  const run = async deadline => {
    const statements = [];
    const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
      statements.push({ sql, values });
      return { rows: sql.includes('FROM company_profiles') ? [{ id: profileId, razon_social: 'Empresa QA' }] : sql.includes('count(*)::int AS total') ? [{ total: 3, open_total: 2 }] : [] };
    } }, '@/lib/prod2Analysis': analysis });
    const result = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId: null, deadline });
    const count = statements.find(s => s.sql.includes('count(*)::int AS total FROM prod2_discoveries')).sql;
    const page = statements.find(s => s.sql.includes('LIMIT $')).sql;
    // The filter is what follows the visibility predicate; the page order is its last ORDER BY.
    const visible = '(d.id_convocatoria IS NOT NULL OR pf.id_convocatoria IS NOT NULL)';
    const order = page.lastIndexOf('ORDER BY');
    return { result, count, page, countFilter: count.slice(count.lastIndexOf(visible)),
      pageFilter: page.slice(page.lastIndexOf(visible), order), order: page.slice(order) };
  };
  const closed = /COALESCE\(COALESCE\(d\.proposals_closes_at, d\.registration_closes_at\) < now\(\)[\s\S]*?pf\.reader_status = 'closed_without_reading', false\)/;
  const discarded = /pf\.reader_status IS DISTINCT FROM 'rejected_below_60'/;
  const accepted = /\(jev_union\.affinity_index IS NULL OR jev_union\.affinity_index >= 60\)/;
  const open = await run('open'), onlyClosed = await run('closed'), all = await run('all'), none = await run(null);
  // Descartadas en la revisión previa and current JEV < 60 never appear, whatever the view; rows without a JEV row stay.
  for (const view of [open, onlyClosed, all, none]) {
    for (const sql of [view.countFilter, view.pageFilter]) {
      assert.match(sql, discarded);
      assert.match(sql, accepted);
    }
  }
  for (const sql of [open.countFilter, open.pageFilter]) assert.match(sql, new RegExp(`NOT ${closed.source}`));
  for (const sql of [onlyClosed.countFilter, onlyClosed.pageFilter]) {
    assert.match(sql, closed);
    assert.doesNotMatch(sql, new RegExp(`NOT ${closed.source}`));
  }
  for (const sql of [all.countFilter, all.pageFilter, none.countFilter, none.pageFilter]) assert.doesNotMatch(sql, closed);
  // Open first (by score, as before), then the closed ones, newest publication first. Hidden rows are gone, so history is just closed.
  // Las líneas prioritarias suben dentro de cada grupo, después de abiertas/cerradas y antes del puntaje de siempre.
  assert.match(all.order, new RegExp(`^ORDER BY ${closed.source}, CASE WHEN ${closed.source} THEN p\\.published_at END DESC NULLS LAST, \\(fit\\.business_line_favorite AND fit\\.keyword_points > 0\\) DESC NULLS LAST, CASE WHEN matched\\.id IS NOT NULL`));
  assert.doesNotMatch(all.page, /OR NOT pf\.reader_status IS DISTINCT FROM/);
  // Every count also says how many are open (not closed; hidden rows are already excluded), whatever the filter.
  for (const { count, result } of [open, onlyClosed, all, none]) {
    assert.match(count, new RegExp(`count\\(\\*\\) FILTER \\(WHERE NOT ${closed.source}\\)::int AS open_total`));
    assert.match(count, /LEFT JOIN LATERAL \([\s\S]*?\) jev_union ON true\s+WHERE/, 'the count reads the same JEV join as the page');
    assert.deepEqual([result.total, result.open_total], [3, 2]);
  }
  // Day-only stages compare as YYYYMMDD text in Lima; unknown dates are never closed.
  assert.match(open.pageFilter, /to_char\(now\(\) AT TIME ZONE 'America\/Lima', 'YYYYMMDD'\)/);
  assert.match(open.pageFilter, /presentaci\[oó\]n\.\*\(oferta\|propuesta\)/);
  assert.match(none.order, /^ORDER BY \(fit\.business_line_favorite AND fit\.keyword_points > 0\) DESC NULLS LAST, CASE WHEN matched\.id IS NOT NULL/);
});

test('JEV union score is the canonical row per convocatoria, any profile, latest first, and a non-number is no score', async () => {
  const statements = [];
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values });
    return { rows: sql.includes('FROM company_profiles') ? [{ id: profileId, razon_social: 'Empresa QA' }] : sql.includes('count(*)::int AS total') ? [{ total: 1 }] : [] };
  } }, '@/lib/prod2Analysis': analysis });
  await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId });
  await service.getProd2OpportunityForTenant('tenant', '123', profileId);
  const queries = statements.filter(statement => statement.sql.includes('FROM prod2_discoveries'));
  assert.equal(queries.length, 3, 'count, page and detail');
  for (const { sql } of queries) {
    const join = sql.slice(sql.indexOf('LEFT JOIN LATERAL (\n  SELECT CASE WHEN jsonb_typeof'), sql.indexOf(') jev_union ON true') + ') jev_union ON true'.length);
    assert.match(join, /^LEFT JOIN LATERAL \(\s+SELECT CASE WHEN jsonb_typeof\(ev\.result_json->'affinity_index'\) = 'number'\s+THEN \(ev\.result_json->>'affinity_index'\)::double precision END AS affinity_index\s+FROM prod2_evaluations ev/);
    assert.match(join, /WHERE ev\.id_convocatoria = p\.id_convocatoria AND ev\.kind = 'jev_affinity' AND ev\.status = 'done'\s+AND ev\.policy_version = 'prod2_commercial_union_v1'\s+ORDER BY ev\.completed_at DESC, ev\.id DESC LIMIT 1\s+\) jev_union ON true$/);
    // One row per convocatoria, the same for every tenant: neither a profile nor a parameter takes part in it.
    assert.doesNotMatch(join, /profile_id|profile_hash|tenant|\$\d/);
  }
  assert.doesNotMatch(queries[0].sql, /jev_union\.affinity_index AS jev_score/, 'the count has no row to project');
  for (const { sql } of queries.slice(1)) assert.match(sql, /jev_union\.affinity_index AS jev_score/);
  // The DIN title affinity stays a separate, tie-break-only join.
  assert.doesNotMatch(queries[1].sql.slice(queries[1].sql.indexOf('SELECT'), queries[1].sql.indexOf('FROM prod2_discoveries')), /commercial_jev/);
  assert.match(queries[1].sql, /\) jev_union ON true\s+LEFT JOIN LATERAL \(SELECT NULL::jsonb AS result_json, NULL::double precision AS affinity_index\) commercial_jev ON true/);
});

test('mayores ordering: match score, then the JEV union score, then the DIN title affinity, then keywords', async () => {
  const { service, statements } = scoreProjectionService({ publishedScore: 70 });
  await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId, deadline: 'all' });
  const page = statements.find(statement => statement.sql.includes('LIMIT $')).sql;
  const order = page.slice(page.lastIndexOf('ORDER BY'));
  const positions = ['matched.score BETWEEN 0 AND 100 THEN matched.score ELSE NULL END DESC', 'jev_union.affinity_index DESC NULLS LAST',
    'commercial_jev.affinity_index DESC NULLS LAST', 'fit.keyword_points DESC NULLS LAST', 'p.published_at DESC NULLS LAST, p.id_convocatoria DESC'].map(part => order.indexOf(part));
  assert.ok(positions.every(position => position > 0), positions.join());
  assert.deepEqual(positions, [...positions].sort((left, right) => left - right));
});

function scoreProjectionService({ storedScore = 95, scoreValidated, sourceReviewRequired = false, publishedScore, publishedVerdict = 'gris',
  suppliedProvisionalScore, matchId = '99' } = {}) {
  const statements = [];
  const meta = { source_key: 'seace_prod2', id_convocatoria: '123', extraction_id: '7', document_sha256: documentHash, profile_hash: profileHash,
    ...(scoreValidated === undefined ? {} : { score_validated: scoreValidated, raw_model_score: storedScore }) };
  const breakdown = { meta, resumen: 'Verificar respaldo compatible.', requisitos: [{ requisito: 'Experiencia compatible', estado: 'requiere_revision' }],
    administrative_checklist: [{ tipo: 'rnp', requisito: 'RNP Vigente', estado: 'preparar', score_impact: false }] };
  const row = { id_convocatoria: '123', schedule_json: [], documents_count: 0, match_id: matchId, match_profile_id: profileId,
    match_review_required: scoreValidated === false || sourceReviewRequired,
    provisional_score: suppliedProvisionalScore === undefined ? (scoreValidated === false || sourceReviewRequired ? storedScore : null) : suppliedProvisionalScore,
    match_score: publishedScore, match_verdict: publishedVerdict, breakdown_json: breakdown, missing_actions_json: [{ accion: 'Validar experiencia.' }],
    evaluated_profile_hash: profileHash, matched_at: '2026-10-13T15:00:00Z', match_updated_at: '2026-10-13T15:00:00Z' };
  const source = { id: '7', quality: 'auto', sha256_original: documentHash, summary_json: {}, raw_extraction_json: {}, created_at: '2026-10-13T14:00:00Z' };
  const facet = { id: '17', facet: 'specific_experience', label: 'Experiencia compatible', required: true, details_json: {}, evidence_json: [] };
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values });
    return { rows: sql.startsWith('SELECT id::text, razon_social FROM company_profiles') ? [{ id: profileId, razon_social: 'Empresa QA' }]
      : sql.includes('count(*)::int AS total FROM prod2_discoveries') ? [{ total: 1 }]
      : sql.includes('FROM prod2_discoveries') ? [row]
      : sql.includes('FROM prod2_document_extractions WHERE') ? [source]
      : sql.includes('FROM prod2_requirement_facets f') ? [facet] : [] };
  } }, '@/lib/prod2Analysis': analysis });
  return { service, statements, breakdown, facet };
}

test('PROD2 list and detail hide explicitly unvalidated scores including raw zero, while retaining documentary explanations', async () => {
  for (const storedScore of [0, 95]) {
    const { service, statements, breakdown, facet } = scoreProjectionService({ storedScore, scoreValidated: false, publishedScore: null });
    const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId });
    const detail = await service.getProd2OpportunityForTenant('tenant', '123', profileId);
    assert.equal(list.total, 1, 'an evaluation under review does not remove its opportunity from the feed');
    assert.equal(list.data[0].match_score, null); assert.equal(list.data[0].match_verdict, 'gris');
    assert.equal(detail.process.match_score, null); assert.equal(detail.process.match_verdict, 'gris');
    assert.equal(detail.analysis.status, 'matched');
    assert.equal(detail.analysis.match.id, '99'); assert.equal(detail.analysis.match.score, null);
    assert.equal(list.data[0].provisional_score, storedScore);
    assert.equal(detail.process.provisional_score, storedScore);
    assert.equal(detail.analysis.match.provisional_score, storedScore);
    assert.equal(detail.analysis.match.breakdown_json.meta.score_validated, false);
    assert.equal(detail.analysis.match.breakdown_json, breakdown);
    assert.equal(detail.analysis.match.missing_actions_json[0].accion, 'Validar experiencia.');
    assert.equal(detail.analysis.facets[0], facet);
    const selections = statements.filter(row => row.sql.includes('FROM prod2_discoveries') && !row.sql.includes('count(*)::int AS total FROM prod2_discoveries'));
    for (const { sql } of selections) {
      assert.match(sql, /CASE WHEN matched\.breakdown_json->'meta'->>'score_validated' = 'false' THEN NULL ELSE matched\.score END AS match_score/);
      assert.match(sql, /CASE WHEN matched\.breakdown_json->'meta'->>'score_validated' = 'false' THEN 'gris' ELSE matched\.verdict END AS match_verdict/);
      assert.doesNotMatch(sql, /COALESCE\([^\n]*matched\.score[^\n]*,\s*0\)/, 'an unpublished score is never fabricated as zero');
    }
    const facets = statements.find(row => row.sql.includes('FROM prod2_requirement_facets f'));
    assert.match(facets.sql, /f\.extraction_id = \$2::bigint/);
    assert.deepEqual(Array.from(facets.values), ['123', '7']);
    assert.doesNotMatch(statements.map(row => row.sql).join('\n'), /\b(?:INSERT|UPDATE|DELETE)\b/);
  }
});

test('provisional score is a separate range-checked stored number and cannot create a score for absent or invalid values', async () => {
  for (const value of [null, -1, 101, NaN, Infinity, '95']) {
    const { service, statements } = scoreProjectionService({ scoreValidated: false, publishedScore: null, suppliedProvisionalScore: value });
    const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId });
    const detail = await service.getProd2OpportunityForTenant('tenant', '123', profileId);
    assert.equal(list.data[0].provisional_score, null, String(value));
    assert.equal(detail.process.provisional_score, null, String(value));
    assert.equal(detail.analysis.match.provisional_score, null, String(value));
    const sql = statements.find(statement => statement.sql.includes('AS provisional_score')).sql;
    assert.match(sql, /AND matched\.score >= 0 AND matched\.score <= 100 THEN matched\.score ELSE NULL END AS provisional_score/);
    assert.doesNotMatch(sql, /COALESCE\(matched\.score|raw_model_score.*AS provisional_score/);
  }
  const { service } = scoreProjectionService({ scoreValidated: false, publishedScore: null, suppliedProvisionalScore: 90, matchId: null });
  const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId });
  const detail = await service.getProd2OpportunityForTenant('tenant', '123', profileId);
  assert.equal(list.data[0].provisional_score, null);
  assert.equal(detail.process.provisional_score, null);
  assert.equal(detail.analysis.match, null);
});

test('a provisional number never bypasses source, extraction revision or profile provenance in the documentary analysis', () => {
  const input = { idConvocatoria: '123', profileHash, extraction: { id: '7', quality: 'auto', sha256_original: documentHash }, facets: [],
    match: { score: null, provisional_score: 95, breakdown_json: { meta: { score_validated: false, source_key: 'seace_prod2', id_convocatoria: '123',
      extraction_id: '7', document_sha256: documentHash, profile_hash: profileHash } } } };
  assert.equal(analysis.buildProd2Analysis(input).match.provisional_score, 95);
  for (const [key, value] of Object.entries({ source_key: 'seace_prod4', id_convocatoria: '456', extraction_id: '8', document_sha256: 'c'.repeat(64), profile_hash: 'd'.repeat(64) })) {
    const changed = structuredClone(input); changed.match.breakdown_json.meta[key] = value;
    assert.equal(analysis.buildProd2Analysis(changed).match, null, key);
  }
  const failed = structuredClone(input); failed.extraction.quality = 'failed';
  assert.equal(analysis.buildProd2Analysis(failed).match, null);
});

test('done analysis filter uses the same current source and tenant-owned profile in count and page, even with an unpublished score', async () => {
  const { service, statements } = scoreProjectionService({ scoreValidated: false, publishedScore: null });
  const params = service.parseProd2ListParams(new URLSearchParams({ analysis: 'done', object: 'service', q: 'software', profile_id: profileId, page: '2', page_size: '1' }));
  const list = await service.listProd2OpportunitiesForTenant('tenant', params);
  assert.equal(list.total, 1);
  assert.equal(list.data[0].match_id, '99', 'an unpublished score remains an available analysis');
  assert.equal(list.data[0].match_score, null);
  assert.equal(list.data[0].match_review_required, true);
  const selections = statements.filter(statement => statement.sql.includes('FROM prod2_discoveries'));
  assert.equal(selections.length, 2);
  for (const { sql, values } of selections) {
    assert.match(sql, /matched\.id IS NOT NULL/);
    assert.match(sql, /m\.profile_id = \$1::uuid/);
    assert.match(sql, /provenance\.id_convocatoria = p\.id_convocatoria/);
    assert.match(sql, /ex\.id = provenance\.extraction_id AND ex\.is_current = true AND ex\.quality <> 'failed'/);
    assert.match(sql, /ev\.id = provenance\.evaluation_id AND ev\.status = 'done' AND ev\.kind = 'gemini_match'/);
    assert.match(sql, /document_sha256.*ex\.sha256_original/);
    assert.match(sql, /profile_hash.*provenance\.profile_hash/);
    assert.match(sql, /p\.object_type = \$2/);
    assert.match(sql, /p\.description ILIKE \$3/);
    assert.deepEqual(Array.from(values.slice(0, 3)), [profileId, 'service', '%software%']);
    assert.doesNotMatch(sql, /matched\.score (?:>\s|IS NOT NULL)|match_score IS NOT NULL/);
  }
  const page = selections[1];
  assert.ok(page.sql.indexOf('matched.id IS NOT NULL') < page.sql.lastIndexOf('LIMIT $4 OFFSET $5'));
  assert.deepEqual(Array.from(page.values), [profileId, 'service', '%software%', 1, 1]);
  assert.match(page.sql, /matched\.id::text AS match_id/);
  assert.match(page.sql, /COALESCE\(matched\.breakdown_json->'meta'->>'score_validated' = 'false', false\)/);
  assert.match(page.sql, /COALESCE\(matched\.evaluated_source_review_required, false\)/);
  assert.doesNotMatch(statements.map(statement => statement.sql).join('\n'), /\b(?:INSERT|UPDATE|DELETE)\b/);
});

test('done analysis never falls back to another profile or foreign tenant when profile resolution fails', async () => {
  for (const explicit of [null, profileId]) {
    const statements = [];
    const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
      statements.push({ sql, values });
      return { rows: sql.includes('count(*)::int AS total') ? [{ total: 0 }] : [] };
    } }, '@/lib/prod2Analysis': analysis });
    const params = { object: null, q: '', page: 1, pageSize: 20, profileId: explicit, analysis: 'done' };
    if (explicit) {
      await assert.rejects(service.listProd2OpportunitiesForTenant('foreign-tenant', params), error => error.status === 404);
      assert.equal(statements.length, 1);
    } else {
      const list = await service.listProd2OpportunitiesForTenant('empty-tenant', params);
      assert.equal(list.total, 0);
      assert.equal(list.data.length, 0);
      for (const statement of statements.slice(1)) {
        assert.equal(statement.values[0], null);
        assert.match(statement.sql, /m\.profile_id = \$1::uuid/);
        assert.match(statement.sql, /matched\.id IS NOT NULL/);
      }
    }
    assert.match(statements[0].sql, /tenant_id = \$1 AND is_active = true/);
  }
});

test('source review retains an analysis signal even when a numeric legacy score exists; validated zero remains valid', async () => {
  for (const scenario of [{ publishedScore: 70, publishedVerdict: 'ambar', sourceReviewRequired: true },
    { storedScore: 0, scoreValidated: true, publishedScore: 0, publishedVerdict: 'rojo', sourceReviewRequired: false }]) {
    const { service } = scoreProjectionService(scenario);
    const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId, analysis: 'done' });
    const detail = await service.getProd2OpportunityForTenant('tenant', '123', profileId);
    assert.equal(list.data[0].match_id, '99');
    assert.equal(detail.process.match_id, '99');
    assert.equal(list.data[0].match_review_required, scenario.sourceReviewRequired);
    assert.equal(detail.process.match_review_required, scenario.sourceReviewRequired);
    assert.equal(list.data[0].match_score, scenario.publishedScore);
  }
});

test('legacy PROD2 scores and explicitly validated zero keep their score, verdict and match', async () => {
  for (const scenario of [{ storedScore: 84, publishedScore: 84, publishedVerdict: 'ambar' },
    { storedScore: 0, scoreValidated: true, publishedScore: 0, publishedVerdict: 'rojo' }]) {
    const { service, statements } = scoreProjectionService(scenario);
    const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId });
    const detail = await service.getProd2OpportunityForTenant('tenant', '123', profileId);
    assert.equal(list.data[0].match_score, scenario.storedScore);
    assert.equal(list.data[0].match_verdict, scenario.publishedVerdict);
    assert.equal(detail.analysis.match.score, scenario.storedScore);
    assert.equal(detail.analysis.match.verdict, scenario.publishedVerdict);
    assert.equal(list.data[0].provisional_score, null);
    assert.equal(detail.process.provisional_score, null);
    assert.equal(detail.analysis.match.provisional_score, null);
    for (const { sql } of statements) assert.doesNotMatch(sql, /COALESCE\(matched\.breakdown_json->'meta'->>'score_validated', 'false'\)/);
  }
});

test('list and detail use current profile keyword fit and expose only the JEV union score, never the DIN or category title affinity', async () => {
  const statements = [];
  const row = { id_convocatoria: '123', schedule_json: [], jev_score: 72,
    keyword_points: 35, fit_points: 35, fit_level: 3, business_line_name: 'Software e integración',
    keyword_hits: [{ keyword: 'software', match: 'strong_term', points: 10 }], documents_count: 0 };
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values });
    return { rows: sql.startsWith('SELECT id::text, razon_social FROM company_profiles') ? [{ id: profileId, razon_social: 'Empresa QA' }]
      : sql.includes('count(*)::int AS total FROM prod2_discoveries') ? [{ total: 1 }]
      : sql.includes('FROM prod2_discoveries') ? [row] : [] };
  } }, '@/lib/prod2Analysis': analysis });
  const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId });
  const detail = await service.getProd2OpportunityForTenant('tenant', '123', profileId);
  for (const result of [list.data[0], detail.process]) {
    assert.equal(result.fit_points, 35); assert.equal(result.fit_level, 3);
    assert.equal(result.keyword_points, 35); assert.equal(result.fit_score, null);
    assert.equal(result.business_line_name, 'Software e integración');
    assert.equal(result.keyword_hits, row.keyword_hits); assert.equal(result.fit_basis, 'profile_keywords');
    // Solo sale el puntaje JEV de la convocatoria; ni el índice DIN de la empresa ni el histórico ni las categorías por título.
    assert.equal(result.jev_score, 72);
    for (const key of ['affinity', 'commercial_affinity', 'jev_result', 'commercial_jev_result', 'commercial_affinity_index', 'commercial_classification', 'commercial_categories']) assert.equal(key in result, false, key);
  }
  for (const query of statements.filter(s => s.sql.includes('FROM prod2_discoveries') && !s.sql.includes('count(*)::int AS total FROM prod2_discoveries'))) {
    assert.match(query.sql, /WHERE cp\.id = \$1::uuid AND cp\.is_active/);
    assert.match(query.sql, /COALESCE\(p\.description, ''\)/);
    assert.match(query.sql, /bl\.is_active/);
    assert.match(query.sql, /prod2_match_provenance/);
    assert.match(query.sql, /jev_union\.affinity_index AS jev_score/);
    assert.equal(query.values[0], profileId);
  }
  assert.doesNotMatch(statements.map(s => s.sql).join('\n'), /worker_jobs|\b(?:INSERT|UPDATE|DELETE)\b/);
  for (const { sql } of statements.filter(s => s.sql.includes('FROM prod2_discoveries'))) {
    assert.doesNotMatch(sql, /jev_result|commercial_jev_result|AS commercial_affinity_index|commercial_classification|prod2_title_classifications|effective_commercial/);
  }
});

test('JEV score is a finite 0–100 number or null; a missing, text or out-of-range value never becomes a score', async () => {
  for (const [stored, published] of [[0, 0], [60, 60], [84.5, 84.5], [100, 100], [null, null], [undefined, null], ['72', null], [NaN, null], [Infinity, null], [-1, null], [100.01, null]]) {
    const statements = [];
    const row = { id_convocatoria: '123', schedule_json: [], documents_count: 0, ...(stored === undefined ? {} : { jev_score: stored }) };
    const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async sql => {
      statements.push(sql);
      return { rows: sql.includes('count(*)::int AS total') ? [{ total: 1 }] : sql.includes('FROM prod2_discoveries') ? [row] : [] };
    } }, '@/lib/prod2Analysis': analysis });
    const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId: null });
    const detail = await service.getProd2OpportunityForTenant('tenant', '123');
    assert.equal(list.data[0].jev_score, published, String(stored));
    assert.equal(detail.process.jev_score, published, String(stored));
  }
});

test('an accepted JEV row with no reading and no evaluation reads "Por calificar"; a feed status always wins', async () => {
  for (const [row, status, label] of [
    [{ jev_score: 72 }, 'accepted_unread', 'Por calificar'],
    [{ jev_score: 59.99 }, null, null],
    [{ jev_score: null }, null, null],
    [{ jev_score: 72, participation_extraction: { id: '9' } }, null, null],
    [{ jev_score: 72, match_id: '5' }, null, null],
    [{ jev_score: 72, publication_status: 'needs_review' }, 'needs_review', 'Lectura por revisar'],
    [{ jev_score: 72, publication_status: 'unknown_status' }, null, null],
  ]) {
    const stored = { id_convocatoria: '123', schedule_json: [], documents_count: 0, ...row };
    const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async sql => ({ rows:
      sql.includes('count(*)::int AS total') ? [{ total: 1 }] : sql.includes('FROM prod2_discoveries') ? [stored] : [] }) },
      '@/lib/prod2Analysis': analysis });
    const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId: null });
    assert.equal(list.data[0].publication_status, status, JSON.stringify(row));
    assert.equal(list.data[0].publication_status_label, label, JSON.stringify(row));
  }
});

test('participation in PROD2 list and detail comes only from the current convocatoria reading', async () => {
  const statements = [];
  const reading = { id: '72', quality: 'partial', requires_human_review: true, page_scope: 'combined',
    participation: { subcontratacion: { status: 'prohibited', clause: 'No se podrá subcontratar.', page: 12, conditions: [] } } };
  const row = { id_convocatoria: '123', schedule_json: [], documents_count: 0, participation_extraction: reading };
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql) => {
    statements.push(sql);
    return { rows: sql.includes('count(*)::int AS total') ? [{ total: 1 }] : sql.includes('FROM prod2_discoveries') ? [row] : [] };
  } }, '@/lib/prod2Analysis': analysis, './prod2DocumentStorage': { prod2StorageAvailable: () => false } });
  const list = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId: null });
  const detail = await service.getProd2OpportunityForTenant('tenant', '123');
  assert.equal(list.data[0].participation_extraction, reading);
  assert.equal(detail.process.participation_extraction, reading);
  const selections = statements.filter(sql => sql.includes('AS participation_extraction'));
  assert.equal(selections.length, 2);
  for (const sql of selections) {
    assert.match(sql, /WHERE ex\.id_convocatoria = p\.id_convocatoria AND ex\.is_current/);
    assert.match(sql, /ORDER BY ex\.created_at DESC, ex\.id DESC LIMIT 1/);
    assert.match(sql, /'requires_human_review', ex\.requires_human_review/);
    assert.match(sql, /ex\.summary_json->'participation'.*ex\.raw_extraction_json->'participation'/);
    assert.match(sql, /'page_scope', 'combined'/);
    assert.doesNotMatch(sql, /subcontracting_status|consortium_status/);
    assert.doesNotMatch(sql.slice(0, sql.indexOf('FROM prod2_discoveries p')), /ex\.raw_extraction_json\s*[,)]/,
      'raw extraction may be used inside the classification join but is never projected publicly');
  }
  assert.doesNotMatch(statements.join('\n'), /\b(?:INSERT|UPDATE|DELETE)\b/);
});

test('document signatures are short-lived GETs restricted to canonical verified storage keys', () => {
  const storage = load('prod2DocumentStorage.ts', {}, { R2_ENDPOINT: 'https://qaaccount.r2.cloudflarestorage.com', R2_BUCKET: 'qa-documents', R2_ACCESS_KEY_ID: 'test-access-id', R2_SECRET_ACCESS_KEY: 'test-secret' });
  const url = new URL(storage.signedProd2DocumentUrl(`prod2/123/${documentCode}/${documentHash}.pdf`, '123', documentCode, new Date('2026-10-05T12:00:00Z')));
  assert.equal(url.searchParams.get('X-Amz-Expires'), '60');
  assert.equal(url.searchParams.get('X-Amz-Date'), '20261005T120000Z');
  assert.match(url.searchParams.get('X-Amz-Signature'), /^[0-9a-f]{64}$/);
  assert.equal(storage.signedProd2DocumentUrl(`prod2/456/${documentCode}/${documentHash}.pdf`, '123', documentCode), null);
  assert.equal(storage.signedProd2DocumentUrl('../private.pdf', '123', documentCode), null);
  assert.equal(storage.signedProd2DocumentUrl(`prod2/123/${documentCode}/${documentHash}.pdf`, '.*', documentCode), null);
  assert.equal(storage.signedProd2DocumentUrl(`prod2/123/${profileId}/${documentHash}.pdf`, '123', documentCode), null);
});

test('converted ZIP preview keeps original hash distinct and verifies convocatoria and document identity', async () => {
  const previewHash = 'c'.repeat(64), statements = [];
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push(sql); return { rows: values[0] === '123' && values[1] === documentCode
      ? [{ storage_key: `prod2/123/${documentCode}/${previewHash}.pdf`, sha256_original: documentHash }] : [] };
  } }, '@/lib/prod2Analysis': analysis, './prod2DocumentStorage': {
    validProd2PreviewKey: (key, conv, code) => key === `prod2/${conv}/${code}/${previewHash}.pdf`,
    signedProd2DocumentUrl: (key) => `https://private.example/${key}`,
  } });
  assert.equal(await service.getProd2DocumentUrl('123', documentCode), `https://private.example/prod2/123/${documentCode}/${previewHash}.pdf`);
  assert.doesNotMatch(statements[0], /format = 'pdf'/);
  assert.equal(await service.getProd2DocumentUrl('124', documentCode), null);
  assert.equal(await service.getProd2DocumentUrl('123', profileId), null);
});

function documentDetailService(events, publicationsCount = events?.length ?? 1, options = {}) {
  const statements = [];
  const officialUrl = `https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=${documentCode}`;
  const row = { id_convocatoria: '123', schedule_json: [], documents_count: 1,
    document_inventory_total: publicationsCount, document_inventory_complete: true,
    document_publication_events: events, document_publication_events_count: publicationsCount };
  const document = { codigo_alfresco: documentCode, name: 'bases.pdf', document_type: 'Bases Administrativas',
    stage_name: 'Convocatoria', published_at_text: '06/10/2026 12:00', published_at: '2026-10-06T12:00:00-05:00',
    official_download_url: officialUrl, storage_key: null, sha256_original: null, bytes: null, extension: null,
    ...(options.document ?? {}) };
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql) => {
    statements.push(sql);
    return { rows: sql.includes('FROM prod2_discoveries') ? [row] : sql.includes('FROM prod2_documents pd LEFT JOIN') ? [document] : [] };
  } }, '@/lib/prod2Analysis': analysis, './prod2DocumentStorage': {
    prod2StorageAvailable: () => false, validProd2PreviewKey: defaultStorage.validProd2PreviewKey,
    ...(options.storage ?? {}),
  } }, options.environment ?? {});
  return { service, statements, document, officialUrl };
}

test('one downloadable resource exposes both official publications without exposing raw history or storage internals', async () => {
  const first = { uuid: documentCode, filename: 'bases.pdf', stage: 'Convocatoria', type: 'Bases Administrativas',
    published_at_text: '06/10/2026 12:00', published_at: '2026-10-06T12:00:00-05:00' };
  const second = { ...first, stage: 'Integración de las Bases', type: 'Bases Integradas',
    published_at_text: '06/10/2026 12:15', published_at: '2026-10-06T12:15:00-05:00' };
  const { service, statements, officialUrl } = documentDetailService([first, second], 2, {
    environment: { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'true' },
  });
  const result = await service.getProd2OpportunityForTenant('tenant', '123');
  assert.equal(result.process.documents_count, 1);
  assert.equal(result.process.document_inventory_total, 2);
  assert.equal(result.process.document_publication_events_count, 2);
  assert.equal(result.documents.length, 1);
  assert.equal(result.documents[0].publication_events_count, 2);
  assert.deepEqual(Array.from(result.documents[0].publication_events, event => event.stage_name), ['Convocatoria', 'Integración de las Bases']);
  assert.equal(result.documents[0].publication_events[1].published_at_text, '06/10/2026 12:15');
  assert.equal(result.documents[0].official_download_url, officialUrl);
  assert.equal(result.documents[0].download_url, `/api/prod2/opportunities/123/documents/${documentCode}`); // Original access needs no R2 copy.
  assert.equal('storage_key' in result.documents[0], false);
  assert.equal('document_publication_events' in result.process, false);
  assert.match(statements.find(sql => sql.includes('FROM prod2_discoveries')), /jsonb_array_length/);
  assert.doesNotMatch(statements.join('\n'), /prod2_document_events/); // Compatible read before the event-table migration.
});

test('listed original resolves on demand without a downloaded row or R2 configuration', async () => {
  const statements = [], resolved = [];
  const source = 'https://alfprod.seace.gob.pe/alfresco/service/api/node/content/workspace/SpacesStore/file?ticket=fresh';
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values }); return { rows: [{ storage_key: null }] };
  } }, '@/lib/prod2Analysis': analysis,
    './prod2OfficialDocument': { resolveProd2OfficialDocument: async code => { resolved.push(code); return source; } },
    './prod2DocumentStorage': { validProd2PreviewKey: () => { throw new Error('R2 is not needed'); } } },
  { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'true' });
  assert.equal(await service.getProd2DocumentUrl('123', documentCode), source);
  assert.deepEqual(resolved, [documentCode]);
  assert.match(statements[0].sql, /FROM prod2_documents pd LEFT JOIN LATERAL/);
  assert.match(statements[0].sql, /pd\.is_listed/);
  assert.deepEqual(Array.from(statements[0].values), ['123', documentCode]);
  assert.doesNotMatch(statements[0].sql, /INSERT|UPDATE|DELETE/);
});

test('unknown or invalid identity does not call the official resolver and outages expose no provider details', async () => {
  let queries = 0, resolves = 0;
  const resolver = { resolveProd2OfficialDocument: async () => { resolves++; throw new Error('secret-ticket-or-upstream-body'); } };
  const empty = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => { queries++; return { rows: [] }; } },
    '@/lib/prod2Analysis': analysis, './prod2OfficialDocument': resolver }, { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'true' });
  assert.equal(await empty.getProd2DocumentUrl('123', documentCode), null);
  assert.equal(resolves, 0);
  await assert.rejects(empty.getProd2DocumentUrl('123', '../private'), error => error.status === 400);
  await assert.rejects(empty.getProd2DocumentUrl('0', documentCode), error => error.status === 400);
  assert.equal(queries, 1);
  const unavailable = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => ({ rows: [{ storage_key: null }] }) },
    '@/lib/prod2Analysis': analysis, './prod2OfficialDocument': resolver }, { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'true' });
  await assert.rejects(unavailable.getProd2DocumentUrl('123', documentCode), error => error.status === 503
    && !error.message.includes('secret-ticket-or-upstream-body'));
});

test('official original is preferred even when a historical converted PDF exists', async () => {
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => ({ rows: [{ storage_key: 'historical' }] }) },
    '@/lib/prod2Analysis': analysis,
    './prod2OfficialDocument': { resolveProd2OfficialDocument: async () => 'https://alfprod.seace.gob.pe/original.zip?ticket=fresh' },
    './prod2DocumentStorage': { validProd2PreviewKey: () => { throw new Error('No historical read needed'); } } },
  { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'true' });
  assert.equal(await service.getProd2DocumentUrl('123', documentCode), 'https://alfprod.seace.gob.pe/original.zip?ticket=fresh');
});

test('default and nonliteral flags preserve the deployed historical signing path without resolving official tickets', async () => {
  const key = `prod2/123/${documentCode}/${documentHash}.pdf`;
  for (const environment of [{}, { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'false' },
    { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'TRUE' }, { PROD2_OFFICIAL_DOCUMENTS_ENABLED: '1' }]) {
    const statements = [], signed = [];
    let resolves = 0;
    const service = load('prod2Opportunities.ts', {
      '@/server/db/client': { query: async (sql, values) => { statements.push({ sql, values }); return { rows: [{ storage_key: key, sha256_original: documentHash }] }; } },
      '@/lib/prod2Analysis': analysis,
      './prod2OfficialDocument': { resolveProd2OfficialDocument: async () => { resolves++; throw new Error('Unexpected official request'); } },
      './prod2DocumentStorage': { validProd2PreviewKey: defaultStorage.validProd2PreviewKey,
        signedProd2DocumentUrl: (storageKey, id, code) => { signed.push({ storageKey, id, code }); return 'https://private.example/historical.pdf'; } },
    }, environment);
    assert.equal(await service.getProd2DocumentUrl('123', documentCode), 'https://private.example/historical.pdf');
    assert.equal(resolves, 0);
    assert.deepEqual(signed, [{ storageKey: key, id: '123', code: documentCode }]);
    assert.match(statements[0].sql, /FROM prod2_document_downloads dl JOIN prod2_documents pd USING\(id_convocatoria, codigo_alfresco\)/);
    assert.match(statements[0].sql, /pd\.is_listed[\s\S]*dl\.storage_key IS NOT NULL ORDER BY dl\.downloaded_at DESC LIMIT 1/);
    assert.deepEqual(Array.from(statements[0].values), ['123', documentCode]);
    assert.doesNotMatch(statements[0].sql, /INSERT|UPDATE|DELETE|LEFT JOIN LATERAL/);
  }
});

test('default detail publishes downloads only for available R2 and a verified identity-bound preview key', async () => {
  const validKey = `prod2/123/${documentCode}/${documentHash}.pdf`;
  for (const [available, storageKey, expected] of [[true, validKey, true], [false, validKey, false],
    [true, null, false], [true, `prod2/999/${documentCode}/${documentHash}.pdf`, false]]) {
    const { service } = documentDetailService(null, 1, { storage: { prod2StorageAvailable: () => available }, document: { storage_key: storageKey } });
    const result = await service.getProd2OpportunityForTenant('tenant', '123');
    assert.equal(result.documents[0].download_url, expected ? `/api/prod2/opportunities/123/documents/${documentCode}` : null);
  }
});

test('default missing or invalid previews remain unavailable without an official request', async () => {
  let resolves = 0;
  for (const rows of [[], [{ storage_key: null }], [{ storage_key: `prod2/999/${documentCode}/${documentHash}.pdf` }]]) {
    const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => ({ rows }) }, '@/lib/prod2Analysis': analysis,
      './prod2OfficialDocument': { resolveProd2OfficialDocument: async () => { resolves++; throw new Error('Unexpected official request'); } } });
    assert.equal(await service.getProd2DocumentUrl('123', documentCode), null);
  }
  assert.equal(resolves, 0);
});

test('explicit official opt-in preserves a verified historical fallback on provider outage', async () => {
  const key = `prod2/123/${documentCode}/${documentHash}.pdf`;
  let resolves = 0;
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => ({ rows: [{ storage_key: key }] }) }, '@/lib/prod2Analysis': analysis,
    './prod2OfficialDocument': { resolveProd2OfficialDocument: async () => { resolves++; throw new Error('provider-unavailable'); } },
    './prod2DocumentStorage': { validProd2PreviewKey: defaultStorage.validProd2PreviewKey, signedProd2DocumentUrl: () => 'https://private.example/historical.pdf' },
  }, { PROD2_OFFICIAL_DOCUMENTS_ENABLED: 'true' });
  assert.equal(await service.getProd2DocumentUrl('123', documentCode), 'https://private.example/historical.pdf');
  assert.equal(resolves, 1);
});

test('document route returns an uncacheable redirect or a safe source outage response', async () => {
  const ServiceError = class extends Error { constructor(message, status) { super(message); this.status = status; } };
  const routeMocks = {
    'next/server': { NextResponse: { json: (data, options) => new Response(JSON.stringify(data), options),
      redirect: (url, options) => new Response(null, { status: 307, headers: { ...options.headers, Location: url } }) } },
    '@/server/auth/tenant': { requireTenantId: async () => 'tenant' },
    '@/server/services/prod2Opportunities': { Prod2RequestError: ServiceError,
      getProd2DocumentUrl: async () => 'https://alfprod.seace.gob.pe/original.pdf?ticket=fresh' },
  };
  const filename = '../../app/api/prod2/opportunities/[id]/documents/[code]/route.ts';
  const context = { params: Promise.resolve({ id: '123', code: documentCode }) };
  const response = await load(filename, routeMocks).GET(new Request('https://local.example'), context);
  assert.equal(response.status, 307);
  assert.equal(response.headers.get('Cache-Control'), 'private, no-store');
  assert.equal(response.headers.get('Referrer-Policy'), 'no-referrer');
  assert.match(response.headers.get('Location'), /alfprod\.seace\.gob\.pe/);
  routeMocks['@/server/services/prod2Opportunities'].getProd2DocumentUrl = async () => { throw new ServiceError('SEACE no disponible.', 503); };
  const unavailable = await load(filename, routeMocks).GET(new Request('https://local.example'), context);
  assert.equal(unavailable.status, 503);
  assert.equal(unavailable.headers.get('Cache-Control'), 'private, no-store');
  assert.deepEqual(await unavailable.json(), { error: 'SEACE no disponible.' });
});

test('legacy capture without publication array preserves one event per resource', async () => {
  const { service, document } = documentDetailService(null);
  const result = await service.getProd2OpportunityForTenant('tenant', '123');
  assert.equal(result.documents[0].publication_events_count, 1);
  assert.equal(result.documents[0].publication_events[0].stage_name, document.stage_name);
  assert.equal(result.documents[0].publication_events[0].published_at_text, document.published_at_text);
  assert.equal(result.process.documents_count, 1);
  assert.equal(result.process.document_publication_events_count, 1);
});

test('publication history never attaches a foreign resource or contradictory filename', async () => {
  const first = { uuid: documentCode, filename: 'bases.pdf', stage: 'Convocatoria', type: 'Bases',
    published_at_text: '06/10/2026', published_at: null };
  const { service } = documentDetailService([first, { ...first, uuid: profileId }, { ...first, filename: 'otro.pdf' },
    { ...first, published_at: 1 }, { ...first, stage: null }]);
  const result = await service.getProd2OpportunityForTenant('tenant', '123');
  assert.equal(result.documents[0].publication_events_count, 1);
  assert.equal(result.documents[0].publication_events[0].published_at, null);
});

test('all PROD2 read routes preserve a missing-session 401 response', async () => {
  for (const filename of ['../../app/api/prod2/opportunities/route.ts', '../../app/api/prod2/opportunities/[id]/route.ts', '../../app/api/prod2/opportunities/[id]/documents/[code]/route.ts']) {
    let reads = 0;
    const route = load(filename, {
      'next/server': { NextResponse: { json: (data, options) => new Response(JSON.stringify(data), options) } },
      '@/server/auth/tenant': { requireTenantId: async () => { throw new Response('Missing tenant context', { status: 401 }); } },
      '@/server/services/prod2Opportunities': {
        Prod2RequestError: class extends Error {},
        getProd2OpportunityForTenant: () => { reads++; },
        listProd2OpportunitiesForTenant: () => { reads++; },
        getProd2DocumentUrl: () => { reads++; },
      },
    });
    const result = await route.GET({ nextUrl: { searchParams: new URLSearchParams() } }, { params: Promise.resolve({ id: '123', code: documentCode }) });
    assert.equal(result.status, 401, filename);
    assert.equal(reads, 0, filename);
  }
});

test('saved state comes from the tenant bookmark of each canonical opportunity', async () => {
  const statements = [];
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values });
    if (sql.includes('FROM saved_opportunities')) return { rows: [{ opportunity_id: 'opp-2' }] };
    if (sql.includes('p.published_at DESC NULLS LAST, p.id_convocatoria DESC')) return { rows: [{ id_convocatoria: '1', opportunity_id: 'opp-1' }, { id_convocatoria: '2', opportunity_id: 'opp-2' }] };
    return { rows: sql.includes('count(*)::int AS total') ? [{ total: 2 }] : [] };
  } }, '@/lib/prod2Analysis': analysis, './prod2DocumentStorage': { prod2StorageAvailable: () => false } });
  const result = await service.listProd2OpportunitiesForTenant('tenant-a', { object: null, q: '', page: 1, pageSize: 20, profileId: null });
  assert.deepEqual(result.data.map(row => [row.id_convocatoria, row.is_saved]), [['1', false], ['2', true]]);
  const lookup = statements.find(statement => statement.sql.includes('FROM saved_opportunities'));
  assert.equal(lookup.values[0], 'tenant-a');
  assert.deepEqual(Array.from(lookup.values[1]), ['opp-1', 'opp-2']);
});

test('lists keep working before the saved_opportunities migration is applied', async () => {
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql) => {
    if (sql.includes('FROM saved_opportunities')) throw Object.assign(new Error('relation "saved_opportunities" does not exist'), { code: '42P01' });
    if (sql.includes('p.published_at DESC NULLS LAST, p.id_convocatoria DESC')) return { rows: [{ id_convocatoria: '1', opportunity_id: 'opp-1' }] };
    return { rows: sql.includes('count(*)::int AS total') ? [{ total: 1 }] : [] };
  } }, '@/lib/prod2Analysis': analysis, './prod2DocumentStorage': { prod2StorageAvailable: () => false } });
  const result = await service.listProd2OpportunitiesForTenant('tenant-a', { object: null, q: '', page: 1, pageSize: 20, profileId: null });
  assert.equal(result.data[0].is_saved, false);
});

test('PROD2 score flag is independent of the legacy saved-analysis flag', () => {
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => { throw new Error('Parser must not query'); } }, '@/lib/prod2Analysis': analysis });
  for (const query of ['', 'with_score=false']) assert.equal(service.parseProd2ListParams(new URLSearchParams(query)).withScore, false);
  assert.equal(service.parseProd2ListParams(new URLSearchParams('with_score=true')).withScore, true);
  assert.equal(service.parseProd2ListParams(new URLSearchParams('analysis=done')).analysis, 'done');
  for (const invalid of ['all', '1', 'true OR true']) assert.throws(() => service.parseProd2ListParams(new URLSearchParams({ with_score: invalid })), error => error.status === 400);
});

test('PROD2 scores precede keyword affinity globally, including zero/provisional, with numeric filtering in count and page', async () => {
  for (const score of [0, 51, 72, 100]) {
    const { service, statements } = scoreProjectionService({ storedScore: score, scoreValidated: false });
    await service.listProd2OpportunitiesForTenant('tenant', { object: 'service', q: 'software', page: 2, pageSize: 5, profileId, withScore: true });
    const selections = statements.filter(statement => statement.sql.includes('FROM prod2_discoveries'));
    assert.equal(selections.length, 2);
    for (const { sql } of selections) {
      assert.match(sql, /CASE WHEN matched\.id IS NOT NULL AND matched\.score BETWEEN 0 AND 100 THEN matched\.score ELSE NULL END IS NOT NULL/);
      assert.match(sql, /m\.profile_id = \$1::uuid/);
      assert.match(sql, /ev\.status = 'done' AND ev\.kind = 'gemini_match'/);
      assert.doesNotMatch(sql, /matched\.score > 0|COALESCE\(matched\.score, 0\)/);
    }
    const page = selections[1];
    assert.match(page.sql, /ORDER BY \(fit\.business_line_favorite AND fit\.keyword_points > 0\) DESC NULLS LAST, CASE WHEN matched\.id IS NOT NULL AND matched\.score BETWEEN 0 AND 100 THEN matched\.score ELSE NULL END DESC NULLS LAST, jev_union\.affinity_index DESC NULLS LAST, commercial_jev\.affinity_index DESC NULLS LAST, fit\.keyword_points DESC NULLS LAST/);
    assert.ok(page.sql.indexOf('ELSE NULL END IS NOT NULL') < page.sql.lastIndexOf('LIMIT $4 OFFSET $5'));
    assert.deepEqual(Array.from(page.values), [profileId, 'service', '%software%', 5, 5]);
  }
});

test('PROD2 numeric-score filter never falls back to a foreign or missing profile', async () => {
  for (const explicit of [null, profileId]) {
    const statements = [];
    const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
      statements.push({ sql, values }); return { rows: sql.includes('count(*)::int AS total') ? [{ total: 0 }] : [] };
    } }, '@/lib/prod2Analysis': analysis });
    const params = { object: null, q: '', page: 1, pageSize: 20, profileId: explicit, withScore: true };
    if (explicit) {
      await assert.rejects(service.listProd2OpportunitiesForTenant('foreign-tenant', params), error => error.status === 404);
      assert.equal(statements.length, 1);
    } else {
      const result = await service.listProd2OpportunitiesForTenant('empty-tenant', params);
      assert.equal(result.total, 0);
      const page = statements.find(statement => statement.sql.includes('LIMIT $2 OFFSET $3'));
      assert.equal(page.values[0], null);
      assert.match(page.sql, /m\.profile_id = \$1::uuid/);
    }
  }
});

// ---------------------------------------------------------------------------------------------------------------------
// Línea de negocio, palabra clave, «Activas», «Nueva» y líneas prioritarias en la lista de mayores.
// ---------------------------------------------------------------------------------------------------------------------
const lineId = '00000000-0000-0000-0000-0000000000a1';
const visibility = '(d.id_convocatoria IS NOT NULL OR pf.id_convocatoria IS NOT NULL)';

// Servicio con las consultas capturadas. Cada una se reconoce por cómo empieza: la del cálculo de palabras clave también
// contiene «FROM company_profiles», así que no sirve mirar solo eso.
function lineService({ lines = [], counts = { total: 3, open_total: 2, active_total: 1 }, rows = [], resolvedProfile = { id: profileId, razon_social: 'Empresa QA' } } = {}) {
  const statements = [];
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values });
    if (sql.startsWith('SELECT id::text, razon_social FROM company_profiles')) return { rows: resolvedProfile ? [resolvedProfile] : [] };
    if (sql.includes('FROM business_lines bl JOIN company_profiles cp')) return { rows: lines };
    if (sql.startsWith('SELECT count(*) FILTER')) return { rows: [counts] };
    if (sql.includes('LIMIT $')) return { rows };
    return { rows: [] };
  } }, '@/lib/prod2Analysis': analysis });
  const run = async (params = {}) => {
    const result = await service.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 10, profileId, ...params });
    return { result, count: statements.find(s => s.sql.startsWith('SELECT count(*) FILTER')), page: statements.find(s => s.sql.includes('LIMIT $')),
      linesQuery: statements.find(s => s.sql.includes('FROM business_lines bl JOIN company_profiles cp')) };
  };
  return { service, statements, run };
}
const parseList = query => load('prod2Opportunities.ts', { '@/server/db/client': { query: async () => { throw new Error('Parser must not query'); } },
  '@/lib/prod2Analysis': analysis }).parseProd2ListParams(new URLSearchParams(query));
const filterOf = sql => sql.slice(sql.lastIndexOf(visibility));

test('line accepts a profile line or «favorites», kw is trimmed text of up to 60 characters, and both default to no filter', () => {
  assert.deepEqual([parseList('').line, parseList('').kw], [null, '']);
  assert.deepEqual([parseList('line=').line, parseList('kw=').kw], [null, '']);
  assert.equal(parseList(`line=${lineId}`).line, lineId);
  assert.equal(parseList('line=favorites').line, 'favorites');
  assert.equal(parseList(new URLSearchParams({ line: `  ${lineId}  ` }).toString()).line, lineId);
  assert.equal(parseList(new URLSearchParams({ kw: '  Licencias de software  ' }).toString()).kw, 'Licencias de software');
  assert.equal(parseList(new URLSearchParams({ kw: 'a'.repeat(60) }).toString()).kw.length, 60);
  assert.equal(parseList(new URLSearchParams({ kw: `  ${'a'.repeat(60)}  ` }).toString()).kw.length, 60, 'the limit applies to the trimmed text');
  // They never disturb the parameters the page already had.
  const all = parseList(`line=favorites&kw=nube&deadline=active&q=x&object=service&with_score=true&profile_id=${profileId}`);
  assert.deepEqual([all.line, all.kw, all.deadline, all.q, all.object, all.withScore, all.profileId], ['favorites', 'nube', 'active', 'x', 'service', true, profileId]);
});

test('line and kw outside their contract are a 400 and never reach PostgreSQL', async () => {
  const badRequest = error => error?.status === 400;
  for (const invalid of ['not-a-uuid', 'FAVORITES', 'favorite', 'favorites OR true', `${lineId} OR true`, '1', "'; DROP TABLE business_lines;--", `${lineId}0`]) {
    assert.throws(() => parseList(new URLSearchParams({ line: invalid }).toString()), badRequest, invalid);
  }
  assert.throws(() => parseList(new URLSearchParams({ kw: 'a'.repeat(61) }).toString()), badRequest);
  // A caller that skips the parser gets the same 400 before any query runs.
  for (const params of [{ line: 'not-a-uuid' }, { kw: 'a'.repeat(61) }]) {
    const { run, statements } = lineService();
    await assert.rejects(run(params), badRequest);
    assert.equal(statements.length, 0);
  }
});

test('ACTIVE is a real detail, not closed and with a known closing date, either with time or day-only', () => {
  const { service } = lineService();
  const flat = value => value.replace(/\s+/g, ' ');
  const active = flat(service.ACTIVE);
  assert.ok(active.startsWith(`(d.id_convocatoria IS NOT NULL AND NOT ${flat(service.CLOSED)} AND (COALESCE(d.proposals_closes_at, d.registration_closes_at) IS NOT NULL OR (SELECT max(substring(`), active.slice(0, 200));
  assert.ok(active.endsWith(') IS NOT NULL))'));
  assert.match(active, /ends_at_text' ~ '\^\\d\{2\}\/\\d\{2\}\/\\d\{4\}'/, 'the day-only branch reuses the closing rule of «Cerradas»');
  assert.notEqual(service.ACTIVE, `NOT ${service.CLOSED}`, 'active is stricter than open');
});

test('deadline=active filters count and page by ACTIVE, every count reports active_total, and «open» keeps its meaning', async () => {
  const flat = value => value.replace(/\s+/g, ' ');
  for (const deadline of ['active', 'open', 'closed', 'all', null]) {
    const { run, service } = lineService({ counts: { total: 9, open_total: 5, active_total: 4 } });
    const { result, count, page } = await run({ deadline });
    assert.ok(flat(count.sql).includes(`count(*) FILTER (WHERE ${flat(service.ACTIVE)})::int AS active_total, count(*)::int AS total FROM prod2_discoveries`), deadline);
    assert.match(count.sql, /count\(\*\) FILTER \(WHERE NOT COALESCE\(/);
    assert.deepEqual([result.total, result.open_total, result.active_total], [9, 5, 4]);
    // Only the «active» view puts ACTIVE in the predicate; «open» is still just NOT closed.
    const countFilter = filterOf(count.sql);
    const pageFilter = page.sql.slice(page.sql.lastIndexOf(visibility), page.sql.lastIndexOf('ORDER BY'));
    for (const [where, filter] of [['count', countFilter], ['page', pageFilter]]) {
      assert.equal(filter.includes(service.ACTIVE), deadline === 'active', `${deadline} ${where}`);
      assert.equal(filter.includes(`AND NOT ${service.CLOSED}`) && !filter.includes(service.ACTIVE), deadline === 'open', `${deadline} ${where}: «open» is still just NOT closed`);
    }
  }
  const empty = await lineService({ counts: {} }).run({});
  assert.equal(empty.result.active_total, 0, 'a missing count is zero, never undefined');
});

test('line=<uuid> keeps rows whose best line is that line with points, in the count and the page, bound as a parameter', async () => {
  const { run } = lineService();
  const { count, page } = await run({ line: lineId });
  assert.deepEqual(Array.from(count.values), [profileId, lineId]);
  assert.deepEqual(Array.from(page.values), [profileId, lineId, 10, 0]);
  for (const sql of [count.sql, page.sql]) assert.match(sql, /\(fit\.business_line_id = \$2::uuid AND fit\.keyword_points > 0\)/);
  // The count needs the same line calculation as the page, bound to the same resolved profile.
  assert.match(count.sql, /LEFT JOIN LATERAL \(\s+WITH profile AS[\s\S]*?cp\.id = \$1::uuid AND cp\.is_active = true[\s\S]*?\) fit ON true\s+WHERE/);
  assert.match(page.sql, /LIMIT \$3 OFFSET \$4/);
  assert.doesNotMatch(count.sql + page.sql, new RegExp(lineId), 'the UUID travels only as a bound value');
});

test('line=favorites keeps rows whose best line is a priority line with points, without extra parameters', async () => {
  const { run } = lineService();
  const { count, page } = await run({ line: 'favorites' });
  assert.deepEqual(Array.from(count.values), [profileId]);
  assert.deepEqual(Array.from(page.values), [profileId, 10, 0]);
  for (const sql of [count.sql, page.sql]) assert.ok(filterOf(sql).includes(`AND (fit.business_line_favorite AND fit.keyword_points > 0)`));
  assert.match(count.sql, /\) fit ON true\s+WHERE/);
  assert.doesNotMatch(count.sql + page.sql, /favorites/, 'the literal never reaches SQL');
});

test('kw keeps rows with that word among the hits of their best line, ignoring case, and requires points', async () => {
  const { run } = lineService();
  const { count, page } = await run({ kw: 'Licencias de Software' });
  assert.deepEqual(Array.from(count.values), [profileId, 'Licencias de Software']);
  assert.deepEqual(Array.from(page.values), [profileId, 'Licencias de Software', 10, 0]);
  for (const sql of [count.sql, page.sql]) {
    assert.ok(filterOf(sql).includes(`(fit.keyword_points > 0 AND EXISTS (SELECT 1 FROM jsonb_array_elements(fit.keyword_hits) h WHERE lower(h->>'keyword') = lower($2::text)))`));
  }
  assert.doesNotMatch(count.sql + page.sql, /Licencias de Software/, 'the text travels only as a bound value');
  // A quote in the word is just text.
  const quoted = await lineService().run({ kw: "o'brien\"; --" });
  assert.deepEqual(Array.from(quoted.page.values), [profileId, "o'brien\"; --", 10, 0]);
});

test('line and kw combine with each other and with the other filters, numbering their parameters in order', async () => {
  const { run } = lineService();
  const both = await run({ line: lineId, kw: 'nube', q: 'software', object: 'service', deadline: 'active', withScore: true });
  assert.deepEqual(Array.from(both.count.values), [profileId, 'service', '%software%', lineId, 'nube']);
  assert.deepEqual(Array.from(both.page.values), [profileId, 'service', '%software%', lineId, 'nube', 10, 0]);
  for (const sql of [both.count.sql, both.page.sql]) {
    assert.match(sql, /fit\.business_line_id = \$4::uuid AND fit\.keyword_points > 0/);
    assert.match(sql, /lower\(h->>'keyword'\) = lower\(\$5::text\)/);
  }
  assert.match(both.page.sql, /LIMIT \$6 OFFSET \$7/);
  const favoritesAndWord = await lineService().run({ line: 'favorites', kw: 'nube' });
  assert.deepEqual(Array.from(favoritesAndWord.page.values), [profileId, 'nube', 10, 0]);
  assert.match(favoritesAndWord.page.sql, /lower\(h->>'keyword'\) = lower\(\$2::text\)/);
});

test('without line or kw the count does not pay for the keyword calculation and nothing about the filters leaks into the query', async () => {
  const { run } = lineService();
  const { count, page } = await run({});
  assert.doesNotMatch(count.sql, /WITH profile AS|phrase_signals|fit\./);
  assert.match(page.sql, /WITH profile AS/);
  assert.doesNotMatch(page.sql, /fit\.business_line_id = \$|jsonb_array_elements\(fit\.keyword_hits\) h WHERE/);
  assert.deepEqual(Array.from(page.values), [profileId, 10, 0]);
});

test('line filters are tenant-safe: a foreign profile is a 404 before anything else, and the selector reads the resolved profile of the tenant', async () => {
  for (const params of [{ line: lineId }, { line: 'favorites' }, { kw: 'nube' }]) {
    const { run, statements } = lineService({ resolvedProfile: null });
    await assert.rejects(run(params), error => error.status === 404);
    assert.equal(statements.length, 1, 'the profile check is the only query');
    assert.deepEqual(Array.from(statements[0].values), ['tenant', profileId]);
  }
  const { run } = lineService({ lines: [{ id: lineId, name: 'Software', is_favorite: true }] });
  const { linesQuery } = await run({ line: lineId });
  assert.deepEqual(Array.from(linesQuery.values), [profileId, 'tenant']);
  assert.match(linesQuery.sql, /WHERE cp\.id = \$1::uuid AND cp\.tenant_id = \$2::uuid AND cp\.is_active = true AND bl\.is_active = true/);
  assert.doesNotMatch(linesQuery.sql, /worker_jobs|\b(?:INSERT|UPDATE|DELETE)\b/);
});

test('the selector lists the active lines of the resolved profile, priority first and then by name', async () => {
  const stored = [{ id: lineId, name: 'Software', is_favorite: true }, { id: 'b', name: 'Redes', is_favorite: false }, { id: 'c', name: 'Cloud', is_favorite: 'nope' }];
  const { run } = lineService({ lines: stored });
  const { result, linesQuery } = await run({});
  assert.match(linesQuery.sql, /ORDER BY bl\.is_favorite DESC, lower\(bl\.nombre\), bl\.id/);
  assert.deepEqual(JSON.parse(JSON.stringify(result.lines)), [{ id: lineId, name: 'Software', is_favorite: true }, { id: 'b', name: 'Redes', is_favorite: false }, { id: 'c', name: 'Cloud', is_favorite: false }],
    'only a real true counts as a priority line');
  // Without a profile there is nothing to select and nothing to ask.
  const none = lineService({ resolvedProfile: null, lines: stored });
  const empty = await none.run({ profileId: null });
  assert.equal(JSON.stringify(empty.result.lines), '[]');
  assert.equal(empty.linesQuery, undefined);
  assert.equal(empty.result.profile, null);
});

test('priority lines rise right after the open/closed order and before the score, in every view', async () => {
  for (const deadline of ['all', 'active', 'open', 'closed', null]) {
    const { run, service } = lineService();
    const { page } = await run({ deadline });
    const order = page.sql.slice(page.sql.lastIndexOf('ORDER BY'));
    const favorite = '(fit.business_line_favorite AND fit.keyword_points > 0) DESC NULLS LAST';
    assert.equal(service.FAVORITE_FIT, '(fit.business_line_favorite AND fit.keyword_points > 0)');
    const keys = [favorite, 'matched.score BETWEEN 0 AND 100 THEN matched.score ELSE NULL END DESC', 'jev_union.affinity_index DESC NULLS LAST',
      'commercial_jev.affinity_index DESC NULLS LAST', 'fit.keyword_points DESC NULLS LAST', 'p.published_at DESC NULLS LAST, p.id_convocatoria DESC'].map(key => order.indexOf(key));
    assert.ok(keys.every(position => position > 0), `${deadline}: ${keys.join()}`);
    assert.deepEqual(keys, [...keys].sort((left, right) => left - right), deadline);
    assert.equal(order.split(favorite).length, 2, 'one favorite key');
    // In «Todas» the open/closed order and the history order come first; elsewhere the favorite key opens the ORDER BY.
    if (deadline === 'all') assert.ok(order.indexOf(service.CLOSED) < order.indexOf(favorite) && order.startsWith(`ORDER BY ${service.CLOSED}, CASE WHEN`), deadline);
    else assert.ok(order.startsWith(`ORDER BY ${favorite}`), deadline);
  }
});

test('a priority line counts only when it actually matches: ordering and filtering always ask for points', async () => {
  const { run } = lineService();
  const { page } = await run({ deadline: 'all', line: 'favorites', kw: 'nube' });
  // «business_line_favorite» alone would also be true for the priority line that gets 0 points (the lateral always returns one line).
  const afterSelect = page.sql.slice(page.sql.indexOf('FROM prod2_discoveries p'));
  const uses = afterSelect.split('fit.business_line_favorite').slice(1);
  assert.equal(uses.length, 2, 'once in the filter, once in the order');
  for (const part of uses) assert.ok(part.startsWith(' AND fit.keyword_points > 0)'), part.slice(0, 40));
});

test('list rows expose is_new, is_active, the matching line and its priority flag, and a missing value is never true', async () => {
  const matching = { id_convocatoria: '123', schedule_json: [], documents_count: 0, is_new: true, is_active: true, business_line_favorite: true,
    fit_business_line_id: lineId, business_line_name: 'Software', keyword_points: 20, fit_points: 20, fit_level: 2,
    keyword_hits: [{ keyword: 'software', match: 'strong_term', points: 10 }, { keyword: 'nube', match: 'company_keyword', points: 10 }] };
  const sparse = { id_convocatoria: '124', schedule_json: [], documents_count: 0 };
  const loose = { id_convocatoria: '125', schedule_json: [], documents_count: 0, is_new: 't', is_active: 1, business_line_favorite: 'true', fit_business_line_id: null };
  const { run } = lineService({ rows: [matching, sparse, loose] });
  const { result, page } = await run({});
  const [first, second, third] = result.data;
  assert.deepEqual([first.is_new, first.is_active, first.business_line_favorite, first.business_line_id], [true, true, true, lineId]);
  assert.deepEqual([second.is_new, second.is_active, second.business_line_favorite, second.business_line_id], [false, false, false, null]);
  assert.deepEqual([third.is_new, third.is_active, third.business_line_favorite, third.business_line_id], [false, false, false, null], 'only a real boolean true');
  assert.equal(first.keyword_hits, matching.keyword_hits);
  assert.match(page.sql, /AS is_new, [\s\S]*? AS is_active,/);
  assert.match(page.sql, /fit\.business_line_favorite,/);
  assert.match(page.sql, /\(p\.published_at AT TIME ZONE 'America\/Lima'\)::date >= \(now\(\) AT TIME ZONE 'America\/Lima'\)::date - 1\) AS is_new/);
});

test('the detail of a procedure carries the same flags as the list row', async () => {
  const statements = [];
  const row = { id_convocatoria: '123', schedule_json: [], documents_count: 0, is_new: true, is_active: false, business_line_favorite: true,
    fit_business_line_id: lineId, business_line_name: 'Software', keyword_points: 20, fit_points: 20, fit_level: 2, keyword_hits: [] };
  const service = load('prod2Opportunities.ts', { '@/server/db/client': { query: async (sql, values) => {
    statements.push({ sql, values });
    return { rows: sql.startsWith('SELECT id::text, razon_social FROM company_profiles') ? [{ id: profileId, razon_social: 'Empresa QA' }]
      : sql.includes('FROM prod2_discoveries') ? [row] : [] };
  } }, '@/lib/prod2Analysis': analysis });
  const detail = await service.getProd2OpportunityForTenant('tenant', '123', profileId);
  assert.deepEqual([detail.process.is_new, detail.process.is_active, detail.process.business_line_favorite, detail.process.business_line_id], [true, false, true, lineId]);
  const sql = statements.find(s => s.sql.includes('FROM prod2_discoveries')).sql;
  assert.match(sql, /\) AS is_new, /);
  assert.match(sql, /\) AS is_active,/);
});

// Contra PostgreSQL real, solo con DATABASE_URL (la copia local) y solo SELECT sobre valores propios: no escribe ni lee tablas.
test('PostgreSQL: ACTIVE and NEW follow the Lima calendar and the known-date rule', { skip: !process.env.DATABASE_URL }, async () => {
  const pg = require('pg');
  const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
  try {
    const { service } = lineService();
    // Each case: [name, has detail, proposals, registration, day-only schedule end (Lima day offset or null), reader status, expected active].
    const timestamp = value => value ? `(${value})::timestamptz` : 'NULL::timestamptz';
    const day = offset => offset === null ? 'NULL::jsonb'
      : `jsonb_build_array(jsonb_build_object('stage', 'Presentación de ofertas', 'ends_at_text', to_char((now() AT TIME ZONE 'America/Lima')::date + ${offset}, 'DD/MM/YYYY')))`;
    const cases = [
      ['no detail', false, null, null, null, null, false],
      ['proposals in two days', true, "now() + interval '2 days'", null, null, null, true],
      ['proposals an hour ago', true, "now() - interval '1 hour'", null, null, null, false],
      ['registration only, open', true, null, "now() + interval '3 days'", null, null, true],
      ['registration only, closed', true, null, "now() - interval '3 days'", null, null, false],
      ['detail without any date', true, null, null, null, null, false],
      ['day-only tomorrow', true, null, null, 1, null, true],
      ['day-only today', true, null, null, 0, null, true],
      ['day-only yesterday', true, null, null, -1, null, false],
      ['future proposals but closed when reviewed', true, "now() + interval '2 days'", null, null, 'closed_without_reading', false],
      ['future proposals, other reader status', true, "now() + interval '2 days'", null, null, 'needs_review', true],
    ];
    const rows = cases.map(([name, detail, proposals, registration, dayOnly, status], index) => ({ name, id: index + 1, detail, proposals, registration, dayOnly, status }));
    const sql = `SELECT p.name, ${service.ACTIVE} AS active, ${service.CLOSED} AS closed
      FROM (VALUES ${rows.map(row => `('${row.name}', ${row.id})`).join(', ')}) AS p(name, id_convocatoria)
      LEFT JOIN (VALUES ${rows.filter(row => row.detail).map(row => `(${row.id}, ${timestamp(row.proposals)}, ${timestamp(row.registration)}, ${day(row.dayOnly)})`).join(', ')})
        AS d(id_convocatoria, proposals_closes_at, registration_closes_at, schedule_json) ON d.id_convocatoria = p.id_convocatoria
      LEFT JOIN (VALUES ${rows.filter(row => row.status).map(row => `(${row.id}, '${row.status}')`).join(', ')}) AS pf(id_convocatoria, reader_status) ON pf.id_convocatoria = p.id_convocatoria
      ORDER BY p.id_convocatoria`;
    const result = await pool.query(sql);
    assert.deepEqual(result.rows.map(row => [row.name, row.active]), cases.map(([name, , , , , , active]) => [name, active]));
    for (const row of result.rows) assert.equal(row.active && row.closed, false, `${row.name}: never active and closed`);

    // «Nueva» = published today or yesterday, by Lima calendar day (not by 24 hours).
    const lima = "(now() AT TIME ZONE 'America/Lima')::date";
    const published = [
      ['now', 'now()', true],
      ['today 00:00 Lima', `(${lima})::timestamp AT TIME ZONE 'America/Lima'`, true],
      ['yesterday 23:59:59 Lima', `((${lima})::timestamp AT TIME ZONE 'America/Lima') - interval '1 second'`, true],
      ['yesterday 00:00 Lima', `((${lima} - 1)::timestamp AT TIME ZONE 'America/Lima')`, true],
      ['day before yesterday 23:59:59 Lima', `((${lima} - 1)::timestamp AT TIME ZONE 'America/Lima') - interval '1 second'`, false],
      ['a week ago', "now() - interval '7 days'", false],
    ];
    const isNew = await pool.query(`SELECT p.name, ${service.IS_NEW} AS is_new FROM (VALUES ${published.map(([name, value]) => `('${name}', ${value})`).join(', ')}) AS p(name, published_at)`);
    assert.deepEqual(isNew.rows.map(row => [row.name, row.is_new]), published.map(([name, , expected]) => [name, expected]));
  } finally {
    await pool.end();
  }
});
