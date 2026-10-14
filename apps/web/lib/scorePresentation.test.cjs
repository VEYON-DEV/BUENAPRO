const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");

const filename = path.join(__dirname, "scorePresentation.ts");
const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const source = new Module(filename, module);
source._compile(compiled, filename);
const { scoreTone } = source.exports;

test("score display bands include exact boundaries without rounding", () => {
  for (const [score, tone] of [[0, "rojo"], [29, "rojo"], [29.9, "rojo"], [30, "ambar"], [80, "ambar"], [80.1, "verde"], [81, "verde"], [100, "verde"]]) {
    assert.equal(scoreTone(score), tone);
  }
});

test("unknown, nonnumeric or out-of-range values stay gray and never become zero", () => {
  for (const score of [null, undefined, NaN, Infinity, -Infinity, -1, 101, true, false, "0", "95", "", [], {}, { score: 95 }]) {
    assert.equal(scoreTone(score), "gris");
  }
});
