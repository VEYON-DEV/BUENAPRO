const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, mocks) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(path.resolve(__dirname, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports, Buffer, Date, Set, Map, URL, console,
    fetch: async () => { throw new Error('Unexpected paid/network call in offline test'); },
    require(name) {
      if (Object.hasOwn(mocks, name)) return mocks[name];
      throw new Error(`Unexpected dependency in offline test: ${name}`);
    },
  });
  return exports;
}

const tenantId = '00000000-0000-0000-0000-000000000001';
function documents(count = 43) {
  return Array.from({ length: count }, (_, index) => {
    const id = `00000000-0000-0000-0000-${String(index + 1).padStart(12, '0')}`;
    const sha256 = (index + 1).toString(16).padStart(64, '0');
    return {
      id, title: `Contrato ${index + 1}`, filename: `respaldo-${index + 1}.pdf`,
      mime_type: 'application/pdf', size_bytes: 1024, document_type: 'experiencia',
      description: `Orden de suministro ${index + 1}. Conformidad pendiente de confirmar.`,
      tags: ['industrial'], amount: null, entity_name: 'Cliente sintético', valid_until: null,
      updated_at: '2026-10-13T00:00:00Z',
      metadata_json: {
        provenance: { import: 'synthetic-original-preserved' },
        documentary_extraction: {
          version: 'company_evidence_v2', sha256, status: 'pending_human_confirmation',
          extraction: {
            summary: `Orden ${index + 1} de suministro de variadores; no se identifica acta de conformidad.`,
            facts: [{ field: 'monto', value: 'USD 15,000', quote: 'Total USD 15,000', page: 2 }],
            conformity: 'not_identified', unknowns: ['Acta de conformidad'], warnings: [],
            inferred_labels: ['Automatización industrial'],
          },
        },
        document_text: {
          sha256, pages: [{ page: 1, text: 'COMPLETE_PRIVATE_PAGE_TEXT'.repeat(1000) }],
          ocr: { engine: 'synthetic-local-ocr', retained_locally: true },
        },
      },
    };
  });
}

function libraryQuery(rows, calls) {
  return async (sql, args) => {
    calls.push({ sql, args });
    if (sql.includes('FROM company_knowledge_items')) return { rows: [] };
    if (sql.includes('FROM company_documents')) {
      assert.match(sql, /tenant_id=\$1 AND usable_for_applications=true/);
      assert.match(sql, /metadata_json,'\{\}'::jsonb\) - 'document_text'/);
      assert.equal(args[0], tenantId);
      return { rows: rows.slice(0, args[1]) };
    }
    throw new Error(`Unexpected query in offline test: ${sql}`);
  };
}

test('all 43 active tenant documents remain structured and citeable without page text/OCR', async () => {
  const rows = documents();
  const before = JSON.stringify(rows);
  const calls = [];
  const service = load('companyLibrary.ts', { '@/server/db/client': { query: libraryQuery(rows, calls) } });
  const context = await service.agentCompanyLibraryContext(tenantId);
  assert.equal(context.documents.length, 43);
  assert.equal(calls.find(call => call.sql.includes('FROM company_documents')).args[1], 100);
  for (const [index, document] of context.documents.entries()) {
    assert.equal(document.sourceId, `company-document-${rows[index].id}`);
    assert.deepEqual(JSON.parse(JSON.stringify(document.metadata.provenance)), rows[index].metadata_json.provenance);
    assert.deepEqual(JSON.parse(JSON.stringify(document.metadata.documentary_extraction)), rows[index].metadata_json.documentary_extraction);
    assert.equal(Object.hasOwn(document.metadata, 'document_text'), false);
  }
  assert.equal(JSON.stringify(context).includes('COMPLETE_PRIVATE_PAGE_TEXT'), false);
  assert.equal(JSON.stringify(rows), before);
});

test('document limit is configurable and bounded to 100 without changing tenant/active filters', async () => {
  const calls = [];
  const service = load('companyLibrary.ts', { '@/server/db/client': { query: libraryQuery(documents(110), calls) } });
  for (const [documentLimit, expected] of [[43, 43], [1000, 100], [0, 1], [NaN, 100], [3.9, 3]]) {
    const context = await service.agentCompanyLibraryContext(tenantId, { documentLimit });
    assert.equal(context.documents.length, expected);
  }
  assert.equal(calls.filter(call => call.sql.includes('FROM company_documents')).length, 5);
});

test('the real agent prompt retains 43 experience summaries and valid document citations under its complete-context budget', async (t) => {
  const rows = documents();
  const experience = rows.map(row => ({
    objeto: row.title, tipo_objeto: 'bienes', monto: 15000, moneda: 'USD',
    descripcion: row.description,
    documents: [{ id: row.id }],
    documentary_summary: { status: 'pending_human_confirmation', sources: [{
      document_id: row.id, sha256: row.metadata_json.documentary_extraction.sha256,
      extraction: row.metadata_json.documentary_extraction.extraction,
    }] },
  }));
  const calls = [];
  const readLibrary = libraryQuery(rows, calls);
  const query = async (sql, args) => {
    if (sql.includes('FROM company_knowledge_items') || sql.includes('FROM company_documents')) return readLibrary(sql, args);
    if (sql.includes('FROM seace_contracts c')) {
      return { rows: [{
        id_contrato: '1', application_id: null, match_id: null,
        contract: { codigo: 'SYN-1', descripcion: 'Suministro de variadores' },
        profile: { razonSocial: 'Empresa industrial sintética', experience, economicExperience: {} },
        business_lines: [], application: {}, items: [], requirements: [], quote_documents: [], attachments: [],
      }] };
    }
    if (sql.includes('FROM contract_documents') || sql.includes('FROM requirement_facets')) return { rows: [] };
    throw new Error(`Unexpected agent query: ${sql}`);
  };
  const service = load('companyLibrary.ts', { '@/server/db/client': { query } });
  const delivery = load('../agent/documentDelivery.ts', {});
  const evidence = load('../agent/companyEvidenceContext.ts', {});
  const agent = load('../agent/licitationAgent.ts', {
    '@/server/db/client': { query },
    '@/server/services/companyLibrary': service,
    '@/server/env': { ensureServerEnv: () => { throw new Error('Paid client configuration must not execute'); } },
    './documentDelivery': delivery,
    './companyEvidenceContext': evidence,
  });
  const prepared = await agent.prepareLicitationAgentRun({ tenantId, idContrato: 1, userMessage: 'Revisa mi experiencia' });
  assert.equal(prepared.context.payload.sources.companyProfile.experience.length, 43);
  assert.equal(prepared.context.payload.sources.companyLibrary.documents.length, 43);
  const serializedContext = JSON.stringify(prepared.context.payload);
  assert.ok(serializedContext.length < 128000, `Synthetic compact context uses ${serializedContext.length} characters`);
  t.diagnostic(`43 synthetic compact evidence records use ${serializedContext.length}/128000 context characters`);
  assert.equal(prepared.prompt.includes('[CONTEXTO TRUNCADO POR SEGURIDAD]'), false);
  assert.equal(prepared.prompt.includes('COMPLETE_PRIVATE_PAGE_TEXT'), false);
  for (const row of rows) {
    assert.equal(prepared.context.sourceIds.has(`company-document-${row.id}`), true);
    assert.equal(prepared.prompt.includes(row.metadata_json.documentary_extraction.sha256), true);
  }
  const columns = prepared.context.payload.sources.contextRepresentation.rowColumns.documentEvidence;
  const summaryIndex = columns.indexOf('documentSummary');
  assert.notEqual(summaryIndex, -1);
  for (const [index, document] of prepared.context.payload.sources.companyLibrary.documents.entries()) {
    assert.equal(document.evidence[summaryIndex], rows[index].metadata_json.documentary_extraction.extraction.summary);
  }
  const last = rows[42];
  const answer = agent.validateLicitationAgentResult({
    answer: 'Falta verificar la conformidad del último contrato.',
    citations: [{ sourceId: `company-document-${last.id}`, label: 'Orden 43', evidence: 'Total USD 15,000, p. 2' }],
    proposedChanges: { application: {}, items: [], requirements: [] },
  }, prepared.context, 'synthetic-model');
  assert.equal(answer.citations.length, 1);
  assert.equal(answer.citations[0].sourceId, `company-document-${last.id}`);
});
