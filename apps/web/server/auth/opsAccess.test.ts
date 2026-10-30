import test from "node:test";
import assert from "node:assert/strict";
import { effectiveOpsUserId, parseOpsAdminIds, resolveOpsAdminId } from "./opsAccess.ts";

const ADMIN = "5425c92d-861b-4404-9c29-5f5360eeaf18";
const OTHER = "9f6ec18b-9e9b-41d9-ae7c-83d9d7e0d197";
const THIRD = "72a58fc1-4175-4c37-9028-eb8f5896d29e";

test("un id de la lista entra y se devuelve en minúsculas", () => {
  assert.equal(resolveOpsAdminId(ADMIN, { NODE_ENV: "production", OPS_ADMIN_USER_IDS: ADMIN }), ADMIN);
  assert.equal(resolveOpsAdminId(ADMIN.toUpperCase(), { NODE_ENV: "production", OPS_ADMIN_USER_IDS: ADMIN }), ADMIN);
});

test("otro usuario con sesión no entra", () => {
  assert.equal(resolveOpsAdminId(OTHER, { NODE_ENV: "production", OPS_ADMIN_USER_IDS: ADMIN }), null);
});

test("sin sesión no entra, ni en producción ni fuera de ella", () => {
  assert.equal(resolveOpsAdminId(null, { NODE_ENV: "production", OPS_ADMIN_USER_IDS: ADMIN }), null);
  assert.equal(resolveOpsAdminId(undefined, { NODE_ENV: "development", OPS_ADMIN_USER_IDS: ADMIN }), null);
  assert.equal(resolveOpsAdminId("   ", { NODE_ENV: "production", OPS_ADMIN_USER_IDS: ADMIN }), null);
});

test("sin OPS_ADMIN_USER_IDS, o con la lista vacía, nadie entra", () => {
  for (const list of [undefined, "", "  ", ",,", "no-es-un-uuid"]) {
    assert.equal(resolveOpsAdminId(ADMIN, { NODE_ENV: "production", OPS_ADMIN_USER_IDS: list }), null, `lista ${JSON.stringify(list)}`);
    assert.equal(resolveOpsAdminId(ADMIN, { NODE_ENV: "development", OPS_ADMIN_USER_IDS: list, DEV_OPS_USER_ID: ADMIN }), null);
  }
});

test("la lista admite varios ids con espacios y mayúsculas, e ignora lo que no es UUID", () => {
  const ids = parseOpsAdminIds(` ${ADMIN.toUpperCase()} , ${OTHER},basura,,${"x".repeat(36)}`);
  assert.deepEqual([...ids].sort(), [ADMIN, OTHER].sort());
  assert.equal(resolveOpsAdminId(OTHER, { OPS_ADMIN_USER_IDS: `${ADMIN}, ${OTHER}` }), OTHER);
  assert.equal(resolveOpsAdminId(THIRD, { OPS_ADMIN_USER_IDS: `${ADMIN}, ${OTHER}` }), null);
});

test("un id de sesión que no es UUID no entra aunque figure en la lista", () => {
  assert.equal(resolveOpsAdminId("admin", { OPS_ADMIN_USER_IDS: "admin" }), null);
});

test("DEV_OPS_USER_ID sustituye la sesión solo fuera de producción", () => {
  const env = { OPS_ADMIN_USER_IDS: ADMIN, DEV_OPS_USER_ID: ADMIN };
  assert.equal(resolveOpsAdminId(null, { ...env, NODE_ENV: "development" }), ADMIN);
  assert.equal(resolveOpsAdminId(null, { ...env, NODE_ENV: "test" }), ADMIN);
  assert.equal(resolveOpsAdminId(null, { ...env }), ADMIN, "NODE_ENV ausente no es producción");
  assert.equal(resolveOpsAdminId(null, { ...env, NODE_ENV: "production" }), null, "en producción se ignora");
  assert.equal(effectiveOpsUserId(null, { ...env, NODE_ENV: "production" }), null);
});

test("el override de desarrollo no da acceso a un id que no está en la lista", () => {
  assert.equal(resolveOpsAdminId(null, { NODE_ENV: "development", OPS_ADMIN_USER_IDS: ADMIN, DEV_OPS_USER_ID: OTHER }), null);
});

test("una sesión real manda sobre el override: otro usuario con sesión sigue sin entrar", () => {
  const env = { NODE_ENV: "development", OPS_ADMIN_USER_IDS: ADMIN, DEV_OPS_USER_ID: ADMIN };
  assert.equal(resolveOpsAdminId(OTHER, env), null);
  assert.equal(effectiveOpsUserId(OTHER, env), OTHER);
});
