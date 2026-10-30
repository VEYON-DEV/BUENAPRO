const test = require('node:test');
const assert = require('node:assert/strict');
const { loadCommercialModule: load } = require('./commercialClassification.test-support.cjs');
const plain = value => JSON.parse(JSON.stringify(value));
const profileId = '00000000-0000-0000-0000-000000000001';

function service(row, profiles = [{ id: profileId, razon_social: 'Empresa QA' }]) {
  const statements = [];
  const value = load('prod2Opportunities.ts', {
    '@/server/db/client': { query: async (text, values) => {
      statements.push({ text, values });
      if (text.includes('SELECT id::text, razon_social FROM company_profiles')) return { rows: profiles };
      if (text.includes('count(*)::int AS total FROM prod2_discoveries')) return { rows: [{ total: 1 }] };
      if (text.includes('FROM prod2_discoveries')) return { rows: row ? [row] : [] };
      return { rows: [] };
    } },
    '@/lib/prod2Analysis': { normalizeProd2Schedule: value => Array.isArray(value) ? value : [],
      buildProd2Analysis: ({ extraction, facets, match }) => ({ extraction, facets, match }) },
    './prod2DocumentStorage': { prod2StorageAvailable: () => false }, './prod2OfficialDocument': {},
    './keywordTextScoring': { keywordTextFitLateralSql: () => '' }, './savedOpportunities': { savedOpportunityIds: async () => new Set() },
  });
  return { value, statements };
}
const metadataRow = { id_convocatoria: '123', id_procedimiento: '456', opportunity_id: '789', has_official_detail: false,
  publication_status: 'rejected_below_60', detail_fetched_at: null, schedule_fetched_at: null, schedule_json: null,
  proposals_closes_at: null, registration_closes_at: null, participation_extraction: null, match_id: null, match_score: null, provisional_score: null };

test('metadata-only publication preserves its actual disposition without inventing detail, schedule, score or extraction', async () => {
  const { value } = service(metadataRow);
  const listing = await value.listProd2OpportunitiesForTenant('tenant', { object: null, q: '', page: 1, pageSize: 20, profileId });
  const detail = await value.getProd2OpportunityForTenant('tenant', '123', profileId);
  for (const row of [listing.data[0], detail.process]) {
    assert.equal(row.has_official_detail, false);
    assert.equal(row.publication_status, 'rejected_below_60');
    assert.equal(row.publication_status_label, 'Descartada en la revisión previa');
    assert.equal(row.source_presence_label, 'Datos del listado PROD2');
    assert.equal(row.detail_fetched_at, null);
    assert.equal(row.proposals_closes_at, null);
    assert.equal(row.match_score, null); assert.equal(row.provisional_score, null);
    assert.deepEqual(plain(row.official_schedule), []);
  }
  assert.deepEqual(plain(detail.documents), []);
  assert.equal(detail.analysis.extraction, null);
  assert.equal(detail.analysis.match, null);
});

test('count, list and detail require a real detail or an explicit current-hash publication, never any discovery row', async () => {
  const { value, statements } = service(metadataRow);
  await value.listProd2OpportunitiesForTenant('tenant', value.parseProd2ListParams(new URLSearchParams('q=tableros&object=good&commercial_category=industrial&page=2&page_size=5')));
  await value.getProd2OpportunityForTenant('tenant', '123', profileId);
  const selections = statements.filter(row => row.text.includes('FROM prod2_discoveries'));
  assert.equal(selections.length, 3);
  for (const row of selections) {
    assert.match(row.text, /prod2_discoveries p LEFT JOIN prod2_details d USING\(id_convocatoria\)/);
    assert.match(row.text, /LEFT JOIN prod2_feed_publications pf ON pf\.id_convocatoria = p\.id_convocatoria AND pf\.source_row_hash = p\.row_hash/);
    assert.match(row.text, /\(d\.id_convocatoria IS NOT NULL OR pf\.id_convocatoria IS NOT NULL\)/);
    assert.doesNotMatch(row.text, /WHERE[^]*pf\.accepted = true/, 'closed and previously rejected publications remain visible');
  }
  // Un commercial_category de un enlace antiguo se ignora: ni predicado ni categorías por título (JEV).
  assert.doesNotMatch(selections[0].text, /effective_commercial|prod2_title_classifications/);
  assert.deepEqual(plain(selections[1].values.slice(-2)), [5, 5]);
  assert.match(selections[2].text, /AND p\.id_convocatoria = \$2::bigint/);
});

test('obsolete commercial_* links are accepted and ignored instead of rejected', () => {
  const { value } = service(metadataRow);
  const params = value.parseProd2ListParams(new URLSearchParams('commercial_category=industrial&commercial_area=no_existe&commercial_status=x'));
  assert.equal('commercial' in params, false);
});

test('only known observed publication statuses become labels; none assert current legal availability', async () => {
  for (const [status, label] of [['closed_without_reading', 'Cerrada al revisar'], ['opening_not_confirmed', 'Apertura por confirmar'],
    ['needs_review', 'Lectura por revisar'], ['selection_needs_review', 'Documento por seleccionar'], ['llm1_complete', 'Lectura previa conservada'],
    ['accepted_unread', 'Por calificar'], ['unknown', null]]) {
    const { value } = service({ ...metadataRow, publication_status: status });
    const detail = await value.getProd2OpportunityForTenant('tenant', '123', profileId);
    assert.equal(detail.process.publication_status_label, label);
    assert.equal(detail.process.source_window_status, 'observed');
  }
});

test('publication does not bypass tenant-owned profile selection', async () => {
  const { value, statements } = service(metadataRow, []);
  await assert.rejects(value.getProd2OpportunityForTenant('foreign-tenant', '123', profileId), error => error.status === 404);
  assert.equal(statements.length, 1);
});

module.exports = { service };
