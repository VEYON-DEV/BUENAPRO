const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function loadDb(pool) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, 'db.ts'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(code, {
    exports, Promise, Error, console,
    require(name) {
      if (name === '@/server/db/client') return { pool };
      throw new Error(`Unexpected module: ${name}`);
    },
  });
  return exports;
}

function fakePool({ failOn } = {}) {
  const calls = [];
  const state = { released: 0 };
  const client = {
    async query(text, values) {
      calls.push(values === undefined ? text : [text, values]);
      if (failOn && text.includes(failOn)) throw new Error('boom');
      return { rows: text.startsWith('SELECT') ? [{ n: 1 }] : [] };
    },
    release() { state.released += 1; },
  };
  return { calls, state, pool: { connect: async () => client } };
}

test('cada consulta del panel corre en una transacción de solo lectura con tiempo límite', async () => {
  const { pool, calls, state } = fakePool();
  const { opsQuery, OPS_STATEMENT_TIMEOUT_MS } = loadDb(pool);
  const rows = await opsQuery('SELECT 1 AS n WHERE $1::int = 1', [1]);
  assert.equal(rows[0].n, 1);
  assert.equal(calls[0], 'BEGIN READ ONLY');
  assert.equal(calls[1], `SET LOCAL statement_timeout = ${OPS_STATEMENT_TIMEOUT_MS}`);
  assert.equal(calls[2][0], 'SELECT 1 AS n WHERE $1::int = 1');
  assert.equal(calls[2][1][0], 1);
  assert.equal(calls[3], 'COMMIT');
  assert.equal(state.released, 1);
  assert.ok(OPS_STATEMENT_TIMEOUT_MS <= 30000);
});

test('si la consulta falla se deshace la transacción y la conexión se devuelve al pool', async () => {
  const { pool, calls, state } = fakePool({ failOn: 'SELECT boom' });
  const { opsQuery } = loadDb(pool);
  await assert.rejects(opsQuery('SELECT boom'), /boom/);
  assert.ok(calls.includes('ROLLBACK'));
  assert.ok(!calls.includes('COMMIT'));
  assert.equal(state.released, 1);
});
