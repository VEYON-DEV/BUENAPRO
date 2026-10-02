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
