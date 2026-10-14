import { test } from "node:test";
import assert from "node:assert/strict";
import { formatMoney, groupLabel, groupRecords, recordMatches, recordObject, recordText, updateRecord, validateProfileRecords } from "../apps/web/features/profile/model/records.ts";

test("legacy string becomes a record only when edited", () => {
  assert.equal(recordText("Ingeniero", "role", "role"), "Ingeniero");
  assert.deepEqual(updateRecord("Ingeniero", "role", "grado", "Titulado"), { role: "Ingeniero", grado: "Titulado" });
});
test("editing preserves unknown metadata and nested evidence", () => {
  const original = { role: "Developer", skills: ["JS"], extra: { private: true }, documents: [{ id: "1" }] };
  assert.deepEqual(updateRecord(original, "role", "grado", "Bachiller"), { ...original, grado: "Bachiller" });
  assert.equal(original.grado, undefined);
});
test("blank new hidden record is invalid", () => assert.ok(validateProfileRecords([{ role: "" }])));
test("optional professional name may be empty", () => assert.equal(validateProfileRecords([{ role: "Ingeniero", nombre: "" }]), null));
test("nested certificate is validated", () => assert.ok(validateProfileRecords([{ role: "Ingeniero", certifications: [{ nombre: "" }] }])));
test("negative numbers rejected and zero retained", () => {
  assert.ok(validateProfileRecords([{ objeto: "Contrato", monto: -1 }], "objeto"));
  assert.equal(validateProfileRecords([{ objeto: "Contrato", monto: 0 }], "objeto"), null);
});
test("untouched legacy record is not blocked by newer validation", () => {
  const old = [{ role: "", old_field: "historical" }];
  assert.equal(validateProfileRecords(old, "role", old), null);
});
test("normalization does not mutate rich object", () => {
  const record = { nombre: "ISO", unknown: "retain" };
  assert.deepEqual(recordObject(record, "nombre"), record);
  assert.notEqual(recordObject(record, "nombre"), record);
});

test("groups spelling variants of a rubro together and keeps original indexes", () => {
  const records = [{ objeto: "A", rubro: "OIL & GAS" }, { objeto: "B", rubro: "Educación" }, { objeto: "C", rubro: "Oil & Gas" }, { objeto: "D" }, "Legado"];
  const groups = groupRecords(records, "objeto", "rubro");
  assert.deepEqual(groups.map(group => [group.label, group.items.map(item => item.index)]), [["Oil & Gas", [0, 2]], ["Educación", [1]], ["Sin rubro", [3, 4]]]);
  assert.equal(records[0].rubro, "OIL & GAS");
});
test("all-caps group labels are title-cased for display only", () => {
  assert.equal(groupLabel(["INSTRUMENTACION DE PLANTA"]), "Instrumentacion de Planta");
  assert.equal(groupLabel(["AGROINDUSTRIAL", "Agroindustrial"]), "Agroindustrial");
});
test("search ignores accents and case across text and numbers", () => {
  assert.ok(recordMatches({ objeto: "Migración SCADA", entidad: "Agroaurora" }, "objeto", "migracion scada"));
  assert.ok(recordMatches({ objeto: "X", numero_contrato: 4505459913 }, "objeto", "45054"));
  assert.ok(!recordMatches({ objeto: "Migración SCADA" }, "objeto", "petroperu"));
  assert.ok(recordMatches("Servicio legado", "objeto", "legado"));
});
test("money keeps the record currency without conversion", () => {
  assert.equal(formatMoney(41023, "USD"), "USD 41,023");
  assert.equal(formatMoney(0, "PEN"), "S/ 0");
  assert.equal(formatMoney(null, "PEN"), null);
});
