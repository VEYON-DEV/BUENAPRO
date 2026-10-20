const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const webRoot = path.resolve(__dirname, '../../..');
const ts = require(path.join(webRoot, 'node_modules/typescript'));
const cache = new Map();
function load(file) {
  if (!path.extname(file)) file = fs.existsSync(file + '.ts') ? file + '.ts'
    : fs.existsSync(file + '.tsx') ? file + '.tsx' : path.join(file, 'index.ts');
  if (cache.has(file)) return cache.get(file).exports;
  const mod = new Module(file, module); mod.filename = file;
  mod.paths = Module._nodeModulePaths(path.dirname(file)); cache.set(file, mod);
  const native = mod.require.bind(mod);
  mod.require = spec => {
    if (spec.endsWith('.module.css')) return new Proxy({}, { get: (_, key) => key === '__esModule' ? false : String(key) });
    if (spec === 'next/link') return ({ href, children, ...props }) => React.createElement('a', { href, ...props }, children);
    if (spec === 'next/navigation') return { useRouter: () => ({ refresh() {} }) };
    if (spec.startsWith('@/')) return load(path.join(webRoot, spec.slice(2)));
    if (spec.startsWith('.') && !spec.endsWith('.json')) return load(path.resolve(path.dirname(file), spec));
    return native(spec);
  };
  const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  }}).outputText;
  mod._compile(output, file); return mod.exports;
}
const model = load(path.join(__dirname, 'commercialFilters.ts'));
const { CommercialFilterFields } = load(path.join(webRoot, 'features/opportunities/components/CommercialFilterFields'));
const { OpportunityToolbar } = load(path.join(webRoot, 'features/opportunities/components/OpportunityToolbar'));
const { Prod4OpportunityToolbar } = load(path.join(webRoot, 'features/opportunities/components/Prod4OpportunityToolbar'));
const { OpportunitySourceTabs } = load(path.join(webRoot, 'features/opportunities/components/OpportunitySourceTabs'));
const { CommercialCategoryLabel } = load(path.join(webRoot, 'features/opportunities/components/CommercialCategoryLabel'));
const { Prod4OpportunityList } = load(path.join(webRoot, 'features/opportunities/components/Prod4OpportunityList'));
const selected = { source: 'prod2', profile_id: 'profile-1', q: 'tableros', page: '3', with_score: 'true',
  commercial_area: 'inteligencia_artificial,industrial_electrica', commercial_work: 'licencias_suscripciones',
  commercial_specialty: 'chatbot', commercial_status: 'needs_review' };
function render(Component, props) { return renderToStaticMarkup(React.createElement(Component, props)); }
function hrefs(html) { return [...html.matchAll(/href="([^"]+)"/g)].map(match => new URL(match[1].replaceAll('&amp;', '&'), 'http://qa.invalid')); }

test('commercial selection only keeps known unique IDs; clearing retains unrelated source and profile', () => {
  assert.deepEqual(model.commercialFilterValues({ commercial_area: 'industrial_electrica,invalid,industrial_electrica' }, 'commercial_area'), ['industrial_electrica']);
  assert.equal(model.commercialSelectionCount(selected), 5);
  const url = new URL(model.clearCommercialFiltersHref(selected), 'http://qa.invalid');
  assert.equal(url.searchParams.get('source'), 'prod2');
  assert.equal(url.searchParams.get('profile_id'), 'profile-1');
  assert.equal(url.searchParams.get('q'), 'tableros');
  assert.equal(url.searchParams.get('with_score'), 'true');
  assert.equal(url.searchParams.has('page'), false);
  for (const name of ['commercial_area', 'commercial_work', 'commercial_specialty', 'commercial_status']) assert.equal(url.searchParams.has(name), false);
});

test('one category selector exposes exactly the six approved choices and preserves selected value', () => {
  const html = render(CommercialFilterFields, { defaults: { commercial_category: 'industrial' } });
  for (const label of ['Categoría', 'Todas', 'Licencias y suscripciones', 'Desarrollo de software', 'IA y automatización', 'Industrial', 'Cloud e infraestructura', 'Soporte y mantenimiento']) assert.ok(html.includes(label), label);
  assert.equal((html.match(/<select\b/g) || []).length, 1);
  assert.equal((html.match(/<option\b/g) || []).length, 7);
  assert.match(html, /value="industrial" selected=""/);
  assert.match(html, /<select[^>]*name="commercial_category"/);
  assert.doesNotMatch(html, /type="checkbox"|Estado de clasificación|Tipo de trabajo|Especialidad|name="commercial_area"/);
  assert.equal(model.commercialSelectionCount({ commercial_category: 'industrial' }), 1);
  assert.equal(model.commercialCategorySelection({ commercial_category: 'industrial' }), 'industrial');
  assert.equal(model.commercialCategorySelection({}), '');
});

test('previous advanced bookmarks retain their exact hidden filters without expanding a complex UI', () => {
  const html = render(Prod4OpportunityToolbar, { source: 'prod2', defaults: selected });
  assert.ok(html.includes('Selección anterior'));
  assert.match(html, /name="commercial_area"[^>]*value="inteligencia_artificial,industrial_electrica"/);
  assert.match(html, /aria-controls="prod2-commercial-filters" aria-expanded="true"/);
  assert.doesNotMatch(html, /type="checkbox"|Tipo de trabajo|Estado de clasificación/);
  assert.equal((html.match(/<form\b/g) || []).length, 2);
  const search = html.slice(html.indexOf('<form'), html.indexOf('</form>'));
  for (const name of ['commercial_area', 'commercial_work', 'commercial_specialty', 'commercial_status']) assert.ok(search.includes(`name="${name}"`), name);
  const filterForm = html.slice(html.lastIndexOf('<form'));
  assert.doesNotMatch(filterForm, /name="page"/);
  assert.equal(model.commercialCategorySelection(selected), 'previous');
  assert.equal(model.commercialCategorySelection({ commercial_category: 'industrial,licencias_suscripciones' }), 'previous');
});

test('simple category survives both source forms and navigation; clear removes it while retaining profile', () => {
  const simple = { source: 'prod2', profile_id: 'profile-1', q: 'tableros', with_score: 'true', page: '3', commercial_category: 'industrial' };
  for (const [Component, props] of [[Prod4OpportunityToolbar, { source: 'prod2', defaults: simple }], [OpportunityToolbar, { defaults: { ...simple, source: 'prod6' } }]]) {
    const html = render(Component, props);
    const search = html.slice(html.indexOf('<form'), html.indexOf('</form>'));
    assert.match(search, /name="commercial_category"[^>]*value="industrial"/);
    for (const url of hrefs(html).filter(url => url.searchParams.has('commercial_category'))) {
      assert.equal(url.searchParams.get('commercial_category'), 'industrial');
      assert.equal(url.searchParams.get('profile_id'), 'profile-1');
      assert.equal(url.searchParams.has('page'), false);
    }
  }
  const urls = hrefs(render(OpportunitySourceTabs, { source: 'prod2', profileId: 'profile-1', commercialFilters: simple }));
  for (const url of urls) assert.equal(url.searchParams.get('commercial_category'), 'industrial');
  const cleared = new URL(model.clearCommercialFiltersHref(simple), 'http://qa.invalid');
  assert.equal(cleared.searchParams.has('commercial_category'), false);
  assert.equal(cleared.searchParams.get('profile_id'), 'profile-1');
  assert.equal(cleared.searchParams.get('q'), 'tableros');
});

test('SEACE object and score links preserve all commercial criteria while resetting page', () => {
  const html = render(Prod4OpportunityToolbar, { source: 'prod2', defaults: selected });
  const links = hrefs(html).filter(url => url.searchParams.has('object') || url.searchParams.get('with_score') !== 'true');
  assert.ok(links.length >= 2);
  for (const url of links.filter(url => url.searchParams.has('commercial_area'))) {
    assert.equal(url.searchParams.get('commercial_area'), selected.commercial_area);
    assert.equal(url.searchParams.get('commercial_work'), selected.commercial_work);
    assert.equal(url.searchParams.has('page'), false);
  }
});

test('minor contracts retain the commercial filter through search, quick views and type links', () => {
  const defaults = { ...selected, source: 'prod6', objeto: '2', deadline: 'all' };
  const html = render(OpportunityToolbar, { defaults });
  const firstForm = html.slice(html.indexOf('<form'), html.indexOf('</form>'));
  assert.match(firstForm, /name="commercial_area"[^>]*value="inteligencia_artificial,industrial_electrica"/);
  assert.match(html, /id="opportunity-filters"/);
  const closing = hrefs(html).find(url => url.searchParams.get('deadline') === '24h');
  assert.ok(closing); assert.equal(closing.searchParams.get('commercial_work'), selected.commercial_work);
  assert.equal(closing.searchParams.get('profile_id'), 'profile-1');
  const goods = hrefs(html).find(url => url.searchParams.get('objeto') === '1');
  assert.ok(goods); assert.equal(goods.searchParams.get('commercial_specialty'), 'chatbot');
});

test('switching sources keeps the same commercial filters and profile without carrying pagination', () => {
  const html = render(OpportunitySourceTabs, { source: 'prod2', profileId: 'profile-1', withScore: true, commercialFilters: selected });
  const urls = hrefs(html); assert.equal(urls.length, 2);
  for (const url of urls) {
    assert.equal(url.searchParams.get('commercial_area'), selected.commercial_area);
    assert.equal(url.searchParams.get('commercial_status'), 'needs_review');
    assert.equal(url.searchParams.get('profile_id'), 'profile-1');
    assert.equal(url.searchParams.get('with_score'), 'true');
    assert.equal(url.searchParams.has('page'), false);
  }
  assert.equal(urls[0].searchParams.get('source'), 'prod2');
  assert.equal(urls[1].searchParams.has('source'), false);
});

test('default filters stay collapsed; the retired PROD4 toolbar receives no new commercial control', () => {
  const html = render(Prod4OpportunityToolbar, { source: 'prod2', defaults: {} });
  assert.match(html, /aria-expanded="false"/); assert.doesNotMatch(html, /<h3>Clasificación comercial/);
  const old = render(Prod4OpportunityToolbar, { source: 'prod4', defaults: {} });
  assert.doesNotMatch(old, /prod2-commercial-filters|commercial_area|Filtros/);
});

test('title category is a compact neutral label with accessible complete labels and preliminary origin', () => {
  const html = render(CommercialCategoryLabel, { commercial_categories: ['licencias_suscripciones', 'soporte_mantenimiento'],
    commercial_classification_source: 'title', commercial_classification_state: 'classified', compact: true });
  assert.match(html, />Licencias y suscripciones</); assert.match(html, />\+1</); assert.match(html, />Por título</);
  assert.match(html, /aria-label="Licencias y suscripciones; Soporte y mantenimiento\. Por título: clasificación preliminar/);
  const expanded = render(CommercialCategoryLabel, { commercial_categories: ['industrial', 'ia_automatizacion'], commercial_classification_source: 'document', commercial_classification_state: 'classified' });
  assert.match(expanded, />Industrial</); assert.match(expanded, />IA y automatización</); assert.doesNotMatch(expanded, /Por título/);
  for (const [state, label] of [['unclassified', 'Por clasificar'], ['other', 'Otros'], ['unclear', 'Por confirmar']]) {
    assert.ok(render(CommercialCategoryLabel, { commercial_classification_state: state }).includes(label));
  }
});

test('only PROD2 receives a category column; PROD4 retains its existing list contract', () => {
  const row = { id_convocatoria: '123', id_procedimiento: '456', nomenclatura: 'LP-2026', title: 'Licencias completas', object_type: 'good',
    commercial_categories: ['licencias_suscripciones'], commercial_classification_source: 'title', commercial_classification_state: 'classified' };
  const current = render(Prod4OpportunityList, { rows: [row], source: 'prod2', profileId: 'DIN' });
  assert.match(current, />Categoría</); assert.match(current, />Licencias y suscripciones</); assert.match(current, />Por título</);
  const retired = render(Prod4OpportunityList, { rows: [row], source: 'prod4' });
  assert.doesNotMatch(retired, />Categoría<|>Por título<|>Licencias y suscripciones</);
});

test('explicit listing-only publication stays visibly unread without claiming a consulted schedule or zero official documents', () => {
  const row = { id_convocatoria: '123', id_procedimiento: '456', nomenclatura: 'LP-2026', title: 'Tableros eléctricos', object_type: 'good',
    has_official_detail: false, documents_count: 0, publication_status: 'rejected_below_60', publication_status_label: 'Descartada por JEV' };
  const html = render(Prod4OpportunityList, { rows: [row], source: 'prod2' });
  assert.match(html, /Solo datos del listado/); assert.match(html, /Ficha por consultar/);
  assert.match(html, /Descartada por JEV/); assert.match(html, /Por leer/);
  assert.doesNotMatch(html, /Cronograma consultado|0 documentos/);
});
