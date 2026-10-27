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
    exports, Object, Set, Map, Date, JSON, AbortSignal, process: { cwd: () => '/workspace/apps/web', env: { GEMINI_API_KEY: 'offline-test-key', GEMINI_MODEL: 'gemini-3.1-flash-lite', GEMINI_FALLBACK_MODEL: 'gemini-2.5-flash' } },
    fetch: async () => { throw new Error('Unexpected network in offline test'); },
    require(name) {
      if (Object.hasOwn(mocks, name)) return mocks[name];
      if (name.startsWith('node:')) return require(name);
      throw new Error(`Unexpected module: ${name}`);
    },
    ...globals,
  });
  return exports;
}
const policy = load('matchAnalysisPolicy.ts');
const clone = value => JSON.parse(JSON.stringify(value));

function fixture() {
  const experiences = Array.from({ length: 43 }, (_, index) => ({
    numero_contrato: `CONTRACT-${index}`, objeto: 'Suministro y configuración de servidores', rubro: 'Industrial', especialidad: 'Hardware',
    monto: 1180, moneda: 'PEN', fecha_inicio: '2025-01-01', modalidad_participacion: 'individual',
    documentary_summary: {
      status: 'pending_human_confirmation', no_legal_accreditation: true,
      original_fields: { backup: 'OLD_BACKUP_SENTINEL'.repeat(300) }, original_description: 'OLD_DESCRIPTION_SENTINEL',
      sources: [{ document_id: `doc-${index}`, sha256: String(index).padStart(64, '0'), status: 'pending_human_confirmation', extraction: {
        summary: `Fuente ${index}: suministro de servidores. Conformidad identificada; monto sujeto al rubro solicitado.`,
        classification: { rubro: 'Industrial', specialty: 'Hardware', page: 2, quote: 'Suministro de servidores', inferred: true },
        conformity: 'explicit_documentary_evidence', warnings: ['Factura y orden corresponden al mismo contrato; no sumar.'], unknowns: [],
        principal_amount_index: 0,
        amounts: [
          { amount_decimal: '1180.00', literal: 'S/ 1,180.00', currency: 'PEN', label: 'Total orden', tax_basis: 'includes_tax', source_type: 'orden_de_compra', quote: 'Total S/ 1,180.00 incluido IGV', page: 2 },
          { amount_decimal: '1000.00', literal: 'US$ 1,000.00', currency: 'USD', label: 'Otra partida', tax_basis: 'excludes_tax', source_type: 'factura', quote: 'US$ 1,000.00 sin impuestos', page: 3 },
        ],
        facts: [{ field: 'Objeto', value: 'Suministro de servidores', quote: 'Suministro de servidores', page: 1 },
          { field: 'Fecha de conformidad', value: '2025-01-01', quote: 'Conforme el 01/01/2025', page: 4 },
          { field: 'RUC proveedor', value: 'PRIVATE_ID_SENTINEL', quote: 'PRIVATE_ID_SENTINEL', page: 1 }],
        full_text: 'FULL_TEXT_SENTINEL'.repeat(500), raw: 'RAW_RESPONSE_SENTINEL',
      } }],
    },
  }));
  return {
    razon_social: 'Empresa sintética', ruc: '00000000000', profile_hash: 'private-profile-hash',
    identity_json: { rnp: [{ tipo: 'bienes' }], descripcion: 'COMMERCIAL_DESCRIPTION_SENTINEL'.repeat(300), profile_ingestion_enabled: false, demo_identity: 'DEMO_SENTINEL' },
    team_json: [{ nombre: 'Equipo real', verified: true, role: 'Ingeniería' }, { nombre: 'Equipo demo', is_demo: true }, { nombre: 'Equipo hipotético', verification_status: 'hypothetical' }],
    experience_json: experiences,
    econ_experience_json: { bienes: 9999999999, servicios: 8888888888, areas: experiences.map((row, index) => ({ monto: row.monto, moneda: row.moneda, documents: [`doc-${index}`], source_record_index: index, no_global_sum: true })), documentary_provenance: { backup: 'AUDIT_SENTINEL' } },
    equipment_json: [], certifications_json: [], hireable_roles_json: [],
  };
}

test('compact profile preserves all 43 sources, currencies, tax bases, citations and review limitations without backups or double counting', () => {
  const original = fixture(), before = clone(original);
  const compact = policy.buildCompactMatchProfile(original);
  assert.deepEqual(original, before, 'projection does not edit stored profile');
  assert.equal(compact.experience_json.length, 43);
  assert.equal(compact.experience_json.flatMap(row => row.documentary_summary.sources).length, 43);
  assert.equal(compact.econ_experience_json.areas.length, 0);
  assert.equal(compact.econ_experience_json.no_global_sum, true);
  assert.equal(compact.team_json.length, 1);
  for (const [index, row] of compact.experience_json.entries()) {
    const source = row.documentary_summary.sources[0];
    assert.equal(source.document_id, `doc-${index}`);
    assert.equal(source.status, 'pending_human_confirmation');
    assert.deepEqual(clone(source.extraction.amounts), original.experience_json[index].documentary_summary.sources[0].extraction.amounts);
    assert.deepEqual(clone(source.extraction.classification), original.experience_json[index].documentary_summary.sources[0].extraction.classification);
    assert.deepEqual(clone(source.extraction.facts), original.experience_json[index].documentary_summary.sources[0].extraction.facts);
  }
  const serialized = JSON.stringify(compact);
  assert.doesNotMatch(serialized, /(?:OLD_BACKUP|OLD_DESCRIPTION|FULL_TEXT|RAW_RESPONSE|COMMERCIAL_DESCRIPTION|AUDIT|DEMO)_SENTINEL/);
  assert.ok(serialized.length < JSON.stringify(original).length * 0.25);
});

test('removes exact duplicated amount evidence only, remaps principal and preserves unbacked economic areas', () => {
  const original = fixture();
  const extraction = original.experience_json[0].documentary_summary.sources[0].extraction;
  extraction.amounts.splice(1, 0, clone(extraction.amounts[0]));
  extraction.principal_amount_index = 2;
  original.econ_experience_json.areas.push({ monto: 100, moneda: 'USD', documents: ['unread-doc'], status: 'declared', no_global_sum: true });
  const compact = policy.buildCompactMatchProfile(original);
  const source = compact.experience_json[0].documentary_summary.sources[0].extraction;
  assert.equal(source.amounts.length, 2);
  assert.equal(source.principal_amount_index, 1);
  assert.equal(source.amounts[source.principal_amount_index].currency, 'USD');
  assert.equal(compact.econ_experience_json.areas.length, 1);
  assert.equal(compact.econ_experience_json.areas[0].documents[0], 'unread-doc');
});

test('submission checklist excludes routine RNP/RUC/impediments/forms while preserving substantive equipment, authorizations, staff and technical SLA', () => {
  const facets = [
    ['rnp', 'RNP Vigente'], ['ruc_status', 'RUC activo y habido'], ['not_disqualified', 'No contar con impedimento'],
    ['proposal_document', 'Formato de propuesta económica'], ['proposal_document', 'Formato de declaración jurada'], ['cci', 'Carta de CCI'],
    ['legal_capacity', 'Empresa dedicada al rubro.'], ['proposal_document', 'Documentos que acrediten la formación académica'],
    ['specific_experience', 'Experiencia específica S/ 40,000 en servidores'], ['license', 'Autorización oficial del fabricante'],
    ['proposal_document', 'Documentación técnica oficial de plataforma: API y SLA'], ['key_personnel', 'Especialista ITIL'],
  ].map(([facet, label]) => ({ facet, label, required: true, details_json: {} }));
  const result = policy.splitMatchRequirements(facets);
  assert.equal(result.administrativeChecklist.length, 8);
  assert.equal(result.substantive.length, 4);
  assert.deepEqual(Array.from(result.substantive, row => row.source_index), [9, 10, 11, 12]);
  assert.ok(result.administrativeChecklist.every(row => row.score_impact === false && row.estado === 'preparar'));
  assert.equal(policy.isAdministrativeRequirement({ facet: 'legal_capacity', label: 'Autorización del organismo para operar material peligroso' }), false);
});

function expectedRequirement(index, tipo) {
  return { source_index: index, tipo, requisito: `Requisito ${index}`, obligatorio: true, detalle: {} };
}

test('proposal documents the model omitted move to the checklist and every other requirement is kept', () => {
  const expected = [expectedRequirement(2, 'key_personnel'), expectedRequirement(7, 'proposal_document'), expectedRequirement(8, 'proposal_document'), expectedRequirement(13, 'specific_experience')];
  const checklist = [{ tipo: 'rnp', requisito: 'RNP', estado: 'preparar', score_impact: false }];
  const kept = policy.checklistOmittedProposalDocuments([{ source_index: 2 }, { source_index: 13 }], expected, checklist);
  assert.deepEqual(Array.from(kept, row => row.source_index), [2, 13]);
  assert.deepEqual(clone(checklist), [
    { tipo: 'rnp', requisito: 'RNP', estado: 'preparar', score_impact: false },
    { tipo: 'proposal_document', requisito: 'Requisito 7', estado: 'preparar', score_impact: false },
    { tipo: 'proposal_document', requisito: 'Requisito 8', estado: 'preparar', score_impact: false },
  ]);
});

test('a proposal document the model judged stays a requirement', () => {
  const expected = [expectedRequirement(2, 'key_personnel'), expectedRequirement(7, 'proposal_document'), expectedRequirement(8, 'proposal_document')];
  const checklist = [];
  const kept = policy.checklistOmittedProposalDocuments([{ source_index: 2 }, { source_index: 8 }], expected, checklist);
  assert.deepEqual(Array.from(kept, row => row.source_index), [2, 8]);
  assert.deepEqual(Array.from(checklist, row => row.requisito), ['Requisito 7']);
});

test('an omitted requirement of any other type is not hidden', () => {
  const expected = [expectedRequirement(2, 'key_personnel'), expectedRequirement(3, 'insurance'), expectedRequirement(4, 'proposal_document')];
  const checklist = [];
  const kept = policy.checklistOmittedProposalDocuments([{ source_index: 2 }], expected, checklist);
  assert.deepEqual(Array.from(kept, row => row.source_index), [2, 3]);
  assert.equal(checklist.length, 1);
  const untouched = policy.checklistOmittedProposalDocuments([], expected.slice(0, 2), checklist);
  assert.deepEqual(Array.from(untouched, row => row.source_index), [2, 3]);
  assert.equal(checklist.length, 1);
});

test('unknown/prohibited consortium or subcontract permission never becomes an executable recommendation', () => {
  const reqs = [{ estado: 'cumple_con_accion', accion: 'Formar consorcio con proveedor' }, { estado: 'cumple_con_accion', accion: 'Subcontratar soporte técnico' }];
  const actions = policy.guardParticipationActions(reqs, ['Formar consorcio', 'Seleccionar contrato compatible'], { participation: { subcontratacion: { status: 'prohibited' } } });
  assert.equal(reqs[0].estado, 'requiere_revision');
  assert.equal(reqs[0].accion, 'Verificar autorización y condiciones en las bases');
  assert.equal(reqs[1].accion, 'Buscar alternativa permitida por las bases');
  assert.equal(actions[1], 'Seleccionar contrato compatible');
});

function directService({ profile = fixture(), facets, response, existing = null, contract = null, failure = false, promptText = 'OFFLINE_SYSTEM_PROMPT' } = {}) {
  const statements = [], requests = [], promptPaths = [];
  const selectedFacets = facets ?? [{ facet: 'rnp', label: 'RNP Vigente', required: true, details_json: {}, facet_hash: 'rnp' }, { facet: 'economic_experience', label: 'Experiencia compatible S/ 40,000', required: true, details_json: { monto: 40000 }, facet_hash: 'economic' }];
  const client = {
    query: async (sql, values) => {
      statements.push({ sql, values });
      return { rows: sql.includes('INSERT INTO matches') ? [{ id: 99 }] : [] };
    }, release() {},
  };
  const service = load('matchAnalysis.ts', {
    './matchAnalysisPolicy': policy,
    '@/server/env': { ensureServerEnv() {} },
    'node:fs': { existsSync: () => true, readFileSync: file => { promptPaths.push(file); return promptText; } },
    '@/server/db/client': { pool: {
      query: async (sql, values) => {
        statements.push({ sql, values });
        return { rows: sql.includes('FROM company_profiles') ? profile ? [{ id: 'e11f2610-eb56-457a-97eb-bbd24dd6f1a1', ...profile }] : []
          : sql.includes('FROM seace_contracts') ? [contract ?? { codigo: 'QA', descripcion: 'Servidor', entidad_nombre: 'Entidad QA', extraction_id: 44, quality: 'auto', requires_human_review: false, summary_json: {} }]
          : sql.includes('FROM requirement_facets') ? selectedFacets : sql.includes('FROM matches') && existing ? [existing] : [] };
      }, connect: async () => client,
    } },
  }, { fetch: async (url, options) => {
    requests.push({ url, options, body: JSON.parse(options.body) });
    if (failure) return { ok: false, status: 429 };
    const content = response ?? { veredicto: 'gris', score: 72, resumen: 'Experiencia cercana; comprobar compatibilidad.', requisitos: [{ source_index: 2, source_facet: 'economic_experience', evidence_refs: ['doc-0'], evidence_explanation: 'Contrato de servidores en PEN; validar qué partidas son computables.', requisito: 'Experiencia compatible', categoria: 'experiencia_economica', estado: 'requiere_revision', critico: true, gap: 'Validar objeto y monto computable.', accion: 'Seleccionar contratos compatibles.' }], acciones_recomendadas: ['Seleccionar contratos compatibles.'] };
    return { ok: true, json: async () => ({ candidates: [{ finishReason: 'STOP', content: { parts: [{ text: JSON.stringify(content) }] } }], usageMetadata: { promptTokenCount: 100, candidatesTokenCount: 10, thoughtsTokenCount: 3 } }) };
  } });
  return { service, statements, requests, promptPaths };
}

test('effective web transport reads concrete V5, preserves source alternatives, and records the version without treating old case scores as evidence', async () => {
  const actualPrompt = fs.readFileSync(path.resolve(__dirname, '../../../../workers/seace/prompts/match_analysis_v5.txt'), 'utf8');
  const golden = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../../workers/seace/tests/fixtures/llm2_concrete_evidence_cases.json'), 'utf8'));
  for (const recorded of golden.cases) {
    const profile = { experience_json: recorded.evidence.map(evidence => ({ numero_contrato: evidence.contract, objeto: evidence.activity,
      documentary_summary: { status: 'pending_human_confirmation', sources: [{ document_id: evidence.document_id,
        extraction: { summary: evidence.activity, classification: { quote: evidence.quote, page: evidence.page },
          facts: [{ field: 'Objeto', value: evidence.activity, quote: evidence.quote, page: evidence.page }], amounts: [] } }] } })) };
    const facets = Array.from({ length: Math.max(...recorded.requirements.map(row => row.source_index)) }, (_, index) => {
      const source = recorded.requirements.find(row => row.source_index === index + 1);
      return source ? { facet: source.tipo, label: source.requisito, required: source.obligatorio, details_json: source.detalle, facet_hash: `source-${index}` }
        : { facet: 'rnp', label: 'RNP vigente', required: true, details_json: {}, facet_hash: `admin-${index}` };
    });
    const requirements = recorded.requirements.map(row => ({ source_index: row.source_index, source_facet: row.tipo,
      requisito: row.requisito, categoria: 'otro', estado: 'requiere_revision', critico: true,
      gap: 'Respuesta sintética de transporte.', accion: 'Contrastar condición explícita.',
      evidence_refs: [], evidence_explanation: 'Transporte offline; no análisis generado.', economic_comparison: [] }));
    const testbed = directService({ profile, facets, promptText: actualPrompt,
      response: { veredicto: 'gris', score: 70, resumen: 'Respuesta sintética de transporte.', requisitos: requirements, acciones_recomendadas: [] } });
    await testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: recorded.id });
    assert.equal(testbed.requests.length, 1, 'intercepted transport only, no provider call');
    assert.ok(testbed.promptPaths.every(file => file.endsWith('/match_analysis_v5.txt')));
    const sent = testbed.requests[0].body;
    assert.ok(sent.systemInstruction.parts[0].text.startsWith(actualPrompt));
    assert.match(sent.systemInstruction.parts[0].text, /No exijas misma marca/);
    assert.match(sent.systemInstruction.parts[0].text, /SCADA, instrumentación, tanques, cableado/);
    assert.match(sent.systemInstruction.parts[0].text, /no está acreditado en los datos/);
    const payload = JSON.parse(sent.contents[0].parts[0].text);
    assert.deepEqual(payload.requisitos.map(row => row.source_index), recorded.requirements.map(row => row.source_index));
    assert.deepEqual(payload.requisitos.map(row => row.detalle), recorded.requirements.map(row => row.detalle));
    assert.equal(Object.hasOwn(payload, 'analisis_previo'), false);
    assert.deepEqual(payload.perfil.experience_json.map(row => row.numero_contrato), recorded.evidence.map(row => row.contract));
    const saved = testbed.statements.find(row => row.sql.includes('INSERT INTO matches'));
    assert.equal(JSON.parse(saved.values[4]).meta.prompt_version, 'match_analysis_v5_concrete_evidence');
  }
});

test('direct match scores only substantive capacity; uncertainty keeps 72 rather than old gray 30–49 band and administrative checklist stays outside gaps', async () => {
  const testbed = directService();
  const result = await testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 101535, profileId: 'e11f2610-eb56-457a-97eb-bbd24dd6f1a1' });
  assert.equal(result.score, 72); assert.equal(result.verdict, 'gris');
  assert.equal(testbed.requests.length, 1);
  const payload = JSON.parse(testbed.requests[0].body.contents[0].parts[0].text);
  assert.equal(payload.requisitos.length, 1);
  assert.equal(payload.requisitos[0].source_index, 2);
  // Gemini sees the closest experiences only, and is told how many the company has.
  assert.equal(payload.perfil.experience_json.length, policy.MATCH_EXPERIENCE_LIMIT);
  assert.match(payload.perfil.experience_note, /de 43 registradas/);
  assert.equal(payload.perfil.econ_experience_json.areas.length, 0);
  const saved = testbed.statements.find(row => row.sql.includes('INSERT INTO matches'));
  const breakdown = JSON.parse(saved.values[4]), missing = JSON.parse(saved.values[5]);
  assert.equal(breakdown.administrative_checklist.length, 1);
  assert.equal(breakdown.administrative_checklist[0].score_impact, false);
  assert.equal(missing.length, 1); assert.equal(missing[0].facet, 'experiencia_economica');
  assert.equal(breakdown.meta.output_tokens, 13);
  assert.equal(breakdown.meta.extraction_id, 44);
  assert.equal(breakdown.meta.context_version, 'compact_match_context_v1');
  assert.equal(testbed.statements[0].values[1], 'e11f2610-eb56-457a-97eb-bbd24dd6f1a1');
  const facetsRead = testbed.statements.find(row => row.sql.includes('FROM requirement_facets'));
  assert.match(facetsRead.sql, /extraction_id=\$2/);
  assert.equal(facetsRead.values[1], 44);
  const pendingClosure = testbed.statements.find(row => row.sql.includes('UPDATE worker_jobs'));
  assert.equal(pendingClosure.values[0], 'e11f2610-eb56-457a-97eb-bbd24dd6f1a1');
  assert.doesNotMatch(pendingClosure.sql, /SELECT id::text FROM company_profiles/);
});

test('provider rejection has exactly one request, no paid fallback and no business writes', async () => {
  const testbed = directService({ failure: true });
  await assert.rejects(testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 }), /HTTP 429/);
  assert.equal(testbed.requests.length, 1);
  assert.ok(testbed.requests[0].url.includes('gemini-3.1-flash-lite'));
  assert.equal(testbed.statements.filter(row => /INSERT|UPDATE/.test(row.sql)).length, 0);
});

test('requirement category and criticality are bound to the selected source facet and its mandatory flag', async () => {
  for (const required of [true, false]) {
    const testbed = directService({ facets: [{ facet: 'equipment', label: 'Servidor disponible', required, details_json: {}, facet_hash: 'equipment' }],
      response: { veredicto: 'gris', score: 60, resumen: 'Validar disponibilidad de equipo.', requisitos: [{ source_index: 1, source_facet: 'economic_experience', requisito: 'Servidor disponible', categoria: 'experiencia_economica', estado: 'requiere_revision', critico: true, gap: 'Confirmar equipo compatible.', accion: 'Validar equipo.', evidence_refs: [], evidence_explanation: 'Inventario pendiente de verificación.', economic_comparison: [] }], acciones_recomendadas: [] } });
    await testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 });
    const saved = testbed.statements.find(row => row.sql.includes('INSERT INTO matches'));
    const requirement = JSON.parse(saved.values[4]).requisitos[0];
    assert.equal(requirement.source_facet, 'equipment');
    assert.equal(requirement.categoria, 'equipamiento');
    assert.equal(requirement.critico, required);
  }
});

test('foreign profile, invalid extraction and all-admin facets fail before sending a paid request', async () => {
  for (const options of [
    { profile: null },
    { contract: { extraction_id: 44, quality: 'failed', requires_human_review: false } },
    { contract: { extraction_id: 44, quality: 'auto', requires_human_review: true } },
    { facets: [{ facet: 'rnp', label: 'RNP Vigente', required: true, details_json: {}, facet_hash: 'rnp' }] },
  ]) {
    const testbed = directService(options);
    await assert.rejects(testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 }));
    assert.equal(testbed.requests.length, 0);
    assert.equal(testbed.statements.filter(row => /INSERT|UPDATE/.test(row.sql)).length, 0);
  }
});

test('incomplete or administrative model output cannot be persisted as a capacity score, with no retry', async () => {
  const testbed = directService({ response: { veredicto: 'gris', score: 40, resumen: 'No RNP.', requisitos: [{ source_index: 2, requisito: 'RNP', categoria: 'identidad', estado: 'requiere_revision', critico: true }], acciones_recomendadas: [] } });
  await assert.rejects(testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 }), /exactamente los requisitos sustantivos/);
  assert.equal(testbed.requests.length, 1);
  assert.equal(testbed.statements.filter(row => /INSERT|UPDATE/.test(row.sql)).length, 0);
});

const PAPERWORK_FACETS = [
  { facet: 'rnp', label: 'RNP Vigente', required: true, details_json: {}, facet_hash: 'rnp' },
  { facet: 'key_personnel', label: 'Especialista ITIL', required: true, details_json: {}, facet_hash: 'itil' },
  { facet: 'proposal_document', label: 'Contratos y conformidades de las prestaciones', required: true, details_json: {}, facet_hash: 'contracts' },
  { facet: 'proposal_document', label: 'Documentación técnica oficial de plataforma: API y SLA', required: true, details_json: {}, facet_hash: 'sla' },
  { facet: 'insurance', label: 'Póliza de responsabilidad civil', required: true, details_json: {}, facet_hash: 'policy' },
];

function judgement(sourceIndex, facet, label) {
  return { source_index: sourceIndex, source_facet: facet, requisito: label, categoria: 'otro', estado: 'requiere_revision', critico: true, gap: 'Validar con el perfil.', accion: 'Contrastar con las bases.', evidence_refs: [], evidence_explanation: 'Sin fuente atribuida.', economic_comparison: [] };
}

function paperworkResponse(requisitos) {
  return { veredicto: 'gris', score: 60, resumen: 'Validar los requisitos.', requisitos, acciones_recomendadas: [] };
}

test('proposal documents omitted by the model are saved as paperwork while the judged ones are scored', async () => {
  const testbed = directService({ facets: PAPERWORK_FACETS, response: paperworkResponse([
    judgement(2, 'key_personnel', 'Especialista ITIL'),
    judgement(4, 'proposal_document', 'Documentación técnica oficial de plataforma: API y SLA'),
    judgement(5, 'insurance', 'Póliza de responsabilidad civil'),
  ]) });
  const result = await testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 });
  assert.equal(result.score, 60);
  assert.equal(testbed.requests.length, 1);
  const payload = JSON.parse(testbed.requests[0].body.contents[0].parts[0].text);
  assert.deepEqual(payload.requisitos.map(row => row.source_index), [2, 3, 4, 5]);
  const saved = testbed.statements.find(row => row.sql.includes('INSERT INTO matches'));
  const breakdown = JSON.parse(saved.values[4]);
  assert.deepEqual(breakdown.requisitos.map(row => row.source_index), [2, 4, 5]);
  assert.deepEqual(breakdown.requisitos.map(row => row.source_facet), ['key_personnel', 'proposal_document', 'insurance']);
  assert.deepEqual(breakdown.administrative_checklist.map(row => row.requisito), ['RNP Vigente', 'Contratos y conformidades de las prestaciones']);
  assert.ok(breakdown.administrative_checklist.every(row => row.estado === 'preparar' && row.score_impact === false));
  const missing = JSON.parse(saved.values[5]);
  assert.deepEqual(missing.map(row => row.label), ['Especialista ITIL', 'Documentación técnica oficial de plataforma: API y SLA', 'Póliza de responsabilidad civil']);
});

test('an omitted requirement that is not a proposal document still rejects the result without saving', async () => {
  const testbed = directService({ facets: PAPERWORK_FACETS, response: paperworkResponse([
    judgement(2, 'key_personnel', 'Especialista ITIL'),
    judgement(4, 'proposal_document', 'Documentación técnica oficial de plataforma: API y SLA'),
  ]) });
  await assert.rejects(testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 }), /exactamente los requisitos sustantivos/);
  assert.equal(testbed.requests.length, 1);
  assert.equal(testbed.statements.filter(row => /INSERT|UPDATE/.test(row.sql)).length, 0);
});

test('reordered or repeated judgements still reject the result even when a proposal document was omitted', async () => {
  for (const requisitos of [
    [judgement(5, 'insurance', 'Póliza de responsabilidad civil'), judgement(2, 'key_personnel', 'Especialista ITIL'), judgement(4, 'proposal_document', 'Documentación técnica oficial de plataforma: API y SLA')],
    [judgement(2, 'key_personnel', 'Especialista ITIL'), judgement(2, 'key_personnel', 'Especialista ITIL'), judgement(4, 'proposal_document', 'Documentación técnica oficial de plataforma: API y SLA')],
  ]) {
    const testbed = directService({ facets: PAPERWORK_FACETS, response: paperworkResponse(requisitos) });
    await assert.rejects(testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 }), /exactamente los requisitos sustantivos/);
    assert.equal(testbed.statements.filter(row => /INSERT|UPDATE/.test(row.sql)).length, 0);
  }
});

test('when the model judged none of the proposal documents there is no score to save', async () => {
  const testbed = directService({ facets: PAPERWORK_FACETS.filter(row => ['rnp', 'proposal_document'].includes(row.facet)), response: paperworkResponse([]) });
  await assert.rejects(testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 }), /ningún requisito sustantivo/);
  assert.equal(testbed.requests.length, 1);
  assert.equal(testbed.statements.filter(row => /INSERT|UPDATE/.test(row.sql)).length, 0);
});

test('economic conclusions without a documentary reference become review rather than invented compliance or deficit', async () => {
  for (const estado of ['cumple', 'no_cumple']) {
    const testbed = directService({ response: { veredicto: estado === 'cumple' ? 'verde' : 'rojo', score: 70, resumen: 'Comparar experiencia.',
      requisitos: [{ source_index: 2, source_facet: 'economic_experience', requisito: 'Experiencia compatible', categoria: 'experiencia_economica', estado, critico: true, gap: estado === 'cumple' ? null : 'Monto insuficiente', accion: null, evidence_refs: [], evidence_explanation: 'Sin una fuente atribuida.' }], acciones_recomendadas: [] } });
    const result = await testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 });
    assert.equal(result.verdict, 'gris'); assert.equal(result.score, null);
    const saved = testbed.statements.find(row => row.sql.includes('INSERT INTO matches'));
    const requirement = JSON.parse(saved.values[4]).requisitos[0];
    assert.equal(requirement.estado, 'requiere_revision');
    assert.equal(requirement.source_facet, 'economic_experience');
    assert.equal(JSON.parse(saved.values[4]).meta.raw_model_score, 70);
    assert.equal(JSON.parse(saved.values[4]).meta.score_validated, false);
    assert.doesNotMatch(requirement.gap, /9999999999|8888888888|acreditas|déficit/i);
  }
});

function economicApproval(overrides = {}, minimum = 1000) {
  const requirements = [{ source_index: 8, source_facet: 'specific_experience', estado: 'cumple', critico: true,
    evidence_refs: ['doc-0'],
    economic_comparison: [{ document_id: 'doc-0', amount_index: 0, currency: 'PEN', eligible_amount_decimal: '1180.00', compatible_scope_quote: 'Suministro de servidores', full_amount_comparable: true, period_supported: true, ...overrides }] }];
  const substantive = [{ source_index: 8, tipo: 'specific_experience', requisito: 'Experiencia facturada compatible en los últimos diez años', detalle: { monto: minimum } }];
  return { requirements, substantive };
}

test('economic gate admits only distinct documented PEN amounts with literal scope and dates in the allowed period', () => {
  const data = economicApproval();
  const result = policy.guardEconomicComparisons(data.requirements, policy.buildCompactMatchProfile(fixture()), data.substantive, new Date('2026-10-13T00:00:00Z'));
  assert.equal(result.scoreValidated, true);
  assert.equal(data.requirements[0].estado, 'cumple');
});

test('missing structured comparison or invalid amount/currency/scope/period/mixed total prevents economic approval', () => {
  const invalid = [
    { amount_index: 44 }, { eligible_amount_decimal: '99999.00' }, { amount_index: 1, eligible_amount_decimal: '1000.00', currency: 'PEN' },
    { currency: 'USD' }, { compatible_scope_quote: 'Análisis de imágenes satelitales inexistente' },
    { full_amount_comparable: false }, { period_supported: false },
  ];
  for (const override of invalid) {
    const data = economicApproval(override);
    const result = policy.guardEconomicComparisons(data.requirements, policy.buildCompactMatchProfile(fixture()), data.substantive, new Date('2026-10-13T00:00:00Z'));
    assert.equal(result.scoreValidated, false, JSON.stringify(override));
    assert.equal(data.requirements[0].estado, 'requiere_revision');
    assert.ok(data.requirements[0].economic_validation_reasons.length > 0);
  }
  const absent = economicApproval(); absent.requirements[0].economic_comparison = [];
  assert.equal(policy.guardEconomicComparisons(absent.requirements, fixture(), absent.substantive).scoreValidated, false);
  const stale = economicApproval(), oldProfile = fixture();
  oldProfile.experience_json[0].documentary_summary.sources[0].extraction.facts[1].value = '2010-01-01';
  oldProfile.experience_json[0].documentary_summary.sources[0].extraction.facts[1].quote = 'Conforme el 01/01/2010';
  assert.equal(policy.guardEconomicComparisons(stale.requirements, oldProfile, stale.substantive, new Date('2026-10-13T00:00:00Z')).scoreValidated, false);
});

test('duplicated documents or separate documents for the same contract cannot satisfy a larger economic minimum', () => {
  const duplicated = economicApproval({}, 2300);
  duplicated.requirements[0].economic_comparison.push(clone(duplicated.requirements[0].economic_comparison[0]));
  assert.equal(policy.guardEconomicComparisons(duplicated.requirements, fixture(), duplicated.substantive).scoreValidated, false);
  const duplicateContract = economicApproval({}, 2300), profile = fixture();
  duplicateContract.requirements[0].evidence_refs.push('doc-1');
  duplicateContract.requirements[0].economic_comparison.push({ ...duplicateContract.requirements[0].economic_comparison[0], document_id: 'doc-1' });
  profile.experience_json[1].numero_contrato = profile.experience_json[0].numero_contrato;
  assert.equal(policy.guardEconomicComparisons(duplicateContract.requirements, profile, duplicateContract.substantive).scoreValidated, false);
});

test('economic proof must attribute its document in evidence_refs and use the explicitly required currency', () => {
  const noReference = economicApproval(); noReference.requirements[0].evidence_refs = [];
  assert.equal(policy.guardEconomicComparisons(noReference.requirements, fixture(), noReference.substantive).scoreValidated, false);
  const usd = economicApproval({ amount_index: 1, currency: 'USD', eligible_amount_decimal: '1000.00' });
  usd.substantive[0].detalle.moneda = 'USD';
  assert.equal(policy.guardEconomicComparisons(usd.requirements, fixture(), usd.substantive).scoreValidated, true, 'a USD minimum is compared with USD, without FX');
});

test('cache requires actual prompt hash and semantic gate version; unchanged current analysis avoids another request', async () => {
  const baseline = directService();
  await baseline.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 });
  const saved = baseline.statements.find(row => row.sql.includes('INSERT INTO matches'));
  const breakdown = JSON.parse(saved.values[4]);
  assert.match(breakdown.meta.prompt_sha256, /^[0-9a-f]{64}$/);
  assert.equal(breakdown.meta.semantic_guard_version, 'economic_comparison_guard_v1');
  const current = directService({ existing: { id: 99, score: 72, verdict: 'gris', breakdown_json: breakdown } });
  const result = await current.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 });
  assert.equal(result.skipped, 'unchanged'); assert.equal(current.requests.length, 0);
  for (const override of [{ prompt_sha256: 'a'.repeat(64) }, { semantic_guard_version: 'obsolete' }]) {
    const changed = clone(breakdown); Object.assign(changed.meta, override);
    const stale = directService({ existing: { id: 99, score: 72, verdict: 'gris', breakdown_json: changed } });
    await stale.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 });
    assert.equal(stale.requests.length, 1);
  }
});

test('unstructured 95 economic approval remains only as audited raw score and public result is null/review', async () => {
  const testbed = directService({ response: { veredicto: 'verde', score: 95, resumen: 'Monto industrial suficiente.',
    requisitos: [{ source_index: 2, source_facet: 'economic_experience', requisito: 'Experiencia S/ 40,000', categoria: 'experiencia_economica', estado: 'cumple', critico: true, gap: null, accion: null, evidence_refs: ['doc-0'], evidence_explanation: 'El contrato industrial alcanza el mínimo.', economic_comparison: [] }], acciones_recomendadas: [] } });
  const result = await testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 101535 });
  assert.equal(result.score, null); assert.equal(result.verdict, 'gris');
  const saved = testbed.statements.find(row => row.sql.includes('INSERT INTO matches'));
  assert.equal(saved.values[2], 95, 'preserve raw numeric score for existing non-null database schema');
  const breakdown = JSON.parse(saved.values[4]);
  assert.equal(breakdown.meta.raw_model_score, 95);
  assert.equal(breakdown.meta.score_validated, false);
  assert.equal(breakdown.requisitos[0].estado, 'requiere_revision');
  assert.ok(breakdown.requisitos[0].economic_validation_reasons.length);
  assert.doesNotMatch(breakdown.resumen, /suficiente/);
});

test('unknown evidence UUID rejects the result without saving or sending a second request', async () => {
  const testbed = directService({ response: { veredicto: 'verde', score: 90, resumen: 'Experiencia completa.',
    requisitos: [{ source_index: 2, source_facet: 'economic_experience', requisito: 'Experiencia compatible', categoria: 'experiencia_economica', estado: 'cumple', critico: true, gap: null, accion: null, evidence_refs: ['invented-doc'], evidence_explanation: 'Documento inventado.' }], acciones_recomendadas: [] } });
  await assert.rejects(testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1 }), /referencias documentales desconocidas/);
  assert.equal(testbed.requests.length, 1);
  assert.equal(testbed.statements.filter(row => /INSERT|UPDATE/.test(row.sql)).length, 0);
});

test('forced current evaluation does not anchor to an older 40 score with the same states', async () => {
  const testbed = directService({ existing: { id: 98, verdict: 'gris', score: 40, breakdown_json: { requisitos: [{ requisito: 'Experiencia compatible', estado: 'requiere_revision' }], meta: { prompt_version: 'match_analysis_v3', profile_hash: 'private-profile-hash' } } } });
  const result = await testbed.service.analyzeMatchDirect({ tenantId: 'tenant', idContrato: 1, force: true });
  assert.equal(result.score, 72);
  assert.equal(testbed.requests.length, 1);
});

// --- Which experiences Gemini sees (mirror of workers/seace/tests/test_relevant_experience.py) ---

function experienceItem(objeto, { anio = 2020, rubro = '', summary = '' } = {}) {
  const item = { objeto, rubro, anio };
  if (summary) item.documentary_summary = { sources: [{ document_id: objeto, extraction: { summary } }] };
  return item;
}
const compactWith = (experiences, areas = []) => ({ razon_social: 'DIN', experience_json: experiences, econ_experience_json: { areas, no_global_sum: true } });
const OPPORTUNITY = { descripcion: 'Mantenimiento preventivo de tableros eléctricos y variadores', resumen: {} };
const REQUIREMENTS = [{ requisito: 'Experiencia en mantenimiento de variadores de velocidad' }];

test('a profile within the experience limit is sent exactly as before', () => {
  const small = compactWith(Array.from({ length: policy.MATCH_EXPERIENCE_LIMIT }, (_, n) => experienceItem(`Obra ${n}`)));
  assert.equal(policy.selectRelevantExperience(small, OPPORTUNITY, REQUIREMENTS), small);
});

test('the closest experiences are kept complete, in order, with the same note as the worker', () => {
  const unrelated = Array.from({ length: 9 }, (_, n) => experienceItem(`Capacitación docente ${n}`, { rubro: 'EDUCACION' }));
  const close = experienceItem('Mantenimiento de variadores de velocidad', { anio: 2019 });
  const byDocument = experienceItem('Servicio industrial', { summary: 'Mantenimiento preventivo de tableros eléctricos' });
  const full = compactWith([...unrelated.slice(0, 4), close, ...unrelated.slice(4), byDocument]);
  const selected = policy.selectRelevantExperience(full, OPPORTUNITY, REQUIREMENTS, 3);
  const objects = selected.experience_json.map(item => item.objeto);
  assert.equal(objects.length, 3);
  assert.ok(objects.includes(close.objeto) && objects.includes(byDocument.objeto));
  assert.ok(objects.indexOf(close.objeto) < objects.indexOf(byDocument.objeto));
  assert.equal(selected.experience_note, 'Se incluyen las 3 experiencias más relacionadas con esta contratación de 11 registradas por la empresa.');
  assert.equal(full.experience_json[4], close);
});

test('object words weigh more, ties go to the most recent year and generic words do not count', () => {
  const old = experienceItem('Mantenimiento de variadores', { anio: 2015 });
  const recent = experienceItem('Mantenimiento de variadores', { anio: 2023 });
  const documentsOnly = experienceItem('Proyecto', { summary: 'variadores mantenimiento tableros' });
  const filler = Array.from({ length: 8 }, (_, n) => experienceItem(`Aula ${n}`));
  assert.deepEqual(policy.selectRelevantExperience(compactWith([old, documentsOnly, recent, ...filler]), OPPORTUNITY, REQUIREMENTS, 1).experience_json, [recent]);
  const generic = experienceItem('Servicio de contratación y adquisición de bienes en general');
  const relevant = experienceItem('Tableros eléctricos');
  const opportunity = { descripcion: 'Servicio de adquisición de tableros eléctricos', resumen: {} };
  assert.deepEqual(policy.selectRelevantExperience(compactWith([generic, relevant, ...filler]), opportunity, [], 1).experience_json, [relevant]);
});

test('economic areas follow their experience', () => {
  const experiences = [...Array.from({ length: 9 }, (_, n) => experienceItem(`Aula ${n}`)), experienceItem('Mantenimiento de variadores')];
  const areas = [{ monto: '100', source_record_index: 9 }, { monto: '50', source_record_index: 0 }, { monto: '10' }];
  const selected = policy.selectRelevantExperience(compactWith(experiences, areas), OPPORTUNITY, REQUIREMENTS, 2);
  const position = selected.experience_json.indexOf(experiences[9]);
  assert.deepEqual(selected.econ_experience_json.areas.map(area => area.source_record_index),
    [position, selected.experience_json.includes(experiences[0]) ? 0 : null, undefined]);
  assert.equal(selected.econ_experience_json.no_global_sum, true);
});
