/**
 * Regla de acceso del panel de operación («ops»), sin dependencias para poder probarla.
 *
 * Solo entra quien tenga su id de usuario en `OPS_ADMIN_USER_IDS` (UUID separados por coma). Sin la variable,
 * o con una lista vacía, nadie entra. Fuera de producción, `DEV_OPS_USER_ID` hace de id de sesión cuando no hay
 * una sesión real (mismo patrón que DEV_TENANT_ID en tenant.ts); en producción se ignora.
 */

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type OpsAccessEnv = {
  NODE_ENV?: string;
  OPS_ADMIN_USER_IDS?: string;
  DEV_OPS_USER_ID?: string;
};

/** Ids válidos de `OPS_ADMIN_USER_IDS`, en minúsculas; descarta vacíos y entradas que no son UUID. */
export function parseOpsAdminIds(raw: string | undefined): Set<string> {
  return new Set(
    (raw ?? "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter((value) => UUID_PATTERN.test(value)),
  );
}

/** Id de usuario con el que se evalúa el acceso: el de la sesión o, solo fuera de producción, el de desarrollo. */
export function effectiveOpsUserId(sessionUserId: string | null | undefined, env: OpsAccessEnv): string | null {
  const fromSession = typeof sessionUserId === "string" ? sessionUserId.trim() : "";
  if (fromSession) return fromSession;
  if (env.NODE_ENV !== "production") {
    const dev = env.DEV_OPS_USER_ID?.trim();
    if (dev) return dev;
  }
  return null;
}

/** El id efectivo de este usuario, si está autorizado; si no, null (la ruta responde 404 como si no existiera). */
export function resolveOpsAdminId(sessionUserId: string | null | undefined, env: OpsAccessEnv): string | null {
  const userId = effectiveOpsUserId(sessionUserId, env);
  if (!userId || !UUID_PATTERN.test(userId)) return null;
  return parseOpsAdminIds(env.OPS_ADMIN_USER_IDS).has(userId.toLowerCase()) ? userId.toLowerCase() : null;
}
