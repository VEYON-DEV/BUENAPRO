import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { seaceUnavailableJson } from "@/server/http/seaceUnavailable";
import { tenantCanAccessContract } from "@/server/services/crud";
import { authenticatedSeaceJson } from "@/server/services/seaceAuthenticated";
import { SeaceUnavailable } from "@/server/services/seaceEgress";

function failure(error: unknown) {
  if (error instanceof SeaceUnavailable) return seaceUnavailableJson("SEACE no respondió a la consulta.");
  const code = error instanceof Error ? error.message : "SEACE_ERROR";
  const status = code === "SEACE_NOT_CONNECTED" || code === "SEACE_REQUIRES_2FA" ? 409 : 502;
  return NextResponse.json({ code, error: status === 409 ? "Conecta tu cuenta SEACE para ver las consultas oficiales." : "SEACE no respondió a la consulta." }, { status });
}

export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const tenantId = await requireTenantId();
    const id = Number((await context.params).id);
    if (!(await tenantCanAccessContract(tenantId, id))) return NextResponse.json({ error: "Not found" }, { status: 404 });
    const search = new URL(request.url).searchParams;
    const estado = Number(search.get("estado") ?? 0);
    const page = Math.max(1, Number(search.get("page") ?? 1));
    const pageSize = Math.min(50, Math.max(1, Number(search.get("page_size") ?? 10)));
    const [states, consultations] = await Promise.all([
      authenticatedSeaceJson(tenantId, "/maestra/maestras/listar-estados-consulta"),
      authenticatedSeaceJson(
        tenantId,
        `/consulta/proveedores/listar?id_contrato=${id}&estado_consulta=${estado}&campo_orden=1&orden=2&page=${page}&page_size=${pageSize}`,
      ),
    ]);
    return NextResponse.json({ states, consultations, syncedAt: new Date().toISOString() });
  } catch (error) {
    if (error instanceof Response) return error;
    return failure(error);
  }
}
