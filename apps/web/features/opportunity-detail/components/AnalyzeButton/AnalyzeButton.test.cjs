const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require(path.resolve(__dirname, "../../../../node_modules/typescript"));

function loadTsx(filename, resolve) {
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const local = new Module(filename, module);
  local.filename = filename;
  local.paths = Module._nodeModulePaths(path.dirname(filename));
  const standardRequire = local.require.bind(local);
  local.require = name => {
    if (name.endsWith(".module.css")) return new Proxy({}, { get: (_, key) => key === "__esModule" ? false : String(key) });
    return resolve(name) ?? standardRequire(name);
  };
  local._compile(source, filename);
  return local.exports;
}

let view = null;
let received = null;
const { Button, buttonClassName } = loadTsx(path.resolve(__dirname, "../../../../components/ui/Button/Button.tsx"), () => undefined);
const { AnalyzeButton } = loadTsx(path.join(__dirname, "AnalyzeButton.tsx"), name => {
  if (name === "next/link") return { __esModule: true, default: ({ href, children, ...rest }) => React.createElement("a", { href, ...rest }, children) };
  if (name === "@/components/ui/Button") return { Button, buttonClassName };
  if (name === "../../hooks/useManualGrading") return { useManualGrading: input => { received = input; return { view, submit: async () => {} }; } };
  return undefined;
});
const idle = { tone: "idle", label: "Calificar", disabled: false, message: null, usageText: "Hoy: 12 de 100 calificaciones manuales" };
const render = (props = {}, next = idle) => {
  view = next;
  return renderToStaticMarkup(React.createElement(AnalyzeButton, { id: 501, profileId: "p-1", ...props }));
};

test("a contract without a grade offers «Calificar» as the primary action with the daily usage", () => {
  const html = render();
  assert.match(html, /<button[^>]*>.*Calificar<\/button>/);
  assert.doesNotMatch(html, /disabled/);
  assert.match(html, /primary/);
  assert.match(html, /Hoy: 12 de 100 calificaciones manuales/);
  assert.deepEqual({ ...received }, { source: "contracts", id: 501, profileId: "p-1", regrade: false });
});

test("a graded contract is re-graded: regrade flows into the hook and the button is secondary", () => {
  const html = render({ analyzedAt: "2026-10-29T10:00:00Z" }, { ...idle, label: "Re-calificar" });
  assert.equal(received.regrade, true);
  assert.match(html, /Re-calificar/);
  assert.match(html, /secondary/);
});

test("an in-progress grading is disabled, busy and explains the wait", () => {
  const html = render({}, { tone: "active", label: "Calificación en curso…", disabled: true, message: "Leyendo los documentos; puede tardar unos minutos.", usageText: idle.usageText });
  assert.match(html, /<button[^>]*disabled=""/);
  assert.match(html, /aria-busy="true"/);
  assert.match(html, /Calificación en curso…/);
  assert.match(html, /Leyendo los documentos; puede tardar unos minutos\./);
  assert.match(html, /role="status"/);
});

test("a failure keeps the button enabled for a retry and shows the reason", () => {
  const html = render({}, { tone: "failed", label: "Reintentar calificación", disabled: false, message: "La licitación no tiene documentos disponibles para leer. Puedes intentarlo de nuevo.", usageText: idle.usageText });
  assert.doesNotMatch(html, /<button[^>]*disabled/);
  assert.match(html, /Reintentar calificación/);
  assert.match(html, /messageFailed/);
  assert.match(html, /no tiene documentos disponibles/);
});

test("the daily limit message is shown with a disabled button", () => {
  const html = render({}, { tone: "limit", label: "Calificar", disabled: true, message: "Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.", usageText: "Hoy: 100 de 100 calificaciones manuales" });
  assert.match(html, /<button[^>]*disabled=""/);
  assert.match(html, /messageLimit/);
  assert.match(html, /Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa\./);
  assert.match(html, /Hoy: 100 de 100/);
});

test("without an active profile the user gets a link to the profile, not a nested button", () => {
  const html = render({}, { tone: "no_profile", label: "Configura tu perfil para calificar", disabled: false, message: null, usageText: null });
  assert.match(html, /<a href="\/perfil"/);
  assert.match(html, /Configura tu perfil para calificar/);
  assert.doesNotMatch(html, /<button/);
});

test("an empty status region stays mounted so screen readers announce later changes", () => {
  const html = render({}, { ...idle, usageText: null });
  assert.match(html, /<div class="status" role="status" aria-live="polite"><\/div>/);
});

test("mayores passes its source to the hook and never re-grades", () => {
  const html = render({ source: "prod2", id: "123", canRegrade: false });
  assert.deepEqual({ ...received }, { source: "prod2", id: "123", profileId: "p-1", regrade: false });
  assert.match(html, /<button[^>]*>.*Calificar<\/button>/);
  assert.match(html, /primary/);
});

test("mayores with a grade already in place offers nothing, even if asked to", () => {
  const html = render({ source: "prod2", id: "123", canRegrade: false, analyzedAt: "2026-10-29T10:00:00Z" });
  assert.equal(html, "");
  assert.equal(received.regrade, false);
});

test("menores still re-grades by default when it has a grade", () => {
  render({ analyzedAt: "2026-10-29T10:00:00Z" });
  assert.deepEqual({ ...received }, { source: "contracts", id: 501, profileId: "p-1", regrade: true });
});

test("a mayores request in progress shows the worker hint under a disabled button", () => {
  const hint = "Leyendo los documentos de la convocatoria. Puede tardar varios minutos; si la búsqueda diaria está en curso (de madrugada), se atiende al terminar.";
  const html = render({ source: "prod2", id: "123", canRegrade: false }, { tone: "active", label: "Calificación en curso…", disabled: true, message: hint, usageText: idle.usageText });
  assert.match(html, /<button[^>]*disabled=""/);
  assert.match(html, /aria-busy="true"/);
  assert.ok(html.includes(hint));
});

test("an uncertain charge is a disabled button with a warning, not a retry", () => {
  const html = render({ source: "prod2", id: "123", canRegrade: false }, { tone: "blocked", label: "Calificar", disabled: true, message: "La calificación quedó en revisión por un cobro incierto; el equipo la revisará.", usageText: idle.usageText });
  assert.match(html, /<button[^>]*disabled=""/);
  assert.match(html, /messageLimit/);
  assert.match(html, /el equipo la revisará/);
  assert.doesNotMatch(html, /Reintentar/);
});
