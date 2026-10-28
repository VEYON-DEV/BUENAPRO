import { NextResponse } from "next/server";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import { seaceUnavailableJson } from "@/server/http/seaceUnavailable";
import { startApplication } from "@/server/services/applications";
import { SeaceUnavailable } from "@/server/services/seaceEgress";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const tenantId = await requireTenantId();
    const actorId = await currentUserId();
    const idContrato = Number((await context.params).id);
    if (!Number.isSafeInteger(idContrato)) return NextResponse.json({ error: "Invalid contract" }, { status: 400 });
    const result = await startApplication(tenantId, idContrato, actorId);
    if ("error" in result) return NextResponse.json({ error: result.error }, { status: result.status });
    return NextResponse.json({ data: result.data });
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof SeaceUnavailable) return seaceUnavailableJson("SEACE no está disponible para iniciar la postulación. Inténtalo en unos minutos.");
    const code = error instanceof Error ? error.message : "APPLICATION_START_FAILED";
    const status = code === "SEACE_NOT_CONNECTED" || code === "SEACE_REQUIRES_2FA" ? 409 : 502;
    return NextResponse.json({ code, error: status === 409 ? "Conecta SEACE para comenzar la postulación." : "No se pudo iniciar la postulación." }, { status });
  }
}
