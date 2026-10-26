const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const cache = new Map();

function load(file, mocks = {}) {
  const absolute = path.resolve(__dirname, file);
  if (cache.has(absolute) && !Object.keys(mocks).length) return cache.get(absolute);
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(absolute, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  vm.runInNewContext(compiled, { exports, URLSearchParams, Response, Buffer, process: { env: {} }, require(name) {
    if (name in mocks) return mocks[name];
    if (name.endsWith('.json')) return require(path.resolve(path.dirname(absolute), name));
    if (name === '@/lib/commercialTaxonomy') return load('../../lib/commercialTaxonomy.ts');
    if (name === './commercialClassification') return load('commercialClassification.ts');
    if (name === './prod2CommercialAffinity') return load('prod2CommercialAffinity.ts');
    if (name === 'node:crypto') return require(name);
    throw new Error(`Unexpected offline test module: ${name}`);
  } });
  if (!Object.keys(mocks).length) cache.set(absolute, exports);
  return exports;
}

module.exports = { loadCommercialModule: load, commercial: load('../../lib/commercialTaxonomy.ts'), commercialSql: load('commercialClassification.ts') };
