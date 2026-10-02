import { test } from "node:test";
import assert from "node:assert/strict";
import { recordObject, recordText, updateRecord, validateProfileRecords } from "../apps/web/features/profile/model/records.ts";

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
