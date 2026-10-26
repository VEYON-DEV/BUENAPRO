const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const { commercial, commercialSql, loadCommercialModule } = require('./commercialClassification.test-support.cjs');
const profileId = '00000000-0000-0000-0000-000000000001';

function serviceWithCurrentReading({ matchId = null, score = null, matchReviewRequired = false, provisionalScore = null,
  profileRows = [{ id: profileId, razon_social: 'Empresa QA' }] } = {}) {
  const statements = [], exports = {};
  const reading = { id: '72', quality: 'auto', requires_human_review: true,
    participation: { subcontratacion: { status: 'prohibited', clause: 'No se permite subcontratar.', page: 12, conditions: [] } },
    page_scope: 'document' };
  const compiled = ts.transpileModule(fs.readFileSync(`${__dirname}/contracts.ts`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports, URLSearchParams,
    require(name) {
      if (name === '@/lib/commercialTaxonomy') return commercial;
      if (name === './commercialClassification') return commercialSql;
      if (name === '@/server/db/client') return { query: async (sql, values) => {
        statements.push({ sql, values });
        if (sql.includes('SELECT id::text, razon_social FROM company_profiles')) return { rows: profileRows };
        // Canonical permission can be historical; the current extraction remains independently exposed.
        return { rows: sql.includes('FROM seace_contracts c')
          ? [{ id_contrato: 123, extraction_id: '72', subcontracting_status: 'permitted', participation_extraction: reading,
            match_id: matchId, score, match_review_required: matchReviewRequired, provisional_score: provisionalScore, total_count: 1 }] : [] };
      } };
      if (name === '@/server/services/crud') return { pagination: () => ({ limit: 20, offset: 0, page: 1, pageSize: 20 }) };
      if (name === '@/server/services/keywordScoring') return { keywordFitLateralSql: () => '' };
      throw new Error(`Unexpected module: ${name}`);
    },
  });
  return { service: exports, statements, reading };
}

test('contract list and detail expose only participation from their own latest current extraction', async () => {
  const { service, statements, reading } = serviceWithCurrentReading();
  const listed = await service.listContractsForTenant('tenant-qa', new URLSearchParams());
  const detail = await service.getContractForTenant('tenant-qa', 123);
  assert.equal(listed.data[0].participation_extraction, reading);
  assert.equal(detail.contract.participation_extraction, reading);
  assert.equal(detail.contract.participation_extraction.participation.subcontratacion.status, 'prohibited');
  const selections = statements.filter(({ sql }) => sql.includes('FROM seace_contracts c'));
  assert.equal(selections.length, 2);
  for (const { sql } of selections) {
    assert.match(sql, /te\.participation_extraction/);
    assert.match(sql, /JOIN tdr_extractions tx ON tx\.contract_document_id = d\.id AND tx\.is_current = true/);
    assert.match(sql, /WHERE d\.id_contrato = c\.id_contrato\s+ORDER BY tx\.created_at DESC, tx\.id DESC\s+LIMIT 1/);
    assert.match(sql, /'id', tx\.id::text/);
    assert.match(sql, /'quality', tx\.quality/);
    assert.match(sql, /'requires_human_review', tx\.requires_human_review/);
    assert.match(sql, /COALESCE\(NULLIF\(tx\.summary_json->'participation', 'null'::jsonb\), tx\.raw_extraction_json->'participation'\)/);
    assert.match(sql, /'page_scope', 'document'/);
    assert.doesNotMatch(sql, /INSERT|UPDATE|DELETE/);
  }
  const listSql = selections[0].sql;
  assert.doesNotMatch(listSql, /te\.raw_extraction_json|SELECT tx\.summary_json, tx\.raw_extraction_json/);
  assert.deepEqual(Array.from(selections[0].values), ['tenant-qa', profileId, 20, 0]);
  assert.deepEqual(Array.from(selections[1].values), [123, 'tenant-qa', profileId]);
  assert.match(selections[1].sql, /tx\.id AS extraction_id/);
  const facets = statements.find(({ sql }) => sql.includes('FROM requirement_facets') && !sql.includes('FROM seace_contracts c'));
  assert.match(facets.sql, /WHERE id_contrato = \$1 AND extraction_id = \$2 AND is_current = true/);
  assert.deepEqual(Array.from(facets.values), [123, '72']);
});

test('list and detail hide unvalidated model scores while legacy metadata keeps the stored score and verdict', async () => {
  const { service, statements } = serviceWithCurrentReading();
  await service.listContractsForTenant('tenant-qa', new URLSearchParams('verdict=gris'));
  await service.getContractForTenant('tenant-qa', 123);
  const selections = statements.filter(({ sql }) => sql.includes('FROM seace_contracts c'));
  assert.equal(selections.length, 2);
  for (const { sql } of selections) {
    assert.match(sql, /CASE WHEN m\.breakdown_json->'meta'->>'score_validated' = 'false' THEN NULL ELSE m\.score END AS score/);
    assert.match(sql, /CASE WHEN m\.breakdown_json->'meta'->>'score_validated' = 'false' THEN 'gris' ELSE m\.verdict END AS verdict/);
    assert.match(sql, /ORDER BY CASE WHEN m\.breakdown_json->'meta'->>'score_validated' = 'false' THEN NULL ELSE m\.score END DESC NULLS LAST/);
    assert.doesNotMatch(sql, /COALESCE\(m\.breakdown_json->'meta'->>'score_validated', 'false'\)/, 'absence of new flag never retroactively hides legacy scores');
  }
  assert.match(selections[0].sql, /CASE WHEN m\.breakdown_json->'meta'->>'score_validated' = 'false' THEN 'gris' ELSE m\.verdict END = \$3/);
});

test('minor analysis filter retains unpublished and zero scores while filtering matches before the shared page count', async () => {
  for (const scenario of [{ matchId: 916, score: null, matchReviewRequired: true },
    { matchId: 10, score: 0, matchReviewRequired: false }]) {
    const { service, statements } = serviceWithCurrentReading(scenario);
    const list = await service.listContractsForTenant('tenant-qa', new URLSearchParams('analysis=done&has_extraction=true'));
    const detail = await service.getContractForTenant('tenant-qa', 123);
    assert.equal(list.data[0].match_id, scenario.matchId);
    assert.equal(list.data[0].score, scenario.score);
    assert.equal(list.data[0].match_review_required, scenario.matchReviewRequired);
    assert.equal(detail.contract.match_review_required, scenario.matchReviewRequired);
    const selections = statements.filter(statement => statement.sql.includes('FROM seace_contracts c'));
    const listSql = selections[0].sql;
    assert.match(listSql, /WHERE cp\.tenant_id = \$1 AND m\.id_contrato = c\.id_contrato/);
    assert.match(listSql, /te\.summary_json IS NOT NULL AND m\.id IS NOT NULL/);
    assert.match(listSql, /count\(\*\) OVER\(\)::int AS total_count/);
    assert.ok(listSql.indexOf('m.id IS NOT NULL') < listSql.lastIndexOf('LIMIT $3'));
    assert.match(listSql, /COALESCE\(fit\.keyword_points, 0\) DESC/);
    assert.deepEqual(Array.from(selections[0].values), ['tenant-qa', profileId, 20, 0]);
    for (const { sql } of selections) {
      assert.match(sql, /COALESCE\(m\.breakdown_json->'meta'->>'score_validated' = 'false', false\) AS match_review_required/);
      assert.doesNotMatch(sql, /m\.score (?:>\s|IS NOT NULL)|COALESCE\(m\.score, 0\)/);
      assert.doesNotMatch(sql, /\b(?:INSERT|UPDATE|DELETE)\b/);
    }
  }
});

test('default minor browse preserves unanalyzed rows instead of implicitly requiring a match', async () => {
  const { service, statements } = serviceWithCurrentReading();
  const list = await service.listContractsForTenant('tenant-qa', new URLSearchParams());
  assert.equal(list.data[0].match_id, null);
  assert.equal(list.data[0].match_review_required, false);
  assert.doesNotMatch(statements[0].sql, /m\.id IS NOT NULL/);
});

test('minor provisional scores retain real zero and remain separate from protected final scores in list and detail', async () => {
  for (const score of [0, 95]) {
    const { service, statements } = serviceWithCurrentReading({ matchId: 916, score: null, matchReviewRequired: true, provisionalScore: score });
    const list = await service.listContractsForTenant('tenant-qa', new URLSearchParams('analysis=done'));
    const detail = await service.getContractForTenant('tenant-qa', 123);
    assert.equal(list.data[0].provisional_score, score);
    assert.equal(list.data[0].score, null);
    assert.equal(detail.contract.provisional_score, score);
    assert.equal(detail.contract.score, null);
    for (const { sql } of statements.filter(statement => statement.sql.includes('FROM seace_contracts c'))) {
      assert.match(sql, /CASE WHEN COALESCE\(m\.breakdown_json->'meta'->>'score_validated' = 'false', false\) AND m\.score >= 0 AND m\.score <= 100 THEN m\.score ELSE NULL END AS provisional_score/);
      assert.match(sql, /CASE WHEN m\.breakdown_json->'meta'->>'score_validated' = 'false' THEN NULL ELSE m\.score END AS score/);
      assert.doesNotMatch(sql, /COALESCE\(m\.score|raw_model_score.*AS provisional_score|\b(?:INSERT|UPDATE|DELETE)\b/);
    }
  }
});

test('minor provisional projection rejects missing matches, nonnumeric and out-of-range values while preserving legacy final scores', async () => {
  for (const scenario of [{ matchId: null, matchReviewRequired: true, provisionalScore: 95 },
    { matchId: 916, matchReviewRequired: false, provisionalScore: 95, score: 84 },
    ...[null, '95', NaN, Infinity, -1, 101].map(provisionalScore => ({ matchId: 916, matchReviewRequired: true, provisionalScore }))]) {
    const { service } = serviceWithCurrentReading(scenario);
    const list = await service.listContractsForTenant('tenant-qa', new URLSearchParams());
    const detail = await service.getContractForTenant('tenant-qa', 123);
    assert.equal(list.data[0].provisional_score, null);
    assert.equal(detail.contract.provisional_score, null);
    assert.equal(list.data[0].score, scenario.score ?? null);
    assert.equal(detail.contract.score, scenario.score ?? null);
  }
});

test('minor score-only filter includes stored zero and provisional numbers and runs before page count/limit', async () => {
  for (const score of [0, 51, 72, 100]) {
    const { service, statements } = serviceWithCurrentReading({ matchId: 916, matchReviewRequired: true, provisionalScore: score });
    const result = await service.listContractsForTenant('tenant-qa', new URLSearchParams(`profile_id=${profileId}&with_score=true&q=software`));
    assert.equal(result.data[0].provisional_score, score);
    assert.equal(result.profile.id, profileId);
    const page = statements.find(statement => statement.sql.includes('FROM seace_contracts c'));
    const available = 'CASE WHEN m.id IS NOT NULL AND m.score BETWEEN 0 AND 100 THEN m.score ELSE NULL END';
    assert.ok(page.sql.includes(`${available} IS NOT NULL`));
    assert.ok(page.sql.indexOf(`${available} IS NOT NULL`) < page.sql.lastIndexOf('LIMIT $4'));
    assert.match(page.sql, /ORDER BY\s+CASE WHEN m\.id IS NOT NULL AND m\.score BETWEEN 0 AND 100 THEN m\.score ELSE NULL END DESC NULLS LAST,/);
    assert.ok(page.sql.lastIndexOf('BETWEEN 0 AND 100 THEN m.score ELSE NULL END DESC NULLS LAST') < page.sql.lastIndexOf('COALESCE(fit.keyword_points, 0) DESC'));
    assert.match(page.sql, /cp\.is_active = true AND cp\.id = \$2::uuid/);
    assert.match(page.sql, /count\(\*\) OVER\(\)::int AS total_count/);
    assert.deepEqual(Array.from(page.values), ['tenant-qa', profileId, '%software%', 20, 0]);
    assert.doesNotMatch(page.sql, /COALESCE\(m\.score, 0\)|m\.score > 0/);
  }
});

test('minor browse and detail use one resolved active tenant-owned profile instead of another profile match', async () => {
  const { service, statements } = serviceWithCurrentReading();
  await service.listContractsForTenant('tenant-qa', new URLSearchParams(`profile_id=${profileId}`));
  await service.getContractForTenant('tenant-qa', 123, profileId);
  const guards = statements.filter(statement => statement.sql.includes('SELECT id::text, razon_social FROM company_profiles'));
  assert.equal(guards.length, 2);
  for (const statement of guards) {
    assert.match(statement.sql, /tenant_id = \$1 AND is_active = true AND \(\$2::uuid IS NULL OR id = \$2::uuid\)/);
    assert.deepEqual(Array.from(statement.values), ['tenant-qa', profileId]);
  }
  const detail = statements.filter(statement => statement.sql.includes('FROM seace_contracts c'))[1];
  assert.match(detail.sql, /cp\.tenant_id = \$2 AND m\.id_contrato = c\.id_contrato\s+AND cp\.is_active = true AND cp\.id = \$3::uuid/);
  assert.match(detail.sql, /\$3::uuid::text AS profile_id/);
  assert.deepEqual(Array.from(detail.values), [123, 'tenant-qa', profileId]);
});

test('foreign or inactive minor profiles fail before listing or detail; no active profile cannot borrow another score', async () => {
  for (const detail of [false, true]) {
    const { service, statements } = serviceWithCurrentReading({ profileRows: [] });
    await assert.rejects(detail ? service.getContractForTenant('tenant-qa', 123, profileId)
      : service.listContractsForTenant('tenant-qa', new URLSearchParams(`profile_id=${profileId}&with_score=true`)), error => error.status === 404);
    assert.equal(statements.length, 1);
  }
  const { service, statements } = serviceWithCurrentReading({ profileRows: [] });
  await service.listContractsForTenant('empty-tenant', new URLSearchParams('with_score=true'));
  const page = statements.find(statement => statement.sql.includes('FROM seace_contracts c'));
  assert.equal(page.values[1], null);
  assert.match(page.sql, /cp\.is_active = true AND cp\.id = \$2::uuid/);
});

test('invalid minor profile and score flags are rejected without executing the list SQL', async () => {
  for (const query of ['profile_id=not-uuid', 'with_score=all', 'with_score=true OR true']) {
    const { service, statements } = serviceWithCurrentReading();
    await assert.rejects(service.listContractsForTenant('tenant-qa', new URLSearchParams(query)), error => error.status === 400);
    assert.equal(statements.length, 0);
  }
});
