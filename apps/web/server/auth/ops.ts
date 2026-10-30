import { notFound } from "next/navigation";
import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/server/auth/options";
import { resolveOpsAdminId } from "./opsAccess.ts";

/**
 * Compuerta del panel de operación. Quien no esté autorizado, haya iniciado sesión o no, recibe lo mismo que
 * recibiría ante una ruta inexistente: `notFound()` en las páginas y un 404 JSON en las APIs. Nunca 401 ni 403:
 * el panel no debe delatar que existe.
 */

/** Id del administrador de operación de la sesión actual, o null. */
export async function currentOpsAdminId(): Promise<string | null> {
  // Una sesión ilegible cuenta como «sin sesión»: el resultado es el mismo 404, nunca un error que delate la ruta.
  const session = await getServerSession(authOptions).catch(() => null);
  return resolveOpsAdminId(session?.user?.id, {
    NODE_ENV: process.env.NODE_ENV,
    OPS_ADMIN_USER_IDS: process.env.OPS_ADMIN_USER_IDS,
    DEV_OPS_USER_ID: process.env.DEV_OPS_USER_ID,
  });
}

/** Para páginas: devuelve el id del administrador o lanza `notFound()`. */
export async function requireOpsAdmin(): Promise<string> {
  const userId = await currentOpsAdminId();
  if (!userId) notFound();
  return userId;
}

/** El 404 JSON de las APIs; mismo cuerpo que una ruta inexistente de la aplicación. */
export function opsApiNotFound() {
  return NextResponse.json({ error: "Not found" }, { status: 404, headers: { "Cache-Control": "no-store" } });
}

/** Para route handlers: null si el acceso está permitido; si no, la respuesta 404 que se debe devolver. */
export async function opsApiGuard(): Promise<NextResponse | null> {
  return (await currentOpsAdminId()) ? null : opsApiNotFound();
}
