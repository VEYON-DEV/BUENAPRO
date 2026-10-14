const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const exportsObject = {};
const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, 'prod2OfficialDocument.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
vm.runInNewContext(code, { exports: exportsObject, URL, TextDecoder, AbortSignal,
  fetch: async () => { throw new Error('Unexpected real network request'); } });
const { resolveProd2OfficialDocument, prod2OfficialDocumentReference } = exportsObject;
const uuid = '00000000-0000-0000-0000-000000000010';
const safeBody = (downloadUrl) => JSON.stringify({ downloadUrl });
const mock = body => async () => new Response(body);

test('canonical reference contains only UUID and cannot carry tickets or arbitrary hosts', () => {
  assert.equal(prod2OfficialDocumentReference(uuid), `https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=${uuid}`);
  assert.equal(prod2OfficialDocumentReference('../private'), null);
});

test('JSON and exact JSONP resolve context-relative URLs without executing scripts', async () => {
  for (const wrap of [body => body, body => `buenaproDocument(${body});`]) {
    const url = await resolveProd2OfficialDocument(uuid, mock(wrap(safeBody('/service/api/node/content/file.pdf?ticket=fresh'))));
    assert.equal(url, 'https://alfprod.seace.gob.pe/alfresco/service/api/node/content/file.pdf?ticket=fresh');
  }
  assert.equal(await resolveProd2OfficialDocument(uuid, mock(safeBody('/alfresco/service/api/node/content/file.docx?ticket=fresh'))),
    'https://alfprod.seace.gob.pe/alfresco/service/api/node/content/file.docx?ticket=fresh');
  const requests = [];
  await resolveProd2OfficialDocument(uuid, async (url, options) => { requests.push({ url, options }); return new Response(safeBody('https://alfprod.seace.gob.pe/file.zip?ticket=fresh')); });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url.searchParams.get('id'), uuid);
  assert.equal(requests[0].url.searchParams.get('doc'), 'buenaproDocument');
  assert.equal(requests[0].url.searchParams.get('guest'), 'false');
  assert.equal(requests[0].options.redirect, 'error');
  assert.equal(requests[0].options.cache, 'no-store');
  assert.ok(requests[0].options.signal instanceof AbortSignal);
});

test('unsafe authority, protocol, port, credentials and fragment are refused with a fixed safe error', async () => {
  for (const url of ['https://evil.example/file.pdf', 'https://alfprod.seace.gob.pe.evil.example/file.pdf',
    'http://alfprod.seace.gob.pe/file.pdf', 'https://127.0.0.1/file.pdf', 'file:///private.pdf',
    'https://user:password@alfprod.seace.gob.pe/file.pdf', 'https://alfprod.seace.gob.pe:444/file.pdf',
    '//evil.example/file.pdf', 'https://alfprod.seace.gob.pe/file.pdf#ticket']) {
    await assert.rejects(resolveProd2OfficialDocument(uuid, mock(safeBody(url))), error => error.name === 'Error'
      && !error.message.includes(url) && error.message.includes('SEACE'));
  }
});

test('malformed metadata, other callbacks and executable suffixes never pass through', async () => {
  for (const body of ['<html>upstream secret</html>', 'null', '[]', '{}', safeBody(''),
    `evilCallback(${safeBody('/service/file.pdf')});`, `buenaproDocument(${safeBody('/service/file.pdf')}); alert('secret')`,
    JSON.stringify({ downloadUrl: 12 })]) {
    await assert.rejects(resolveProd2OfficialDocument(uuid, mock(body)), error => error.message.includes('SEACE') && !error.message.includes('secret'));
  }
});

test('HTTP denial, timeout, oversized metadata and invalid identity fail without binary fetches', async () => {
  let calls = 0;
  await assert.rejects(resolveProd2OfficialDocument('not-a-uuid', async () => { calls++; return new Response('{}'); }));
  assert.equal(calls, 0);
  for (const fetcher of [async () => new Response('secret denial', { status: 403 }),
    async () => { throw new DOMException('secret upstream', 'TimeoutError'); },
    async () => new Response('a'.repeat(65537)),
    async () => new Response('{}', { headers: { 'Content-Length': '65537' } })]) {
    await assert.rejects(resolveProd2OfficialDocument(uuid, fetcher), error => !error.message.includes('secret') && error.message.includes('SEACE'));
  }
});
