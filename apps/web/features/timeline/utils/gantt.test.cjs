const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const exportsForTest = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(`${__dirname}/gantt.ts`, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports: exportsForTest, Date, Intl });
const utils = exportsForTest;

test('calendar coordinates preserve Lima day without synthesizing deadline hours', () => {
  assert.equal(utils.limaDay('2026-10-03T01:00:00Z'), '2026-10-02');
  assert.equal(utils.limaDay('2026-10-03'), '2026-10-03');
  assert.equal(utils.addDays('2026-12-31', 1), '2027-01-01');
  assert.equal(utils.validDay('2026-02-30'), false);
  assert.equal(utils.dayOffset('2026-10-03T01:00:00Z', '2026-10-02'), 0);
  assert.match(utils.formatBoundary('2026-10-03', 'day'), /hora no informada/);
});

test('all overlapping stages get independent tracks, clipped stages remain visible', () => {
  const stages = [
    {id:'registration', startsAt:'2026-09-01', endsAt:'2026-10-09'},
    {id:'questions', startsAt:'2026-10-02', endsAt:'2026-10-03'},
    {id:'answers', startsAt:'2026-10-03', endsAt:'2026-10-03'},
    {id:'missing', startsAt:null, endsAt:'2026-10-03'},
    {id:'reversed', startsAt:'2026-10-05', endsAt:'2026-10-02'},
  ];
  const result = utils.layoutStages(stages, '2026-10-02', 7);
  assert.equal(result.bars.length, 3);
  assert.equal(result.lanes, 3);
  assert.equal(result.bars.find(bar=>bar.stage.id==='registration').clippedStart, true);
  assert.equal(result.bars.find(bar=>bar.stage.id==='registration').clippedEnd, true);
  assert.equal(result.bars.find(bar=>bar.stage.id==='answers').end - result.bars.find(bar=>bar.stage.id==='answers').start, 1);
});
