import { NextResponse } from "next/server";
import { requireTenantId, requireTenantRole } from "@/server/auth/tenant";
import { seaceUnavailableJson } from "@/server/http/seaceUnavailable";
import { connectSeace, disconnectSeace, seaceConnectionStatus } from "@/server/services/seaceAuthenticated";
import { SeaceUnavailable } from "@/server/services/seaceEgress";

function message(error: unknown) {
  const typed = error as Error & { status?: number; code?: string };
  const value = error instanceof Error ? error.message : "SEACE_CONNECTION_FAILED";
  if (value.includes("SEACE_CREDENTIALS_KEY")) return "La conexión segura no está configurada en el servidor.";
  if (value === "SEACE_INVALID_CREDENTIALS") return "SEACE rechazó el usuario o la contraseña.";
  if (typed.status) return `SEACE devolvió un error ${typed.status} durante el inicio de sesión.`;
  return "No se pudo conectar con SEACE. Revisa tus credenciales e inténtalo nuevamente.";
}

function safeLog(error: unknown) {
  const typed = error as Error & { status?: number; code?: string };
  console.warn("seace_connection_failed", {
    code: typed.code ?? typed.name ?? "UNKNOWN",
    status: typed.status ?? null,
  });
}

export async function GET() {
  try {
    return NextResponse.json(await seaceConnectionStatus(await requireTenantId()));
  } catch (error) {
    if (error instanceof Response) return error;
    return NextResponse.json({ error: message(error) }, { status: 503 });
  }
}

export async function PUT(request: Request) {
  try {
    const { tenantId, userId } = await requireTenantRole();
    const body = await request.json();
    const username = String(body.username ?? "").trim();
    const password = String(body.password ?? "");
    if (!/^\d{8,12}$/.test(username) || password.length < 8) {
      return NextResponse.json({ error: "Ingresa un usuario SEACE válido y su contraseña." }, { status: 400 });
    }
    const result = await connectSeace(tenantId, userId, username, password);
    if (result.requires2fa) {
      return NextResponse.json({ ...result, error: "La cuenta requiere 2FA; esta verificación se habilitará en una siguiente versión." }, { status: 409 });
    }
    return NextResponse.json(result);
  } catch (error) {
    if (error instanceof Response) return error;
    safeLog(error);
    if (error instanceof SeaceUnavailable) return seaceUnavailableJson("SEACE no está disponible en este momento. Inténtalo en unos minutos.");
    return NextResponse.json({ error: message(error) }, { status: 502 });
  }
}

export async function DELETE() {
  try {
    const { tenantId, userId } = await requireTenantRole();
    await disconnectSeace(tenantId, userId);
    return NextResponse.json({ disconnected: true });
  } catch (error) {
    if (error instanceof Response) return error;
    return NextResponse.json({ error: "No se pudo desconectar SEACE." }, { status: 500 });
  }
}
