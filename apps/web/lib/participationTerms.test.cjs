const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const api = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(`${__dirname}/participationTerms.ts`, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: api });
const clause = { status: 'permitted', clause: 'Se permite subcontratar previa autorización.', page: 12, conditions: [] };
const extraction = (term, review = false) => ({ id: '72', quality: 'auto', requires_human_review: review,
  participation: { subcontratacion: term }, page_scope: 'combined' });

test('missing or failed reading stays pending instead of using preserved canonical permission', () => {
  for (const value of [null, {}, { subcontracting_status: 'permitted' }, { ...extraction(clause), quality: 'failed' }]) {
    assert.equal(api.participationReading(value).subcontratacion.status, 'pending');
  }
});
test('permission requires a quoted clause and a valid integer page', () => {
  for (const override of [{ clause: '' }, { clause: null }, { page: null }, { page: '12' }, { page: true }, { page: 0 }, { page: 1.5 }, { status: 'no_aplica' }]) {
    assert.equal(api.participationReading(extraction({ ...clause, ...override })).subcontratacion.status, 'not_identified');
  }
  assert.equal(api.participationReading(extraction(clause)).subcontratacion.status, 'permitted');
});
test('conditions constrain permission and keep their text without inventing a percentage', () => {
  const term = api.participationReading(extraction({ ...clause, conditions: [' Autorización previa. ', '', null, 'Solo actividades accesorias.'] })).subcontratacion;
  assert.equal(term.status, 'conditional');
  assert.deepEqual(Array.from(term.conditions), ['Autorización previa.', 'Solo actividades accesorias.']);
});
test('review remains distinct from the extracted classification and does not assert confirmation', () => {
  const term = api.participationReading(extraction({ ...clause, status: 'prohibited' }, true)).subcontratacion;
  assert.equal(term.status, 'prohibited'); assert.equal(term.needsReview, true);
  assert.match(api.participationHint(term), /Requiere revisión humana/);
  const missingFlag = extraction(clause); delete missingFlag.requires_human_review;
  assert.equal(api.participationReading(missingFlag).subcontratacion.needsReview, true);
});
test('consortium is independent and combined page numbering remains explicit', () => {
  const reading = api.participationReading({ ...extraction(clause), participation: {
    subcontratacion: { ...clause, status: 'prohibited' }, consorcio: { ...clause, status: 'conditional' } } });
  assert.equal(reading.consorcio.status, 'conditional'); assert.equal(reading.subcontratacion.status, 'prohibited');
  assert.equal(reading.pageScope, 'combined');
  assert.equal(api.participationReading({ ...extraction(clause), page_scope: 'document' }).pageScope, 'document');
  assert.equal(api.participationLabel({ ...reading.consorcio, status: 'permitted' }, 'consorcio'), 'Permitido');
  assert.match(api.participationHint({ ...reading.consorcio, status: 'prohibited' }, 'consorcio'), /^No permitido según/);
});
