import test from "node:test";
import assert from "node:assert/strict";
import {
  GRADING_POLL_INTERVAL_MS,
  gradingFailureMessage,
  gradingLimitMessage,
  gradingPresentation,
  gradingUsageText,
  isGradingActive,
  localGradingRequest,
  normalizeGradingRequest,
  normalizeGradingUsage,
  type GradingRequest,
  type GradingStatus,
} from "./manualGrading.ts";

const request = (status: GradingStatus, reason: string | null = null): GradingRequest => ({
  status, mode: "queued", reason, created_at: "2026-10-30T10:00:00.000Z", updated_at: "2026-10-30T10:01:00.000Z",
});
const base = { request: null, usage: { used: 12, limit: 100 }, submitting: false, problem: null, regrade: false };

test("without a verdict the action is «Calificar» and with one it is «Re-calificar»", () => {
  const first = gradingPresentation(base);
  assert.deepEqual([first.tone, first.label, first.disabled, first.message], ["idle", "Calificar", false, null]);
  const again = gradingPresentation({ ...base, regrade: true });
  assert.deepEqual([again.tone, again.label, again.disabled], ["idle", "Re-calificar", false]);
});

test("a finished request leaves the button available with the right label", () => {
  assert.equal(gradingPresentation({ ...base, request: request("done") }).label, "Calificar");
  assert.equal(gradingPresentation({ ...base, request: request("done"), regrade: true }).label, "Re-calificar");
});

test("pending, reading and evaluating show a disabled in-progress state with a hint", () => {
  for (const status of ["pending", "reading"] as const) {
    const view = gradingPresentation({ ...base, request: request(status) });
    assert.deepEqual([view.tone, view.label, view.disabled], ["active", "Calificación en curso…", true], status);
    assert.equal(view.message, "Leyendo los documentos; puede tardar unos minutos.");
  }
  const evaluating = gradingPresentation({ ...base, request: request("evaluating"), regrade: true });
  assert.deepEqual([evaluating.tone, evaluating.label, evaluating.disabled], ["active", "Calificación en curso…", true]);
  assert.equal(evaluating.message, "Evaluando con tu perfil; puede tardar un minuto.");
});

test("sending a request is also an in-progress state", () => {
  const view = gradingPresentation({ ...base, submitting: true });
  assert.deepEqual([view.tone, view.disabled, view.message], ["active", true, "Leyendo los documentos; puede tardar unos minutos."]);
});

test("an in-progress request wins over the daily limit", () => {
  const view = gradingPresentation({ ...base, usage: { used: 100, limit: 100 }, request: request("pending") });
  assert.equal(view.tone, "active");
});

test("a failed request explains the reason in Spanish and allows a retry", () => {
  const view = gradingPresentation({ ...base, request: request("failed", "no_documents") });
  assert.deepEqual([view.tone, view.label, view.disabled], ["failed", "Reintentar calificación", false]);
  assert.equal(view.message, "La licitación no tiene documentos disponibles para leer. Puedes intentarlo de nuevo.");
});

test("every worker reason code has its own plain-Spanish message and unknown codes fall back", () => {
  const codes = ["reading_requires_review", "reading_failed", "no_documents", "contract_not_found", "profile_inactive", "invalid_model_output", "reading_timeout"];
  const messages = codes.map(code => gradingFailureMessage(code));
  assert.equal(new Set(messages).size, codes.length);
  for (const message of messages) {
    assert.match(message, /^[A-ZÁÉÍÓÚ]/);
    assert.match(message, /Puedes intentarlo de nuevo\.$/);
    assert.doesNotMatch(message, /_/);
  }
  assert.equal(gradingFailureMessage(null), "No se pudo completar la calificación. Puedes intentarlo de nuevo.");
  assert.equal(gradingFailureMessage("something_new"), gradingFailureMessage(null));
});

test("the daily limit disables the button and shows the message with the real number", () => {
  const fromUsage = gradingPresentation({ ...base, usage: { used: 100, limit: 100 } });
  assert.deepEqual([fromUsage.tone, fromUsage.disabled], ["limit", true]);
  assert.equal(fromUsage.message, "Se alcanzó el límite diario de 100 calificaciones manuales de tu empresa.");
  const custom = gradingPresentation({ ...base, usage: { used: 30, limit: 30 }, regrade: true });
  assert.equal(custom.label, "Re-calificar");
  assert.equal(custom.message, gradingLimitMessage(30));
  const fromServer = gradingPresentation({ ...base, usage: null, problem: { kind: "limit", message: "Se alcanzó el límite diario de 50 calificaciones manuales de tu empresa." } });
  assert.deepEqual([fromServer.tone, fromServer.disabled], ["limit", true]);
  assert.match(fromServer.message ?? "", /50/);
  const below = gradingPresentation({ ...base, usage: { used: 99, limit: 100 } });
  assert.equal(below.tone, "idle");
});

test("the limit takes precedence over an old failure because a retry would be rejected", () => {
  const view = gradingPresentation({ ...base, usage: { used: 100, limit: 100 }, request: request("failed", "reading_failed") });
  assert.equal(view.tone, "limit");
});

test("a missing profile sends the user to configure it", () => {
  const view = gradingPresentation({ ...base, problem: { kind: "no_profile" } });
  assert.deepEqual([view.tone, view.label, view.disabled], ["no_profile", "Configura tu perfil para calificar", false]);
});

test("a network problem is retryable", () => {
  const view = gradingPresentation({ ...base, problem: { kind: "network" } });
  assert.deepEqual([view.tone, view.label, view.disabled], ["failed", "Reintentar calificación", false]);
  assert.match(view.message ?? "", /conexión/);
});

test("usage text reads «Hoy: X de N calificaciones manuales» and is hidden while unknown", () => {
  assert.equal(gradingUsageText({ used: 7, limit: 100 }), "Hoy: 7 de 100 calificaciones manuales");
  assert.equal(gradingPresentation(base).usageText, "Hoy: 12 de 100 calificaciones manuales");
  assert.equal(gradingUsageText(null), null);
  assert.equal(gradingPresentation({ ...base, usage: null }).usageText, null);
  assert.equal(gradingUsageText({ used: Number.NaN, limit: 100 }), null);
});

test("active statuses are exactly pending, reading and evaluating", () => {
  for (const status of ["pending", "reading", "evaluating"]) assert.equal(isGradingActive(status), true, status);
  for (const status of ["done", "failed", "", null, undefined, "other"]) assert.equal(isGradingActive(status), false, String(status));
});

test("server payloads are validated before reaching the screen", () => {
  assert.deepEqual(normalizeGradingRequest({ status: "reading", mode: "direct", reason: null, created_at: "a", updated_at: "b", extra: 1 }),
    { status: "reading", mode: "direct", reason: null, created_at: "a", updated_at: "b" });
  assert.equal(normalizeGradingRequest({ status: "weird" }), null);
  assert.equal(normalizeGradingRequest(null), null);
  assert.equal(normalizeGradingRequest("reading"), null);
  assert.equal(normalizeGradingRequest({ status: "failed", mode: "other", reason: 4 })?.mode, "queued");
  assert.equal(normalizeGradingRequest({ status: "failed", reason: 4 })?.reason, null);
  assert.deepEqual(normalizeGradingUsage({ used: 3, limit: 100 }), { used: 3, limit: 100 });
  assert.equal(normalizeGradingUsage({ used: "x", limit: 100 }), null);
  assert.equal(normalizeGradingUsage({ used: 1, limit: 0 }), null);
  assert.equal(normalizeGradingUsage(undefined), null);
});

test("a local request mirrors what the server just answered", () => {
  const failed = localGradingRequest("failed", "reading_failed", new Date("2026-10-30T12:00:00Z"));
  assert.deepEqual(failed, { status: "failed", mode: "direct", reason: "reading_failed", created_at: "2026-10-30T12:00:00.000Z", updated_at: "2026-10-30T12:00:00.000Z" });
});

test("progress is polled about every 20 seconds", () => {
  assert.equal(GRADING_POLL_INTERVAL_MS, 20_000);
});
