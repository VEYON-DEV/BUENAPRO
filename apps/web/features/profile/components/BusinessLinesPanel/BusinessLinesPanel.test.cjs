const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const webRoot = path.resolve(__dirname, "../../../..");
const loaded = new Map();
function loadSource(filename) {
  if (loaded.has(filename)) return loaded.get(filename).exports;
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const local = new Module(filename, module);
  local.filename = filename;
  local.paths = Module._nodeModulePaths(path.dirname(filename));
  const normalRequire = local.require.bind(local);
  local.require = specifier => {
    if (specifier.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => key === "__esModule" ? false : String(key) });
    if (specifier === "@/components/ui/Button") return { Button: ({ children, variant: _variant, ...props }) => React.createElement("button", props, children) };
    if (specifier === "@/components/ui/IconButton") return loadSource(path.join(webRoot, "components/ui/IconButton/IconButton.tsx"));
    if (specifier === "@/components/ui/Input") return { Input: props => React.createElement("input", props) };
    if (specifier === "@/lib/api/client") return { apiFetch: async () => { throw new Error("SSR must not fetch"); } };
    if (specifier === "../../model/lineFavorite") return loadSource(path.join(webRoot, "features/profile/model/lineFavorite.ts"));
    return normalRequire(specifier);
  };
  loaded.set(filename, local);
  local._compile(compiled, filename);
  return local.exports;
}

const { BusinessLinesPanel } = loadSource(path.join(__dirname, "BusinessLinesPanel.tsx"));
const render = lines => renderToStaticMarkup(React.createElement(BusinessLinesPanel, { lines, catalogs: [{ codigo: "43", nombre: "Tecnología", enabled: true }] }));
const line = overrides => ({ id: "11111111-1111-1111-1111-111111111111", nombre: "Desarrollo de software", cubso_segmentos: ["43"], keywords: ["software", "licencia de software"], is_active: true, ...overrides });
const HINT = "Las licitaciones mayores de tus líneas prioritarias aparecen primero.";

test("each line has a priority toggle with a pressed state and the accessible name «Línea prioritaria»", () => {
  const favorite = line({ id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", nombre: "Redes", is_favorite: true });
  const plain = line({ id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", nombre: "Cloud", is_favorite: false });
  const legacy = line({ id: "cccccccc-cccc-cccc-cccc-cccccccccccc", nombre: "Soporte" });
  const html = render([favorite, plain, legacy]);
  const buttons = [...html.matchAll(/<button([^>]*aria-pressed="(true|false)"[^>]*)>([\s\S]*?)<\/button>/g)];
  assert.deepEqual(buttons.map(match => match[2]), ["true", "false", "false"], "a row without the column is not a priority line");
  // The name is explicit (the tooltip in `title` only describes the action), so no tool reads the title as the name.
  for (const [, attributes] of buttons) assert.match(attributes, /aria-label="Línea prioritaria"/);
  // The star is filled only when marked, and the toggle is described by its line title.
  assert.match(buttons[0][3], /<svg[^>]*fill="currentColor"/);
  assert.match(buttons[1][3], /<svg[^>]*fill="none"/);
  for (const { id } of [favorite, plain, legacy]) {
    assert.match(html, new RegExp(`aria-describedby="line-title-${id}"`));
    assert.match(html, new RegExp(`<h3 id="line-title-${id}">`));
  }
  assert.match(html, /title="Quitar de las líneas prioritarias"/);
  assert.equal((html.match(/title="Marcar como línea prioritaria"/g) ?? []).length, 2);
  assert.match(html, /type="button"/);
});

test("the panel tells what priority does and labels the marked lines", () => {
  const html = render([line({ is_favorite: true, nombre: "Marcada" }), line({ id: "22222222-2222-2222-2222-222222222222", nombre: "Normal" })]);
  assert.ok(html.includes(HINT));
  const [marked, normal] = html.split("</article>");
  assert.match(marked, /2 keywords activas · Prioritaria/);
  assert.doesNotMatch(normal, /Prioritaria<|· Prioritaria/);
  // No lines, nothing to prioritize: no hint, and the first-line prompt stays.
  const empty = render([]);
  assert.ok(!empty.includes(HINT));
  assert.match(empty, /Aún no tienes líneas de negocio/);
});
