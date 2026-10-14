const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

test('detail and list SQL expose a process-specific official SEACE URL', async () => {
  const statements = [];
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(`${__dirname}/prod4Opportunities.ts`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports,
    require(name) {
      if (name === '@/server/db/client') return { query: async (sql) => {
        statements.push(sql);
        return { rows: sql.includes('count(*)::int AS total') ? [{ total: 0 }] : [] };
      }};
      if (name === '@/lib/procurementSchedule') return { normalizeProd4Schedule: () => [] };
      if (name === '@/lib/prod4Analysis') return {};
      throw new Error(`Unexpected module: ${name}`);
    },
  });
  await exports.getProd4Opportunity('1254603');
  await exports.listProd4Opportunities({ object: null, q: '', state: 'current', page: 1, pageSize: 20 });
  const selections = statements.filter(sql => sql.includes('AS source_url'));
  assert.equal(selections.length, 2);
  for (const sql of selections) {
    assert.match(sql, /'https:\/\/prod4\.seace\.gob\.pe\/openegocio\/#\/ficha\/idProceso\/' \|\| p\.id_procedimiento::text/);
    assert.doesNotMatch(sql, /georeferenciacion/);
  }
});

test('PROD4 list and detail select compact participation bound to the latest current procedure extraction', async () => {
  const statements = [], exports = {};
  const reading = { id: '41', quality: 'auto', requires_human_review: true,
    participation: { consorcio: { status: 'conditional', clause: 'Se permite con condiciones.', page: 8, conditions: ['Máximo dos integrantes.'] } },
    page_scope: 'document' };
  const compiled = ts.transpileModule(fs.readFileSync(`${__dirname}/prod4Opportunities.ts`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports,
    require(name) {
      if (name === '@/server/db/client') return { query: async (sql, values) => {
        statements.push({ sql, values });
        return { rows: sql.includes('count(*)::int AS total') ? [{ total: 1 }]
          : sql.includes('AS participation_extraction') ? [{ participation_extraction: reading }] : [] };
      } };
      if (name === '@/lib/procurementSchedule') return { normalizeProd4Schedule: () => [] };
      if (name === '@/lib/prod4Analysis') return {};
      throw new Error(`Unexpected module: ${name}`);
    },
  });
  const params = { object: null, q: '', state: 'current', page: 1, pageSize: 20 };
  assert.equal((await exports.listProd4Opportunities(params)).data[0].participation_extraction, reading);
  assert.equal((await exports.listProd4OpportunitiesForTenant('tenant-qa', params)).data[0].participation_extraction, reading);
  assert.equal((await exports.getProd4Opportunity('1254603')).process.participation_extraction, reading);
  const selections = statements.filter(({ sql }) => sql.includes('AS participation_extraction'));
  assert.equal(selections.length, 3);
  for (const { sql } of selections) {
    assert.match(sql, /FROM prod4_document_extractions ex\s+WHERE ex\.id_procedimiento = p\.id_procedimiento AND ex\.is_current = true\s+ORDER BY ex\.created_at DESC, ex\.id DESC LIMIT 1/);
    assert.match(sql, /'id', ex\.id::text/);
    assert.match(sql, /'quality', ex\.quality/);
    assert.match(sql, /'requires_human_review', ex\.requires_human_review/);
    assert.match(sql, /COALESCE\(NULLIF\(ex\.summary_json->'participation', 'null'::jsonb\), ex\.raw_extraction_json->'participation'\)/);
    assert.match(sql, /'page_scope', CASE WHEN jsonb_array_length\(CASE\s+WHEN jsonb_typeof\(ex\.raw_extraction_json->'source_document_manifest'\) = 'array'/);
    assert.match(sql, /THEN ex\.raw_extraction_json->'source_document_manifest' ELSE '\[\]'::jsonb END\) > 1\s+THEN 'combined' ELSE 'document' END/);
    assert.doesNotMatch(sql, /SELECT ex\.\*|SELECT ex\.raw_extraction_json|INSERT|UPDATE|DELETE/);
  }
  for (const { sql } of selections.slice(0, 2)) {
    assert.doesNotMatch(sql, /o\.consortium_status|o\.subcontracting_status|o\.participation_terms_json/);
  }
});
