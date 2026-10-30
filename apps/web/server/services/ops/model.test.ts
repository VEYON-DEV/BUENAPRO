import test from "node:test";
import assert from "node:assert/strict";
import {
  buildItem,
  companyLabel,
  companyLabels,
  groupFailures,
  labelCompanies,
  shapeCosts,
  shapeDaily,
  shapeQuota,
  traceHref,
} from "./model.ts";
import { columnSteps, findStep } from "./steps.ts";

const DIN = "e11f2610-eb56-457a-97eb-bbd24dd6f1a1";
const VEYON = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3";

test("la etiqueta de empresa quita el sufijo societario", () => {
  assert.equal(companyLabel("DIN AUTOMATIZACION S.A.C."), "DIN AUTOMATIZACION");
  assert.equal(companyLabel("VEYON SAC"), "VEYON");
  assert.equal(companyLabel("Estudio Jhon E.I.R.L."), "Estudio Jhon");
  assert.equal(companyLabel("Sociedad  Anónima  Cerrada S.A."), "Sociedad Anónima Cerrada");
  assert.equal(companyLabel("SAC"), "SAC", "si solo queda el sufijo se conserva el nombre");
});

test("las etiquetas repetidas se desambiguan con el final del RUC", () => {
  const companies = labelCompanies([
    { id: DIN.toUpperCase(), razon_social: "DIN AUTOMATIZACION S.A.C.", tenant_id: "T1", ruc: "20343877294" },
    { id: VEYON, razon_social: "VEYON SAC", tenant_id: "T2", ruc: "20609999991" },
    { id: "cc04995b-0000-4000-8000-000000000001", razon_social: "VEYON S.A.C.", tenant_id: "T3", ruc: "20600000002" },
  ]);
  assert.deepEqual(companies.map((company) => company.label), ["DIN AUTOMATIZACION", "VEYON · …9991", "VEYON · …0002"]);
  assert.equal(companies[0].id, DIN, "los ids quedan en minúsculas");
  assert.equal(companyLabels(companies).get(DIN), "DIN AUTOMATIZACION");
});

test("shapeDaily rellena con ceros, ordena como los días recibidos y agrupa las columnas", () => {
  const days = ["2026-10-26", "2026-10-25", "2026-10-24"];
  const table = shapeDaily("menores", days, [
    { step: "found", day: "2026-10-25", n: 25 },
    { step: "jev_accepted", day: "2026-10-24", n: 30 },
    { step: "jev_accepted", day: "2026-10-25", n: 5 },
    { step: "jev_accepted", day: "2026-09-01", n: 99 }, // fuera de la ventana: se ignora
    { step: "paso_inexistente", day: "2026-10-25", n: 7 },
  ]);
  assert.deepEqual(table.rows.map((row) => row.day), days);
  assert.equal(table.rows[0].values.found, 0);
  assert.equal(table.rows[1].values.found, 25);
  assert.equal(table.totals.jev_accepted, 35);
  assert.equal(table.max.jev_accepted, 30);
  assert.equal(table.columns.length, columnSteps("menores").length);
  assert.equal(table.groups.reduce((sum, group) => sum + group.span, 0), table.columns.length);
  assert.deepEqual(table.groups.map((group) => group.label), ["Entrada", "JEV", "Lectura", "Evaluación", "Operación"]);
  const lectura = table.groups.find((group) => group.label === "Lectura")!;
  assert.equal(lectura.shared, true, "la lectura es compartida por todas las empresas");
  assert.equal(table.groups.find((group) => group.label === "JEV")!.shared, false);
});

test("shapeCosts suma por rubro y día y conserva cuántos eventos tenían costo", () => {
  const days = ["2026-10-02", "2026-10-01"];
  const costs = shapeCosts(days, [
    { bucket: "menores_lectura", day: "2026-10-01", usd: "0.5", n: 4, priced: 4 },
    { bucket: "menores_lectura", day: "2026-10-02", usd: "0.25", n: 2, priced: 1 },
    { bucket: "mayores_jev", day: "2026-10-02", usd: 0.001, n: 10, priced: 10 },
  ]);
  assert.equal(costs.rows[0].day, "2026-10-02");
  assert.equal(costs.rows[0].cells.menores_lectura.priced, 1);
  assert.equal(costs.rows[1].cells.mayores_evaluacion.usd, 0);
  assert.equal(costs.totals.menores_lectura, 0.75);
  assert.ok(Math.abs(costs.totalUsd - 0.751) < 1e-9);
  assert.ok(Math.abs(costs.rows[0].totalUsd - 0.251) < 1e-9);
});

test("el cupo es por tenant (una columna por empresa) y va contra el límite recibido", () => {
  const companies = labelCompanies([
    { id: DIN, razon_social: "DIN AUTOMATIZACION S.A.C.", tenant_id: "46ba7840-ae84-4de0-ac98-16fab9e5d5bb" },
    { id: VEYON, razon_social: "VEYON SAC", tenant_id: "cc04995b-386d-45eb-ae1b-3d4353789fa2" },
    { id: "f98f1490-dd88-4ad8-8894-5e96fb3f21bf", razon_social: "VEYON copia", tenant_id: "CC04995B-386D-45EB-AE1B-3D4353789FA2" },
  ]);
  const quota = shapeQuota(["2026-10-25", "2026-10-24"], companies, [{ tenant_id: "cc04995b-386d-45eb-ae1b-3d4353789fa2", day: "2026-10-25", used: 7 }], 100);
  assert.equal(quota.limit, 100);
  assert.equal(quota.columns.length, 2, "dos perfiles del mismo tenant comparten cupo");
  assert.equal(quota.rows[0].used["cc04995b-386d-45eb-ae1b-3d4353789fa2"], 7);
  assert.equal(quota.rows[1].used["cc04995b-386d-45eb-ae1b-3d4353789fa2"], 0);
});

test("la traza solo se enlaza para licitaciones con identificador válido", () => {
  assert.equal(traceHref("menores", "104188"), "/ops/traza/menores/104188");
  assert.equal(traceHref("mayores", "1034924"), "/ops/traza/mayores/1034924");
  assert.equal(traceHref("menores", null), null);
  assert.equal(traceHref("menores", "abc"), null);
});

const labels = new Map([
  ["104188", { code: "CM-1-2026-X", entity: "ENTIDAD X", description: "Servicio de nube", jev: [{ profileId: DIN, score: 72 }, { profileId: VEYON, score: 45 }] }],
]);
const names = new Map([[DIN, "DIN AUTOMATIZACION"], [VEYON, "VEYON"]]);
const row = (patch = {}) => ({
  item_id: "104188", ts: new Date("2026-10-25T05:00:00Z"), day: "2026-10-24", profile_id: DIN, score: null, state: "accepted",
  reason: null, cost: null, ref: null, title: null, ...patch,
});

test("buildItem une el evento con la ficha de la licitación y el puntaje JEV de cada empresa", () => {
  const item = buildItem("menores", findStep("menores", "read_ok")!, row({ ref: "TDR.pdf" }), 0, labels, names, null);
  assert.equal(item.code, "CM-1-2026-X");
  assert.equal(item.entity, "ENTIDAD X");
  assert.equal(item.ref, "TDR.pdf");
  assert.deepEqual(item.jev, [{ company: "DIN AUTOMATIZACION", score: 72 }, { company: "VEYON", score: 45 }]);
  assert.equal(item.traceHref, "/ops/traza/menores/104188");
  assert.equal(item.company, "DIN AUTOMATIZACION");
  assert.equal(item.ts, "2026-10-25T05:00:00.000Z");
});

test("con una empresa elegida solo se muestra su puntaje; en un paso JEV manda el puntaje del propio evento", () => {
  const focused = buildItem("menores", findStep("menores", "read_ok")!, row(), 0, labels, names, VEYON);
  assert.deepEqual(focused.jev, [{ company: "VEYON", score: 45 }]);
  const jev = buildItem("menores", findStep("menores", "jev_accepted")!, row({ score: "63.5" }), 0, labels, names, null);
  assert.deepEqual(jev.jev, [{ company: "DIN AUTOMATIZACION", score: 63.5 }]);
});

test("las filas que no son licitaciones (búsquedas, embeddings) no tienen traza y usan su referencia como código", () => {
  const search = buildItem("mayores", findStep("mayores", "searches_ok")!, row({ item_id: null, ref: "robot industrial · good · 2025", title: "prod2-map-wide-20261020", state: "complete" }), 0, new Map(), names, null);
  assert.equal(search.traceHref, null);
  assert.equal(search.code, "robot industrial · good · 2025");
  assert.equal(search.entity, "prod2-map-wide-20261020");
  const job = buildItem("menores", findStep("menores", "jobs_failed")!, row({ item_id: null, ref: "#53597", profile_id: null }), 0, new Map(), names, null);
  assert.equal(job.code, "#53597");
  assert.equal(job.ref, null, "no se repite el código como referencia");
  assert.equal(job.traceHref, null);
});

test("buildItem limpia secretos de los mensajes de error", () => {
  const item = buildItem("menores", findStep("menores", "jobs_failed")!, row({ reason: "GET https://x.example/a?token=SECRETO falló", state: "dead" }), 0, labels, names, null);
  assert.doesNotMatch(item.reason ?? "", /SECRETO/);
});

test("groupFailures agrupa por paso y motivo conservando el total real y el orden recibido", () => {
  const base = { ts: "2026-10-25T05:00:00Z", profile_id: null, state: "auto", ref: "TDR.pdf", title: null };
  const groups = groupFailures(
    "menores",
    [
      { ...base, step: "read_review", reason: "payment_count_mismatch", total: 30, item_id: "104188" },
      { ...base, step: "read_review", reason: "payment_count_mismatch", total: 30, item_id: "104189" },
      { ...base, step: "jobs_failed", reason: "extract_tdr · timeout", total: 2, item_id: null, ref: "#9" },
      { ...base, step: "no_existe", reason: "x", total: 1, item_id: null },
    ],
    labels,
    names,
    null,
  );
  assert.equal(groups.length, 2);
  assert.equal(groups[0].total, 30);
  assert.equal(groups[0].items.length, 2, "se listan menos elementos que el total real");
  assert.equal(groups[0].stepLabel, "Lecturas en revisión");
  assert.equal(groups[1].items[0].traceHref, null);
});
