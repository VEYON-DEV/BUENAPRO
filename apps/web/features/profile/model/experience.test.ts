import test from "node:test";
import assert from "node:assert/strict";
import { validateExperienceRecords } from "./experience.ts";

test("accepts complete contracts and preserves legacy entries and metadata", () => {
  const records = ["Servicio histórico sin desglosar", { objeto: "Contrato antiguo", anio: 2021, conformidad: "Conforme", custom: { origin: "manual" } }, {
    objeto: "Implementación ERP", rubro: "Tecnología", especialidad: "Software", tipo_objeto: "servicios", moneda: "PEN", monto: 500000,
    fecha_inicio: "2024-02-29", fecha_fin: "2024-08-01", fecha_conformidad: "2024-08-15", porcentaje_participacion: 50,
    modalidad_participacion: "consorcio", actividades: "Desarrollo\nSoporte", acreditacion: "documentada", documents: [{ id: "abc" }],
  }];
  const before = JSON.stringify(records);
  assert.equal(validateExperienceRecords(records), null);
  assert.equal(JSON.stringify(records), before);
});

test("allows optional and empty fields without requiring a legacy migration", () => {
  assert.equal(validateExperienceRecords(undefined), null);
  assert.equal(validateExperienceRecords([]), null);
  assert.equal(validateExperienceRecords([{ rubro: "", tipo_objeto: null, monto: null, fecha_inicio: "", porcentaje_participacion: 0 }]), null);
});

test("rejects invalid container and record types", () => {
  for (const value of [{}, "contract", [null], [42], [[]]]) assert.ok(validateExperienceRecords(value));
});

test("rejects invalid numeric values instead of silently coercing strings", () => {
  for (const monto of [-1, NaN, Infinity, "100", true]) assert.ok(validateExperienceRecords([{ monto }]));
  for (const porcentaje_participacion of [-1, 101, Infinity, "50"]) assert.ok(validateExperienceRecords([{ porcentaje_participacion }]));
  assert.equal(validateExperienceRecords([{ monto: 0, porcentaje_participacion: 100 }]), null);
});

test("checks the enum contract for object, currency, participation and accreditation", () => {
  for (const key of ["tipo_objeto", "moneda", "modalidad_participacion", "acreditacion"]) {
    assert.ok(validateExperienceRecords([{ [key]: "unknown" }]));
    assert.ok(validateExperienceRecords([{ [key]: 1 }]));
  }
  assert.equal(validateExperienceRecords([{ tipo_objeto: "consultoria_obras", moneda: "USD", modalidad_participacion: "subcontratista", acreditacion: "pendiente" }]), null);
});

test("checks actual calendar dates and execution/acceptance chronology", () => {
  for (const date of ["2023-02-29", "2024-02-30", "2024-13-01", "2024-1-01", "2024-01-01T00:00:00Z", 2024]) {
    for (const key of ["fecha_inicio", "fecha_fin", "fecha_conformidad"]) assert.ok(validateExperienceRecords([{ [key]: date }]));
  }
  assert.ok(validateExperienceRecords([{ fecha_inicio: "2024-02-01", fecha_fin: "2024-01-31" }]));
  assert.ok(validateExperienceRecords([{ fecha_fin: "2024-02-01", fecha_conformidad: "2024-01-31" }]));
  assert.ok(validateExperienceRecords([{ fecha_inicio: "2024-02-01", fecha_conformidad: "2024-01-31" }]));
  assert.equal(validateExperienceRecords([{ fecha_inicio: "2024-02-01", fecha_fin: "2024-02-01", fecha_conformidad: "2024-02-01" }]), null);
});

test("validates structured free text and identifies the invalid contract", () => {
  for (const key of ["rubro", "especialidad", "actividades"]) assert.match(validateExperienceRecords(["legacy", { [key]: {} }])!, /^Contrato 2:/);
});
