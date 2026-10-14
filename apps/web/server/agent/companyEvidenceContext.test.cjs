const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, mocks = {}) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(path.resolve(__dirname, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports, Buffer, Date, Set, Map, URL, console,
    fetch: async () => { throw new Error('Unexpected network/model call in offline test'); },
    require(name) {
      if (Object.hasOwn(mocks, name)) return mocks[name];
      throw new Error(`Unexpected dependency in offline test: ${name}`);
    },
  });
  return exports;
}
const helper = load('companyEvidenceContext.ts');
const clone = value => JSON.parse(JSON.stringify(value));
const fromRow = (value, columns) => Array.isArray(value)
  ? Object.fromEntries(columns.map((name, index) => [name, value[index]])) : value;
function documentaryView(projection, document) {
  const columns = projection.contextRepresentation.rowColumns;
  const evidence = fromRow(document.evidence, columns.documentEvidence);
  return {
    ...projection.contextRepresentation.documentDefaults, ...document, ...evidence,
    amounts: evidence.amounts.map(item => fromRow(item, columns.amounts)),
    classification: fromRow(evidence.classification, columns.classification),
  };
}
function fixture(count = 43) {
  const documents = Array.from({ length: count }, (_, index) => {
    const id = `00000000-0000-0000-0000-${String(index + 1).padStart(12, '0')}`;
    const sha256 = (index + 1).toString(16).padStart(64, '0');
    const quote = `Objeto ${index + 1}: suministro y configuración del equipo.\nImporte S/ 1,180.00 incluido IGV. ${'Detalle íntegro '.repeat(40)}`;
    const extraction = {
      summary: `Documento ${index + 1}: suministro industrial y conformidad identificada pendiente de revisión.`,
      document_types: ['orden_de_compra', 'factura'],
      facts: [
        { field: 'Cliente', value: `Cliente ${index + 1}`, quote: `Cliente ${index + 1}`, page: 1 },
        { field: 'Objeto', value: `Suministro industrial ${index + 1}`, quote, page: 2 },
        { field: 'Proveedor', value: 'Empresa sintética', quote: 'Empresa sintética', page: 1 },
      ],
      amounts: [
        { amount_decimal: '1180.00', literal: 'S/ 1,180.00', currency: 'PEN', label: 'Total orden', tax_basis: 'includes_tax', source_type: 'orden_de_compra', quote, page: 2 },
        { amount_decimal: '1000.00', literal: 'S/ 1,000.00', currency: 'PEN', label: 'Base imponible', tax_basis: 'excludes_tax', source_type: 'factura', quote: 'Base imponible S/ 1,000.00', page: 3 },
        { amount_decimal: '180.00', literal: 'S/ 180.00', currency: 'PEN', label: 'IGV', tax_basis: 'tax', source_type: 'factura', quote: 'IGV S/ 180.00', page: 3 },
      ],
      principal_amount_index: index === 2 ? null : 0,
      classification: { rubro: 'Automatización', specialty: 'Variadores', quote, page: 2, inferred: true },
      conformity: 'explicit_documentary_evidence', unknowns: ['Validación de la ejecución real'],
      warnings: ['La factura y la orden son el mismo servicio; no sumar ambas.'],
    };
    return {
      sourceId: `company-document-${id}`, title: `Evidencia ${index + 1}`, filename: `${index + 1}-respaldo.pdf`,
      documentType: 'experiencia', tags: ['industrial'], validUntil: index === 0 ? '2027-01-01' : null,
      metadata: {
        documentary_validation: { version: 'company_document_reading_v1', status: 'pending_human_confirmation', sha256, extraction },
        documentary_extraction: {
          version: 'company_evidence_v3', status: 'structured_not_verified', sha256, page_count: 3,
          model: 'paid-original', response: { secretBody: 'RAW_RESPONSE_SENTINEL' }, cost_usd: 4,
          extraction: { ...extraction, summary: 'STALE_SUMMARY_SENTINEL', amounts: [{ amount_decimal: '40000.00' }] },
        },
        document_text: { full_text: 'FULL_TEXT_SENTINEL'.repeat(3000), pages: [{ text: 'PRIVATE_PAGE_SENTINEL' }] },
        economic_evidence: { amounts: extraction.amounts, selected_amount: extraction.amounts[0], classification: extraction.classification },
        rights: { public: false, usage: 'internal', expires: '2027-01-01' },
      },
    };
  });
  const experience = documents.map((document, index) => ({
    entidad: `DECLARED_CLIENT_${index}`, objeto: `DECLARED_OBJECT_${index}`, monto: 1180, moneda: 'PEN',
    rubro: 'DECLARED_RUBRO', especialidad: 'DECLARED_SPECIALTY', tipo_objeto: 'bienes',
    actividades: 'ACTIVITY_SENTINEL'.repeat(1000), alcance_participacion: 'PARTICIPATION_SENTINEL',
    documentary_summary: {
      version: 'company_document_reading_v1', status: 'pending_human_confirmation', amount_review_required: index === 2,
      missing_document_ids: index === 1 ? ['archived-id'] : [], archived_missing_references: [],
      original_fields: { description: 'AUDIT_SENTINEL' },
      sources: [{ document_id: document.sourceId.slice('company-document-'.length), sha256: document.metadata.documentary_validation.sha256, extraction: document.metadata.documentary_validation.extraction }],
    },
  }));
  const profile = {
    razonSocial: 'Empresa sintética', experience,
    economicExperience: {
      bienes: 75000, servicios: 5000,
      areas: experience.map((item, index) => ({ ...item, monto: index === 3 ? 999 : item.monto, source_record_index: index, status: 'pending_human_confirmation', no_global_sum: true })),
      documentary_provenance: { version: 'company_document_reading_v1', status: 'pending_human_confirmation', currency_conversion: false, global_totals_unchanged: true, archived_missing_references: [] },
    },
  };
  return { profile, library: { documents } };
}
function assertAllCanonical(projection, library) {
  assert.equal(projection.companyLibrary.documents.length, library.documents.length);
  const seen = new Set();
  for (const [index, source] of library.documents.entries()) {
    const projected = projection.companyLibrary.documents[index];
    const view = documentaryView(projection, projected);
    const canonical = source.metadata.documentary_validation.extraction;
    assert.equal(projected.sourceId, source.sourceId);
    assert.equal(projected.filename, source.filename);
    assert.equal(view.sha256, source.metadata.documentary_validation.sha256);
    assert.equal(view.documentSummary, canonical.summary);
    assert.deepEqual(clone(view.amounts), canonical.amounts);
    assert.deepEqual(clone(view.classification), canonical.classification);
    assert.equal(view.principalAmountIndex, canonical.principal_amount_index);
    assert.equal(view.conformity, canonical.conformity);
    assert.deepEqual(clone(view.warnings), canonical.warnings);
    assert.deepEqual(clone(view.unknowns), canonical.unknowns);
    seen.add(projected.sourceId);
  }
  assert.equal(seen.size, library.documents.length);
}

test('43 corrected summaries and all monetary/citation values remain complete, tenant sources bind index zero, originals remain unchanged', () => {
  const input = fixture(), before = JSON.stringify(input);
  const projection = helper.projectCompanyEvidenceContext(input.profile, input.library);
  assertAllCanonical(projection, input.library);
  const columns = projection.contextRepresentation.rowColumns.experience;
  const experience = projection.companyProfile.experience.map(item => fromRow(item, columns));
  assert.equal(experience.length, 43);
  assert.deepEqual(clone(experience[0].documents), [0]);
  assert.deepEqual(clone(experience[42].documents), [42]);
  assert.equal(experience[42].recordIndex, 42);
  assert.equal(experience[2].amountReviewRequired, true);
  assert.deepEqual(clone(experience[1].missingDocumentIds), ['archived-id']);
  const first = documentaryView(projection, projection.companyLibrary.documents[0]);
  assert.deepEqual(clone(first.client), ['Cliente 1']);
  assert.deepEqual(clone(first.object), ['Suministro industrial 1']);
  assert.deepEqual(clone(first.metadata.rights), input.library.documents[0].metadata.rights);
  assert.equal(first.validUntil, '2027-01-01');
  assert.equal(first.documentaryStatus, 'pending_human_confirmation');
  assert.equal(projection.companyProfile.economicExperience.bienes, 75000);
  assert.equal(projection.companyProfile.economicExperience.servicios, 5000);
  assert.deepEqual(clone(projection.companyProfile.economicExperience.areas[3]), { experienceIndex: 3, monto: 999 });
  const text = JSON.stringify(projection);
  for (const marker of ['FULL_TEXT_SENTINEL', 'RAW_RESPONSE_SENTINEL', 'STALE_SUMMARY_SENTINEL', 'ACTIVITY_SENTINEL', 'PARTICIPATION_SENTINEL', 'AUDIT_SENTINEL', 'DECLARED_CLIENT_', 'DECLARED_OBJECT_']) assert.equal(text.includes(marker), false, marker);
  assert.equal(JSON.stringify(input), before);
});

test('unbound/legacy metadata and independently different monetary/area fields are preserved instead of guessed', () => {
  const { profile, library } = fixture(2);
  profile.experience[0].documentary_summary.sources[0].sha256 = 'mismatched';
  profile.experience[1] = { legacy_description: 'Legacy human experience without canonical binding', monto: 321, moneda: 'USD' };
  library.documents[1].metadata = { document_text: { full_text: 'PRIVATE_TEXT' }, permissions: { redistributable: false }, unsupported_extraction: { details: 'Retained unknown shape' } };
  const projected = helper.projectCompanyEvidenceContext(profile, library);
  assert.deepEqual(clone(projected.companyProfile.experience), profile.experience);
  assert.equal(projected.companyLibrary.documents[1].metadata.document_text, undefined);
  assert.deepEqual(clone(projected.companyLibrary.documents[1].metadata.permissions), { redistributable: false });
  assert.deepEqual(clone(projected.companyLibrary.documents[1].metadata.unsupported_extraction), { details: 'Retained unknown shape' });
});

test('unknown amount keys and explicit status/version overrides remain named and complete', () => {
  const { profile, library } = fixture(1);
  library.documents[0].metadata.documentary_validation.status = 'needs_review';
  library.documents[0].metadata.documentary_validation.extraction.amounts[0].extra = { uncertainty: 'Evidence preserved' };
  const projected = helper.projectCompanyEvidenceContext(profile, library);
  const view = documentaryView(projected, projected.companyLibrary.documents[0]);
  assert.equal(view.documentaryStatus, 'needs_review');
  assert.deepEqual(clone(view.amounts[0]), library.documents[0].metadata.documentary_validation.extraction.amounts[0]);
});

test('document references follow the actual library order rather than a profile position or filename', () => {
  const { profile, library } = fixture(3);
  library.documents.reverse();
  const projected = helper.projectCompanyEvidenceContext(profile, library);
  const columns = projected.contextRepresentation.rowColumns.experience;
  const experiences = projected.companyProfile.experience.map(value => fromRow(value, columns));
  assert.deepEqual(clone(experiences.map(value => value.documents)), [[2], [1], [0]]);
  for (const [index, experience] of experiences.entries()) {
    const source = profile.experience[index].documentary_summary.sources[0].document_id;
    assert.equal(projected.companyLibrary.documents[experience.documents[0]].sourceId, `company-document-${source}`);
  }
});

test('the budget serializer emits complete parseable JSON at the boundary and rejects excess without slicing', () => {
  const value = { evidence: 'complete citation \"quoted\"\n'.repeat(100) };
  const expected = JSON.stringify(value);
  assert.equal(helper.serializeAgentContext(value, expected.length), expected);
  assert.deepEqual(JSON.parse(helper.serializeAgentContext(value, expected.length)), value);
  assert.throws(() => helper.serializeAgentContext(value, expected.length - 1), /No se envió ni se cortó la evidencia/);
  assert.throws(() => helper.serializeAgentContext({ text: 'x'.repeat(128001) }, 128000), /128000 caracteres/);
});

function agentWithSources(profile, library, contractSize = 14000) {
  let queries = 0;
  const query = async sql => {
    assert.match(sql, /^\s*SELECT/i);
    queries++;
    if (sql.includes('FROM seace_contracts c')) return { rows: [{
      id_contrato: '1', application_id: null, match_id: null,
      contract: { codigo: 'OFFLINE-1', descripcion: 'Suministro industrial', additionalContractEvidence: 'Contrato sintético para reservar presupuesto. '.repeat(Math.ceil(contractSize / 44)) },
      profile, business_lines: [], application: {}, items: [], requirements: [], quote_documents: [], attachments: [],
    }] };
    if (sql.includes('FROM contract_documents') || sql.includes('FROM requirement_facets')) return { rows: [] };
    throw new Error('Unexpected offline query');
  };
  const agent = load('licitationAgent.ts', {
    '@/server/db/client': { query },
    '@/server/services/companyLibrary': { agentCompanyLibraryContext: async () => library },
    '@/server/env': { ensureServerEnv: () => { throw new Error('Paid client configuration must never execute'); } },
    './documentDelivery': { documentDeliveryInstruction: () => '' },
    './companyEvidenceContext': helper,
  });
  return { agent, count: () => queries };
}

test('prepare runs offline with 43 complete sources and contract headroom; oversized context rejects before inference', async () => {
  const { profile, library } = fixture();
  const { agent, count } = agentWithSources(profile, library);
  const result = await agent.prepareLicitationAgentRun({ tenantId: 'synthetic-tenant', idContrato: 1, userMessage: 'Revisa la experiencia.' });
  assert.equal(count(), 3);
  assertAllCanonical({ ...result.context.payload.sources, companyProfile: result.context.payload.sources.companyProfile, companyLibrary: result.context.payload.sources.companyLibrary }, library);
  assert.equal(result.context.sourceIds.has('company-profile'), true);
  assert.equal(result.context.sourceIds.has('contract'), true);
  for (const document of library.documents) assert.equal(result.context.sourceIds.has(document.sourceId), true);
  assert.equal(result.prompt.includes('[CONTEXTO TRUNCADO POR SEGURIDAD]'), false);
  assert.ok(JSON.stringify(result.context.payload).length < 128000);
  const oversized = agentWithSources(profile, library, 150000).agent;
  await assert.rejects(oversized.prepareLicitationAgentRun({ tenantId: 'synthetic-tenant', idContrato: 1, userMessage: 'Revisa.' }), /No se envió ni se cortó la evidencia/);
});

const preparedPath = process.env.BUENAPRO_COMPANY_EVIDENCE_PREPARED_PATH;
const baselinePath = process.env.BUENAPRO_COMPANY_EVIDENCE_BASELINE_PATH;
test('real 43 local readings retain every corrected summary, amount, classification, warning and SHA with contract budget', { skip: !preparedPath || !baselinePath }, async t => {
  const prepared = JSON.parse(fs.readFileSync(preparedPath, 'utf8'));
  const baseline = JSON.parse(fs.readFileSync(baselinePath, 'utf8'));
  const library = { documents: baseline.documents.map(document => {
    const entry = prepared.documents[document.id];
    assert.ok(entry, 'Every baseline document must have a prepared reading');
    return { sourceId: `company-document-${document.id}`, title: document.title, filename: document.filename, mimeType: document.mime_type, sizeBytes: document.size_bytes, documentType: document.document_type, description: entry.description, tags: document.tags ?? [], validUntil: document.valid_until, amount: entry.amount, entityName: document.entity_name, metadata: entry.metadata_json, updatedAt: document.updated_at };
  }) };
  const profile = { experience: prepared.experience_json, economicExperience: prepared.econ_experience_json };
  const input = JSON.stringify({ profile, library });
  const projected = helper.projectCompanyEvidenceContext(profile, library);
  assert.equal(library.documents.length, 43);
  assertAllCanonical(projected, library);
  const { agent } = agentWithSources(profile, library);
  const result = await agent.prepareLicitationAgentRun({ tenantId: 'offline-reading-tenant', idContrato: 1, userMessage: 'Revisa la experiencia.' });
  for (const document of library.documents) {
    assert.equal(result.prompt.includes(document.sourceId), true);
    assert.equal(result.prompt.includes(document.metadata.documentary_validation.sha256), true);
  }
  assertAllCanonical(result.context.payload.sources, library);
  const contextChars = JSON.stringify(result.context.payload).length;
  assert.ok(contextChars < 128000);
  assert.equal(result.prompt.includes('[CONTEXTO TRUNCADO POR SEGURIDAD]'), false);
  assert.equal(JSON.stringify({ profile, library }), input);
  t.diagnostic(JSON.stringify({ documents: library.documents.length, decisionProjectionChars: JSON.stringify(projected).length, contextWithSyntheticContractChars: contextChars, limit: 128000, networkCalls: 0, modelsCalled: 0, input: 'local prepared reading; synthetic contract headroom, no PG readback' }));
});
