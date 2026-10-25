import test from "node:test";
import assert from "node:assert/strict";
import {
  JEV_DISCLAIMER,
  JEV_DECISION_LABELS,
  JEV_DOMAIN_LABELS,
  JEV_INFORMATION_LABELS,
  JEV_SUBCATEGORY_LABELS,
  jevAffinity,
  jevCompanyAffinities,
  jevDomainLabel,
  jevSubcategoryLabel,
} from "./jevAffinity.ts";

test("every subcategory, domain, decision and information value has a Spanish label", () => {
  assert.deepEqual(Object.keys(JEV_SUBCATEGORY_LABELS).sort(), [
    "automatizacion_industrial", "capacitacion", "desarrollo_sistemas", "electrico_instrumentacion", "hardware_ti",
    "licencias_software", "otro", "redes_telecom", "soporte_ti",
  ]);
  assert.equal(jevSubcategoryLabel("licencias_software"), "Licencias de software");
  assert.equal(jevSubcategoryLabel("electrico_instrumentacion"), "Eléctrico e instrumentación");
  assert.deepEqual(Object.keys(JEV_DOMAIN_LABELS).sort(), ["ambiguo", "industrial_electrica", "informatica", "otro"]);
  assert.equal(jevDomainLabel("industrial_electrica"), "Industrial y eléctrica");
  assert.deepEqual(JEV_DECISION_LABELS, { accepted: "Aprobada", rejected: "Descartada", pending: "Pendiente" });
  assert.deepEqual(JEV_INFORMATION_LABELS, { claro: "Información suficiente", insuficiente: "Información insuficiente" });
});

test("unknown or empty codes never break the view", () => {
  assert.equal(jevSubcategoryLabel("mantenimiento_civil"), "Mantenimiento civil");
  assert.equal(jevSubcategoryLabel("constructor"), "Constructor");
  assert.equal(jevSubcategoryLabel("toString"), "ToString");
  for (const value of [null, undefined, "", "  ", 3, {}]) assert.equal(jevSubcategoryLabel(value), null);
});

test("list affinity needs a valid 0-100 score and rounds only for display", () => {
  const affinity = jevAffinity({ jev_score: 77.5, jev_decision: "accepted", jev_subcategory: "soporte_ti", jev_threshold: 60 });
  assert.equal(affinity?.score, 78);
  assert.equal(affinity?.exactScore, 77.5);
  assert.equal(affinity?.valueLabel, "78 / 100");
  assert.equal(affinity?.label, "Afinidad JEV");
  assert.equal(affinity?.subcategoryLabel, "Soporte TI");
  assert.equal(affinity?.decisionLabel, "Aprobada");
  assert.match(affinity?.description ?? "", /umbral 60/);
  assert.ok(affinity?.description.endsWith(JEV_DISCLAIMER));
  assert.equal(jevAffinity({ jev_score: 0, jev_decision: "rejected" })?.score, 0, "a real zero is still a score");
  assert.equal(jevAffinity({ jev_score: 100 })?.valueLabel, "100 / 100");
});

test("rows without a JEV score keep the current indicator", () => {
  for (const row of [null, undefined, {}, { jev_score: null, jev_decision: "pending" }, { jev_score: "78" }, { jev_score: NaN },
    { jev_score: -1 }, { jev_score: 100.5 }, { jev_score: Infinity }, { jev_score: true }]) {
    assert.equal(jevAffinity(row as never), null);
  }
});

test("company rows keep the received order and label every state", () => {
  const rows = jevCompanyAffinities([
    { profile_id: "a", profile_name: "VEYON SAC", is_current_profile: true, score: 91.25, threshold: 60, decision: "accepted", decided_by: "jev",
      subcategory: "licencias_software", domain: "informatica", information: "claro", decided_at: "2026-10-25T00:20:36Z" },
    { profile_id: "b", profile_name: "OTRA SAC", is_current_profile: false, score: 41, threshold: 60, decision: "rejected", decided_by: "jev",
      subcategory: "otro", domain: "ambiguo", information: "insuficiente" },
    { profile_id: "c", profile_name: null, score: null, decision: "pending", decided_by: "jev" },
    { profile_id: "d", profile_name: "PREFILTRO SAC", score: null, decision: "rejected", decided_by: "prefilter" },
  ]);
  assert.deepEqual(rows.map((row) => row.profileId), ["a", "b", "c", "d"]);
  const [accepted, rejected, pending, prefilter] = rows;
  assert.equal(accepted.isCurrent, true);
  assert.equal(accepted.score, 91);
  assert.equal(accepted.decisionLabel, "Aprobada");
  assert.equal(accepted.tone, "green");
  assert.equal(accepted.domainLabel, "Informática");
  assert.equal(accepted.informationLabel, "Información suficiente");
  assert.equal(accepted.informationClear, true);
  assert.equal(accepted.noScoreNote, null);
  assert.match(accepted.valueText, /91 de 100; umbral 60/);
  assert.equal(rejected.decisionLabel, "Descartada");
  assert.equal(rejected.tone, "red");
  assert.equal(rejected.informationLabel, "Información insuficiente");
  assert.equal(rejected.informationClear, false);
  assert.equal(pending.decisionLabel, "Pendiente");
  assert.equal(pending.tone, "amber");
  assert.equal(pending.name, "Empresa sin nombre");
  assert.equal(pending.score, null);
  assert.equal(pending.noScoreNote, "En espera de la calificación JEV.");
  assert.equal(prefilter.score, null, "no score is invented for a prefilter decision");
  assert.equal(prefilter.noScoreNote, "Sin calificación JEV: decidida por prefiltro.");
  assert.equal(prefilter.informationLabel, null);
  assert.equal(prefilter.informationClear, null);
});

test("empty, malformed or unknown company data is tolerated", () => {
  assert.deepEqual(jevCompanyAffinities(null), []);
  assert.deepEqual(jevCompanyAffinities(undefined), []);
  assert.deepEqual(jevCompanyAffinities([]), []);
  assert.deepEqual(jevCompanyAffinities([null, {}, { profile_name: "sin id" }] as never), []);
  const [odd] = jevCompanyAffinities([{ profile_id: "x", profile_name: "X", decision: "archived", score: 140 }]);
  assert.equal(odd.decision, null);
  assert.equal(odd.decisionLabel, "Archived");
  assert.equal(odd.tone, "neutral");
  assert.equal(odd.score, null, "an out-of-range score is not shown");
});
