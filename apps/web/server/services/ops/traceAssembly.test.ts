import test from "node:test";
import assert from "node:assert/strict";
import { assembleMajorTrace, assembleMinorTrace, evaluationCost, isAutomaticAnalysis, sortEvents } from "./traceAssembly.ts";
import type { MajorTraceRaw, MinorTraceRaw, TraceEvent } from "./traceAssembly.ts";

const DIN = "e11f2610-eb56-457a-97eb-bbd24dd6f1a1";
const VEYON = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3";
const labels = new Map([[DIN, "DIN AUTOMATIZACION"], [VEYON, "VEYON"]]);
const T = (time: string) => new Date(`2026-10-25T${time}Z`);

const emptyMinor = (): MinorTraceRaw => ({ contract: null, decisions: [], jobs: [], documents: [], readings: [], evaluations: [], manual: [], pipeline: [] });
const emptyMajor = (): MajorTraceRaw => ({ discovery: null, documents: [], downloads: [], evaluations: [], extractions: [], jevCache: [], manual: [] });

function minorFixture(): MinorTraceRaw {
  return {
    contract: {
      id_contrato: "104188", codigo: "CM-100-2026-ENTIDAD", entidad_nombre: "ENTIDAD DEMO", descripcion: "Servicio de nube", pipeline_state: "extracted",
      valor_estimado: "12000.00", fec_publica: T("01:00:00"), fec_fin_cotizacion: T("23:00:00"), first_seen_at: T("04:30:00"), last_seen_at: T("05:30:00"),
      detail_fetched_at: T("04:31:00"), cubso_segmento: "81",
    },
    decisions: [
      { id: "7", profile_id: DIN, razon_social: "DIN AUTOMATIZACION S.A.C.", decision: "accepted", decided_by: "jev", score: "72.00", threshold: "60.00", domain: "informatica", subcategory: "licencias_software", information: "suficiente", model: "jev-1.13.0", input_tokens: 800, output_tokens: 50, cost_usd: "0.00040000", attempts: 1, created_at: T("04:00:00"), updated_at: T("04:10:00"), policy_version: "prod6_screening_v2", prefilter_reasons: [], keyword_lines: ["TI"], result_json: { score: 2.88, api_key: "SECRETO-JEV" } },
      { id: "8", profile_id: VEYON, razon_social: "VEYON SAC", decision: "rejected", decided_by: "jev", score: "45.00", threshold: "60.00", cost_usd: "0.00040000", attempts: 1, created_at: T("04:00:00"), updated_at: T("04:10:00"), policy_version: "prod6_screening_v2", prefilter_reasons: [], keyword_lines: [] },
      { id: "9", profile_id: "33333333-3333-4333-8333-333333333333", razon_social: "Otra SAC", decision: "pending", decided_by: "jev", score: null, attempts: 2, last_error: "JevHttpError status=429", created_at: T("04:00:00"), updated_at: T("04:20:00"), prefilter_reasons: [], keyword_lines: [] },
      { id: "10", profile_id: DIN, decision: "rejected", decided_by: "prefilter", score: null, prefilter_reasons: ["closed"], keyword_lines: [], attempts: 0, created_at: T("03:00:00"), updated_at: T("03:00:00"), policy_version: "prod6_screening_v2" },
    ],
    jobs: [
      { id: "500", job_type: "extract_tdr", queue_name: "llm", status: "dead", attempts: 5, max_attempts: 5, created_at: T("04:35:00"), finished_at: T("04:50:00"), held: false, last_error: "boom https://x.example/a?token=SECRETO-URL", payload: {}, runtime_result: {}, total: 130 },
      { id: "501", job_type: "derive_summary", queue_name: "match", status: "pending", attempts: 0, max_attempts: 1, created_at: T("04:36:00"), held: true, last_error: "retenido", runtime_result: { outcome: "read_or_reused" }, total: 130 },
      { id: "502", job_type: "download_file", queue_name: "io", status: "done", attempts: 1, max_attempts: 5, created_at: T("04:32:00"), finished_at: T("04:33:00"), held: false, last_error: null, runtime_result: {}, total: 130 },
    ],
    documents: [{ id: "30", filename: "TDR.pdf", doc_class: "tdr", mime: "application/pdf", size_original_bytes: "2048", has_text_layer: true, created_at: T("04:33:00") }],
    readings: [
      { id: "40", filename: "TDR.pdf", model: "gemini-3.1-flash-lite", prompt_version: "v3", quality: "auto", requires_human_review: true, is_current: true, input_tokens: 1000, output_tokens: 200, cost_usd: "0.0039", created_at: T("04:40:00"), review_codes: "payment_count_mismatch, term_not_literal", reused_from_extraction_id: null },
      { id: "41", filename: "Anexo.pdf", model: "gemini-3.1-flash-lite", quality: "auto", requires_human_review: false, is_current: false, cost_usd: "0", created_at: T("04:41:00"), reused_from_extraction_id: "40" },
    ],
    evaluations: [
      { id: "900", event_type: "automatic_analysis", payload: { score: 55, verdict: "gris", source: "automatic" }, created_at: T("05:00:00"), profile_id: VEYON, razon_social: "VEYON SAC", meta: { cost_usd: 0.0017, analyzed_at: "2026-10-25T05:00:07Z", model: "gemini-3.1-flash-lite" } },
      { id: "901", event_type: "ai_analysis_completed", payload: { score: 80, verdict: "verde", cost_usd: 0.02, execution: "web_direct" }, created_at: T("05:10:00"), profile_id: DIN, meta: { cost_usd: 0.5, analyzed_at: "2026-10-25T09:00:00Z" } },
      { id: "902", event_type: "state_change", payload: {}, created_at: T("05:20:00"), profile_id: DIN, user_state: "interesada", meta: {} },
    ],
    manual: [{ id: "3", profile_id: DIN, mode: "queued", status: "failed", reason: "reading_requires_review", lima_day: "2026-10-25", created_at: T("05:30:00"), updated_at: T("05:45:00") }],
    pipeline: [{ id: "77", stage: "process_contract", status: "failed", error: "Authorization: Bearer abcdef123456", duration_ms: 12, created_at: T("04:34:00") }],
  };
}

test("la traza de menores queda en orden cronológico y cubre todas las fuentes", () => {
  const trace = assembleMinorTrace("104188", minorFixture(), labels)!;
  const stamps = trace.events.map((event) => event.ts);
  assert.deepEqual(stamps, [...stamps].sort());
  const kinds = new Set(trace.events.map((event) => event.kind));
  for (const kind of ["hallazgo", "jev", "trabajo", "documento", "lectura", "evaluacion", "manual", "seguimiento", "pipeline"]) assert.ok(kinds.has(kind as TraceEvent["kind"]), kind);
  assert.equal(trace.header.code, "CM-100-2026-ENTIDAD");
  assert.equal(trace.header.entity, "ENTIDAD DEMO");
  assert.deepEqual(trace.events.slice(0, 2).map((event) => event.id), ["sondeo", "decision-10"], "a las 03:00 primero se ve la contratación y luego el filtro previo");
});

test("cada decisión JEV sale por empresa, con puntaje, decisión, intentos y error", () => {
  const trace = assembleMinorTrace("104188", minorFixture(), labels)!;
  const byId = Object.fromEntries(trace.events.map((event) => [event.id, event]));
  assert.equal(byId["decision-7"].company, "DIN AUTOMATIZACION");
  assert.equal(byId["decision-7"].title, "JEV aceptó");
  assert.equal(byId["decision-7"].outcome, "ok");
  assert.match(byId["decision-7"].summary!, /Puntaje 72 \(umbral 60\) · intentos 1/);
  assert.equal(byId["decision-7"].costUsd, 0.0004);
  assert.equal(byId["decision-8"].company, "VEYON");
  assert.equal(byId["decision-8"].title, "JEV rechazó");
  assert.equal(byId["decision-8"].outcome, "info");
  assert.equal(byId["decision-9"].title, "JEV pendiente");
  assert.equal(byId["decision-9"].outcome, "error");
  assert.equal(byId["decision-9"].reason, "JevHttpError status=429");
  assert.equal(byId["decision-9"].company, "Otra SAC", "sin etiqueta conocida usa la razón social");
  assert.equal(byId["decision-10"].title, "Filtro previo: descartada");
  assert.equal(byId["decision-10"].reason, "closed");
});

test("los trabajos muestran tipo, estado, intentos, error y si están retenidos; avisan si hay más", () => {
  const trace = assembleMinorTrace("104188", minorFixture(), labels)!;
  const job = (id: string) => trace.events.find((event) => event.id === `job-${id}`)!;
  assert.equal(job("500").outcome, "error");
  assert.match(job("500").summary!, /Estado dead · intento 5\/5/);
  assert.equal(job("501").outcome, "warn");
  assert.match(job("501").summary!, /retenido para revisión manual/);
  assert.match(job("501").summary!, /resultado read_or_reused/);
  assert.equal(job("502").outcome, "ok");
  assert.ok(job("500").facts.some((fact) => fact.label === "Terminó" && fact.time));
  assert.ok(trace.header.notes.some((note) => /3 de 130 trabajos/.test(note)));
});

test("lecturas: códigos de revisión, tokens y reutilización", () => {
  const trace = assembleMinorTrace("104188", minorFixture(), labels)!;
  const reading = trace.events.find((event) => event.id === "lectura-40")!;
  assert.equal(reading.outcome, "warn");
  assert.equal(reading.reason, "payment_count_mismatch, term_not_literal");
  assert.equal(reading.costUsd, 0.0039);
  assert.ok(reading.facts.some((fact) => fact.label === "Tokens" && fact.value === "1200"));
  const reused = trace.events.find((event) => event.id === "lectura-41")!;
  assert.equal(reused.title, "Lectura reutilizada");
  assert.equal(reused.outcome, "ok");
});

test("evaluaciones: origen automático o manual y costo del evento o de la última evaluación cercana", () => {
  const trace = assembleMinorTrace("104188", minorFixture(), labels)!;
  const auto = trace.events.find((event) => event.id === "evaluacion-900")!;
  assert.equal(auto.title, "Evaluación automática");
  assert.equal(auto.costUsd, 0.0017, "7 s entre el evento y la evaluación guardada");
  assert.match(auto.summary!, /Puntaje 55 · veredicto gris/);
  const manual = trace.events.find((event) => event.id === "evaluacion-901")!;
  assert.equal(manual.title, "Evaluación manual");
  assert.equal(manual.costUsd, 0.02, "el costo del propio evento manda");
  assert.equal(trace.events.find((event) => event.id === "seguimiento-902")!.kind, "seguimiento");
  assert.equal(evaluationCost({}, { cost_usd: 0.5, analyzed_at: "2026-10-25T09:00:00Z" }, "2026-10-25T05:00:00.000Z"), null, "una evaluación posterior no se atribuye al evento");
  assert.equal(isAutomaticAnalysis("analysis", "worker"), true);
  assert.equal(isAutomaticAnalysis("analysis", "manual_request"), false);
});

test("la calificación manual aparece al pedirla y al terminar, con su motivo", () => {
  const trace = assembleMinorTrace("104188", minorFixture(), labels)!;
  const start = trace.events.find((event) => event.id === "menores-manual-3")!;
  const end = trace.events.find((event) => event.id === "menores-manual-end-3")!;
  assert.equal(start.outcome, "error");
  assert.equal(end.title, "Calificación manual fallida");
  assert.equal(end.reason, "reading_requires_review");
  assert.ok(Date.parse(end.ts) > Date.parse(start.ts));
});

test("ni los eventos ni su detalle muestran secretos", () => {
  const raw = minorFixture();
  raw.manual[0] = { ...raw.manual[0], requested_by: "persona@example.org" };
  const trace = assembleMinorTrace("104188", raw, labels)!;
  const dump = JSON.stringify(trace);
  assert.doesNotMatch(dump, /SECRETO-JEV|SECRETO-URL|abcdef123456|persona@example/);
  assert.match(dump, /\[oculto\]/);
  assert.doesNotMatch(dump, /requested_by/);
});

test("el total de costo suma los eventos con costo y cuenta eventos por tipo", () => {
  const trace = assembleMinorTrace("104188", minorFixture(), labels)!;
  const expected = trace.events.reduce((sum, event) => sum + (event.costUsd ?? 0), 0);
  assert.equal(trace.totals.costUsd, expected);
  assert.equal(trace.totals.events, trace.events.length);
  assert.equal(trace.totals.byKind.jev, 4);
});

test("sin contrato en seace_contracts se arma desde las decisiones y se avisa", () => {
  const raw = { ...emptyMinor(), decisions: [minorFixture().decisions[3]] };
  raw.decisions[0] = { ...raw.decisions[0], codigo: "CM-1-2026-X", entidad: "ENT", descripcion: "Algo" };
  const trace = assembleMinorTrace("1", raw, labels)!;
  assert.equal(trace.header.code, "CM-1-2026-X");
  assert.ok(trace.header.notes[0].includes("No está en seace_contracts"));
  assert.equal(assembleMinorTrace("1", emptyMinor(), labels), null);
});

test("el orden es estable: mismo instante, primero la etapa más temprana del flujo", () => {
  const same = "2026-10-25T10:00:00.000Z";
  const make = (id: string, kind: TraceEvent["kind"]): TraceEvent => ({ id, ts: same, kind, outcome: "ok", title: id, company: null, summary: null, reason: null, costUsd: null, facts: [], detail: null });
  const sorted = sortEvents([make("e", "evaluacion"), make("b", "jev"), make("d", "lectura"), make("a", "hallazgo"), make("c", "trabajo")]);
  assert.deepEqual(sorted.map((event) => event.id), ["a", "b", "c", "d", "e"]);
});

function majorFixture(): MajorTraceRaw {
  return {
    discovery: {
      id_convocatoria: "1034924", id_procedimiento: "55", nomenclature: "LP-ABR-1-2026-X", buyer_name: "MUNICIPALIDAD X", description: "Adquisición de servidores",
      object_label: "Bien", published_at: T("01:00:00"), reference_amount: "250000.00", currency: "PEN", first_seen_at: T("02:00:00"), last_seen_at: T("09:00:00"),
      detail_fetched_at: T("02:10:00"), proposals_closes_at: T("20:00:00"), document_inventory_total: 3, document_inventory_complete: true,
    },
    documents: [{ codigo_alfresco: "d1", name: "Bases.pdf", document_type: "bases", stage_name: "Convocatoria", first_seen_at: T("02:11:00"), is_listed: true }],
    downloads: [{ codigo_alfresco: "d1", name: "Bases.pdf", sha256_original: "a".repeat(64), bytes: "1048576", content_type: "application/pdf", format: "pdf", downloaded_at: T("02:20:00") }],
    evaluations: [
      { id: "181", kind: "jev_affinity", status: "done", policy_version: "prod2_commercial_union_v1", model: "jev-1.13.0", profile_id: VEYON, cost_usd: "0.00007", completed_at: T("02:05:00"), created_at: T("02:05:00"), result_json: { affinity_index: 91, domain: "informatica" } },
      { id: "183", kind: "gemini_extraction", status: "done", policy_version: "prod2_document_extraction_v1", model: "gemini-3.1-flash-lite", profile_id: VEYON, cost_usd: "0.0062", created_at: T("02:30:00"), completed_at: T("02:31:00"), input_tokens: 14000, output_tokens: 1600, result_json: {} },
      { id: "184", kind: "gemini_extraction", status: "failed", policy_version: "prod2_document_extraction_v1", model: "gemini-2.5-flash-lite", cost_usd: "0.019", created_at: T("02:25:00"), updated_at: T("02:26:00"), completed_at: T("02:26:00"), result_json: { failure_reason: "MAX_TOKENS" } },
      { id: "190", kind: "gemini_match", status: "done", policy_version: "prod2_match_v1", model: "gemini-3.1-flash-lite", profile_id: DIN, razon_social: "DIN AUTOMATIZACION S.A.C.", cost_usd: "0.0024", created_at: T("03:00:00"), completed_at: T("03:01:00"), origin: "prod2_manual_request_v1", result_json: { score: 62, verdict: "ambar" } },
      { id: "191", kind: "gemini_match", status: "uncertain", policy_version: "prod2_match_v1", profile_id: VEYON, created_at: T("03:30:00"), updated_at: T("03:35:00"), origin: "prod2_jev_domain_v1", result_json: { reason: "expired_reservation" } },
    ],
    extractions: [{ id: "60", evaluation_id: "183", model: "gemini-3.1-flash-lite", quality: "auto", requires_human_review: true, facet_count: 14, is_current: true, created_at: T("02:31:00"), filename: "Bases.pdf", review_codes: "payment_count_mismatch" }],
    jevCache: [{ cache_key: "f".repeat(64), model: "jev-1.13.0", policy_version: "prod2_commercial_union_v1", status: "uncertain", created_at: T("02:04:00"), blocked_reason: "provider_blocked" }],
    manual: [{ id: "5", profile_id: DIN, mode: "queued", status: "pending", lima_day: "2026-10-25", created_at: T("02:50:00"), updated_at: T("02:50:00") }],
  };
}

test("la traza de mayores ordena JEV, documentos, lecturas y evaluaciones; la lectura se une a su reserva", () => {
  const trace = assembleMajorTrace("1034924", majorFixture(), labels)!;
  const stamps = trace.events.map((event) => event.ts);
  assert.deepEqual(stamps, [...stamps].sort());
  assert.equal(trace.header.code, "LP-ABR-1-2026-X");
  const reading = trace.events.find((event) => event.id === "lectura-183")!;
  assert.equal(reading.outcome, "warn", "lectura correcta pero en revisión humana");
  assert.equal(reading.reason, "payment_count_mismatch");
  assert.equal(reading.costUsd, 0.0062);
  assert.equal(trace.events.filter((event) => event.id.startsWith("lectura-guardada")).length, 0, "sin duplicar la extracción ya unida");
  const failed = trace.events.find((event) => event.id === "lectura-184")!;
  assert.equal(failed.outcome, "error");
  assert.equal(failed.reason, "MAX_TOKENS");
});

test("mayores: la empresa solo aparece en la evaluación; JEV y lectura son compartidos", () => {
  const trace = assembleMajorTrace("1034924", majorFixture(), labels)!;
  const byId = Object.fromEntries(trace.events.map((event) => [event.id, event]));
  assert.equal(byId["jev-181"].company, null);
  assert.equal(byId["jev-181"].outcome, "ok");
  assert.match(byId["jev-181"].summary!, /afinidad 91/);
  assert.equal(byId["lectura-183"].company, null);
  assert.equal(byId["evaluacion-190"].company, "DIN AUTOMATIZACION");
  assert.equal(byId["evaluacion-190"].title, "Evaluación manual (Calificar)");
  assert.match(byId["evaluacion-190"].summary!, /puntaje 62 · veredicto ambar/);
  assert.equal(byId["evaluacion-191"].title, "Evaluación automática");
  assert.equal(byId["evaluacion-191"].outcome, "warn");
  assert.equal(byId["evaluacion-191"].reason, "expired_reservation");
  assert.equal(byId["jev-cache-ffffffffffff"].outcome, "warn");
  assert.equal(byId["mayores-manual-5"].outcome, "pending");
});

test("mayores: documentos, descargas y sin datos", () => {
  const trace = assembleMajorTrace("1034924", majorFixture(), labels)!;
  const download = trace.events.find((event) => event.id.startsWith("descarga-"))!;
  assert.match(download.summary!, /Bases\.pdf · pdf · 1\.0 MB/);
  assert.doesNotMatch(JSON.stringify(download.detail), new RegExp("a".repeat(40)), "el hash completo no se muestra");
  assert.equal(assembleMajorTrace("1", emptyMajor(), labels), null);
  const bare = assembleMajorTrace("1", { ...emptyMajor(), discovery: majorFixture().discovery }, labels)!;
  assert.ok(bare.header.notes[0].includes("Todavía no tiene evaluaciones"));
});
