import test from "node:test";
import assert from "node:assert/strict";
import {
  COST_BUCKETS,
  EVENT_COLUMNS,
  PARAMS_CTE,
  STEPS,
  columnSteps,
  costsSql,
  countsSql,
  failureSteps,
  failuresSql,
  findStep,
  itemsSql,
  stepsFor,
} from "./steps.ts";
import { MAJOR_QUERIES, MINOR_QUERIES } from "./traceQueries.ts";

const WRITE = /\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|vacuum|call)\b/i;
const everySql = () => [
  ...STEPS.flatMap((step) => [step.events, step.failureEvents ?? ""]),
  countsSql(columnSteps("menores")),
  countsSql(columnSteps("mayores")),
  costsSql(),
  failuresSql(failureSteps("menores")),
  failuresSql(failureSteps("mayores")),
  ...STEPS.map((step) => itemsSql(step)),
  ...Object.values(MINOR_QUERIES),
  ...Object.values(MAJOR_QUERIES),
].filter(Boolean);

test("todo el SQL del panel es de solo lectura", () => {
  for (const sql of everySql()) {
    assert.doesNotMatch(sql, WRITE, sql.slice(0, 120));
    assert.match(sql.trimStart(), /^(WITH|SELECT)\b/i);
  }
});

test("el día se calcula en Lima y los límites de ventana se resuelven una sola vez en el CTE de parámetros", () => {
  assert.match(PARAMS_CTE, /\(\$1::date\)::timestamp AT TIME ZONE 'America\/Lima'/);
  assert.match(PARAMS_CTE, /\(\(\$2::date\) \+ 1\)::timestamp AT TIME ZONE 'America\/Lima'/);
  for (const placeholder of ["$1::date", "$2::date", "$3::uuid", "$4::int"]) {
    assert.equal(PARAMS_CTE.split(placeholder).length - 1 >= 1, true, placeholder);
  }
  for (const sql of [countsSql(columnSteps("menores")), costsSql(), itemsSql(STEPS[0]), failuresSql(failureSteps("menores"))]) {
    assert.match(sql, /^WITH p AS \(/);
    // Cada marcador aparece solo dentro del CTE: el resto del SQL usa `(SELECT … FROM p)`.
    const body = sql.slice(PARAMS_CTE.length + 5);
    assert.doesNotMatch(body, /\$\d/);
  }
});

test("cada paso compara la columna cruda contra la ventana (sargable) y agrupa por día de Lima", () => {
  for (const step of STEPS) {
    if (step.key === "manual_requests" || step.key === "manual_failed") {
      assert.match(step.events, /r\.lima_day BETWEEN \(SELECT d_from FROM p\) AND \(SELECT d_to FROM p\)/, step.key);
      continue;
    }
    assert.match(step.events, />= \(SELECT ts_from FROM p\)/, `${step.source}/${step.key}`);
    assert.match(step.events, /< \(SELECT ts_to FROM p\)/, `${step.source}/${step.key}`);
    assert.match(step.events, /AT TIME ZONE 'America\/Lima'/, `${step.source}/${step.key}`);
  }
});

test("el filtro de empresa solo está en los pasos por empresa; los compartidos lo ignoran", () => {
  for (const step of STEPS) {
    const filtered = step.events.includes("(SELECT profile_id FROM p)");
    assert.equal(filtered, !step.shared, `${step.source}/${step.key} shared=${step.shared}`);
  }
  const profileColumns: Record<string, string> = {
    "menores/jev_accepted": "d.profile_id",
    "menores/eval_auto": "m.profile_id",
    "mayores/eval_manual": "ev.profile_id",
    "mayores/manual_requests": "r.profile_id",
  };
  for (const [id, column] of Object.entries(profileColumns)) {
    const [source, key] = id.split("/");
    const step = findStep(source as "menores" | "mayores", key)!;
    assert.match(step.events, new RegExp(`\\(SELECT profile_id FROM p\\) IS NULL OR ${column.replace(".", "\\.")} = \\(SELECT profile_id FROM p\\)`), id);
  }
});

test("toda consulta de eventos devuelve las mismas columnas y en el mismo orden", () => {
  for (const step of STEPS) {
    const aliases = [...step.events.matchAll(/\bAS (item_id|ts|day|profile_id|score|state|reason|cost|ref|title)\b/g)].map((match) => match[1]);
    const perBranch = EVENT_COLUMNS.length;
    assert.equal(aliases.length % perBranch, 0, `${step.source}/${step.key}`);
    for (let offset = 0; offset < aliases.length; offset += perBranch) {
      assert.deepEqual(aliases.slice(offset, offset + perBranch), [...EVENT_COLUMNS], `${step.source}/${step.key}`);
    }
  }
});

test("el catálogo no repite claves y cada origen tiene su embudo", () => {
  const ids = STEPS.map((step) => `${step.source}/${step.key}`);
  assert.equal(new Set(ids).size, ids.length);
  assert.ok(stepsFor("menores").length > 0 && stepsFor("mayores").length > 0);
  assert.equal(findStep("menores", "jev_ge85"), null, "los pasos de mayores no existen en menores");
  assert.equal(findStep("mayores", "jev_ge85")?.key, "jev_ge85");
});

test("menores: definiciones del embudo", () => {
  const sql = (key: string) => findStep("menores", key)!.events;
  assert.match(sql("found"), /FROM seace_contracts c[\s\S]*c\.first_seen_at >=/);
  // Descartadas por filtro previo: rechazadas sin puntaje.
  assert.match(sql("prefilter_rejected"), /d\.decision = 'rejected' AND d\.score IS NULL/);
  // JEV: decididas por jev con puntaje; el estado «superseded» queda fuera por construcción.
  for (const [key, decision] of [["jev_accepted", "accepted"], ["jev_rejected", "rejected"]] as const) {
    assert.match(sql(key), new RegExp(`d\\.decided_by = 'jev' AND d\\.score IS NOT NULL AND d\\.decision = '${decision}'`));
    assert.match(sql(key), /d\.updated_at >=/);
  }
  assert.match(sql("jev_pending"), /d\.decision = 'pending'/);
  assert.match(sql("jev_pending"), /d\.created_at >=/);
  for (const key of ["jev_accepted", "jev_rejected", "jev_pending", "jev_errors", "prefilter_rejected"]) {
    assert.doesNotMatch(sql(key), /superseded/);
  }
  // Lecturas por tdr_extractions unido a contract_documents.
  for (const key of ["read_ok", "read_review", "read_failed"]) {
    assert.match(sql(key), /FROM tdr_extractions te JOIN contract_documents cd ON cd\.id = te\.contract_document_id/);
  }
  assert.match(sql("read_ok"), /te\.quality <> 'failed' AND NOT te\.requires_human_review/);
  assert.match(sql("read_review"), /te\.quality <> 'failed' AND te\.requires_human_review/);
  assert.match(sql("read_failed"), /te\.quality = 'failed'/);
  // Evaluaciones: automática si la disparó el worker, manual todo lo demás.
  assert.match(sql("eval_auto"), /match_events e JOIN matches m/);
  assert.match(sql("eval_auto"), /\(e\.event_type = 'automatic_analysis' OR COALESCE\(e\.payload->>'source', ''\) IN \('automatic', 'worker'\)\)/);
  assert.match(sql("eval_manual"), /NOT \(e\.event_type = 'automatic_analysis'/);
  // Trabajos fallidos.
  assert.match(sql("jobs_failed"), /j\.status IN \('dead', 'failed'\) AND j\.finished_at >=/);
  assert.match(sql("jobs_failed"), /j\.status = 'pending' AND j\.last_error IS NOT NULL/);
  assert.match(sql("jobs_failed"), /regexp_replace\(regexp_replace\(split_part\(j\.last_error, chr\(10\), 1\)/);
  // Calificaciones manuales de PROD6.
  assert.match(sql("manual_requests"), /r\.source_key = 'seace_prod6'/);
});

test("mayores: definiciones del embudo", () => {
  const sql = (key: string) => findStep("mayores", key)!.events;
  assert.match(sql("searches_ok"), /FROM prod2_keyword_tasks t[\s\S]*t\.status = 'complete'[\s\S]*t\.last_completed_at >=/);
  assert.match(sql("searches_incomplete"), /t\.status IN \('partial', 'blocked'\)/);
  assert.match(sql("found"), /FROM prod2_discoveries p[\s\S]*p\.first_seen_at >=/);
  // JEV de unión: política, estado y rangos 85 / 60–84 / <60.
  for (const key of ["jev_ge85", "jev_60_84", "jev_lt60"]) {
    assert.match(sql(key), /ev\.kind = 'jev_affinity' AND ev\.status = 'done' AND ev\.policy_version = 'prod2_commercial_union_v1'/);
    assert.match(sql(key), /ev\.completed_at >=/);
    assert.match(sql(key), /ev\.result_json->>'affinity_index'/);
  }
  assert.match(sql("jev_ge85"), />= 85\s*$/);
  assert.match(sql("jev_60_84"), />= 60 AND [\s\S]*< 85\s*$/);
  assert.match(sql("jev_lt60"), /< 60\s*$/);
  // Lecturas.
  assert.match(sql("read_ok"), /FROM prod2_document_extractions x/);
  assert.match(sql("read_failed"), /ev\.kind = 'gemini_extraction' AND ev\.status = 'failed'/);
  assert.match(sql("read_failed"), /x\.quality = 'failed'/);
  assert.match(sql("read_uncertain"), /ev\.status IN \('uncertain', 'pending', 'running'\)/);
  // Evaluaciones: origen automático vs manual (en result_json o en breakdown.meta).
  assert.match(sql("eval_auto"), /ev\.kind = 'gemini_match'/);
  assert.match(sql("eval_auto"), /COALESCE\(ev\.result_json->>'evaluation_origin', ev\.result_json->'breakdown'->'meta'->>'evaluation_origin'\) = 'prod2_jev_domain_v1'/);
  assert.match(sql("eval_manual"), /IS DISTINCT FROM 'prod2_jev_domain_v1'/);
  assert.match(sql("eval_failed"), /ev\.status IN \('failed', 'uncertain'\)/);
  assert.match(sql("manual_requests"), /r\.source_key = 'seace_prod2'/);
  assert.match(sql("embeddings_ok"), /FROM prod2_embedding_attempts a/);
});

test("los costos se arman con pasos que existen y por etapa", () => {
  const sql = costsSql();
  for (const [bucket, definition] of Object.entries(COST_BUCKETS)) {
    assert.match(sql, new RegExp(`'${bucket}'::text AS bucket`));
    for (const key of definition.steps) assert.ok(findStep(definition.source, key), `${bucket}/${key}`);
  }
  assert.match(sql, /GROUP BY c\.bucket, c\.day/);
});

test("los conteos agrupan por paso y día en SQL, una rama por columna", () => {
  for (const source of ["menores", "mayores"] as const) {
    const sql = countsSql(columnSteps(source));
    for (const step of columnSteps(source)) assert.match(sql, new RegExp(`'${step.key}'::text AS step`));
    assert.equal(sql.split("GROUP BY e.day").length - 1, columnSteps(source).length);
  }
});

test("las fallas se agrupan por paso y motivo y limitan los elementos de cada grupo", () => {
  const sql = failuresSql(failureSteps("menores"));
  assert.match(sql, /row_number\(\) OVER \(PARTITION BY f\.step, f\.reason ORDER BY f\.ts DESC/);
  assert.match(sql, /WHERE r\.rn <= \(SELECT row_limit FROM p\)/);
  assert.match(sql, /ORDER BY r\.total DESC/);
  assert.ok(failureSteps("menores").some((step) => step.key === "jobs_failed"));
  // Las razones de revisión se cuentan una por una.
  assert.match(findStep("menores", "read_review")!.failureEvents!, /unnest\(string_to_array\(e\.reason, ', '\)\)/);
});

test("la lista de elementos lleva límite y orden por recencia", () => {
  const sql = itemsSql(findStep("menores", "jev_accepted")!);
  assert.match(sql, /ORDER BY e\.ts DESC/);
  assert.match(sql, /LIMIT \(SELECT row_limit FROM p\)/);
  assert.match(sql, /count\(\*\) OVER \(\)::int AS total/);
});

test("worker_jobs se filtra por su fecha cruda y la traza lo consulta una sola vez por licitación", () => {
  const jobs = findStep("menores", "jobs_failed")!.events;
  assert.match(jobs, /j\.finished_at >= \(SELECT ts_from FROM p\) AND j\.finished_at < \(SELECT ts_to FROM p\)/);
  assert.match(jobs, /j\.created_at >= \(SELECT ts_from FROM p\)/);
  assert.match(MINOR_QUERIES.jobs, /j\.payload->>'id_contrato' = \$1::text/);
  assert.match(MINOR_QUERIES.jobs, /LIMIT 100/);
});

test("la traza nunca pide texto de documentos ni payloads completos", () => {
  for (const sql of [...Object.values(MINOR_QUERIES), ...Object.values(MAJOR_QUERIES)]) {
    assert.doesNotMatch(sql, /raw_extraction_json|raw_search_json|raw_detail|raw_file_json|seace_download_url|storage_key|requested_by/);
  }
  // Los JSON se reducen a claves conocidas.
  assert.match(MINOR_QUERIES.jobs, /t\.k IN \(/);
  assert.match(MAJOR_QUERIES.evaluations, /t\.k IN \(/);
});
