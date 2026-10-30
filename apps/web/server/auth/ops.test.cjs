const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const ADMIN = '5425c92d-861b-4404-9c29-5f5360eeaf18';
const OTHER = '9f6ec18b-9e9b-41d9-ae7c-83d9d7e0d197';
const NOT_FOUND = Symbol('NEXT_NOT_FOUND');

/** Carga ops.ts (y su regla opsAccess.ts, real) con la sesión, notFound y NextResponse simulados. */
function loadGate({ session, sessionError = false, env = {} }) {
  const loaded = new Map();
  function load(file) {
    if (loaded.has(file)) return loaded.get(file);
    const exports = {};
    loaded.set(file, exports);
    const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    vm.runInNewContext(code, {
      exports, Object, Set, Map, Promise, Error, console,
      process: { env },
      require(name) {
        if (name.startsWith('.')) return load(path.resolve(path.dirname(file), name));
        if (name === 'next/navigation') return { notFound() { throw NOT_FOUND; } };
        if (name === 'next/server') {
          return { NextResponse: { json: (body, init) => ({ body, status: init.status, headers: init.headers }) } };
        }
        if (name === 'next-auth') {
          return { getServerSession: async () => { if (sessionError) throw new Error('JWT_SESSION_ERROR'); return session; } };
        }
        if (name === '@/server/auth/options') return { authOptions: {} };
        throw new Error(`Unexpected module: ${name}`);
      },
    });
    return exports;
  }
  return load(path.join(__dirname, 'ops.ts'));
}

const production = { NODE_ENV: 'production', OPS_ADMIN_USER_IDS: ADMIN };

test('requireOpsAdmin deja pasar al administrador de la lista', async () => {
  const gate = loadGate({ session: { user: { id: ADMIN } }, env: production });
  assert.equal(await gate.requireOpsAdmin(), ADMIN);
});

test('requireOpsAdmin: otro usuario con sesión recibe notFound', async () => {
  const gate = loadGate({ session: { user: { id: OTHER } }, env: production });
  await assert.rejects(gate.requireOpsAdmin(), (error) => error === NOT_FOUND);
});

test('requireOpsAdmin: sin sesión recibe notFound', async () => {
  const gate = loadGate({ session: null, env: production });
  await assert.rejects(gate.requireOpsAdmin(), (error) => error === NOT_FOUND);
});

test('requireOpsAdmin: una sesión ilegible cuenta como sin sesión (notFound, no un error 500)', async () => {
  const gate = loadGate({ sessionError: true, env: production });
  await assert.rejects(gate.requireOpsAdmin(), (error) => error === NOT_FOUND);
});

test('requireOpsAdmin: con la variable ausente nadie entra', async () => {
  const gate = loadGate({ session: { user: { id: ADMIN } }, env: { NODE_ENV: 'production' } });
  await assert.rejects(gate.requireOpsAdmin(), (error) => error === NOT_FOUND);
});

test('las APIs responden 404 JSON (nunca 401 ni 403) a quien no es administrador, con o sin sesión', async () => {
  for (const session of [null, { user: { id: OTHER } }]) {
    const gate = loadGate({ session, env: production });
    const blocked = await gate.opsApiGuard();
    assert.equal(blocked.status, 404);
    assert.equal(blocked.body.error, 'Not found');
    assert.deepEqual(Object.keys(blocked.body), ['error']);
    assert.equal(blocked.headers['Cache-Control'], 'no-store');
  }
});

test('las APIs dejan pasar al administrador (la guarda devuelve null)', async () => {
  const gate = loadGate({ session: { user: { id: ADMIN } }, env: production });
  assert.equal(await gate.opsApiGuard(), null);
});

test('DEV_OPS_USER_ID funciona sin sesión fuera de producción', async () => {
  const dev = { NODE_ENV: 'development', OPS_ADMIN_USER_IDS: ADMIN, DEV_OPS_USER_ID: ADMIN };
  const gate = loadGate({ session: null, env: dev });
  assert.equal(await gate.requireOpsAdmin(), ADMIN);
  assert.equal(await gate.opsApiGuard(), null);
});

test('DEV_OPS_USER_ID se ignora en producción aunque esté definido', async () => {
  const gate = loadGate({ session: null, env: { ...production, DEV_OPS_USER_ID: ADMIN } });
  await assert.rejects(gate.requireOpsAdmin(), (error) => error === NOT_FOUND);
  assert.equal((await gate.opsApiGuard()).status, 404);
});
