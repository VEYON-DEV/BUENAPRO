import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { query } from "@/server/db/client";
import { seaceUnavailableJson } from "@/server/http/seaceUnavailable";
import { tenantCanAccessContract } from "@/server/services/crud";
import { authenticatedSeaceJson } from "@/server/services/seaceAuthenticated";
import { SeaceUnavailable } from "@/server/services/seaceEgress";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const tenantId = await requireTenantId();
    const id = Number((await context.params).id);
    if (!(await tenantCanAccessContract(tenantId, id))) return NextResponse.json({ error: "Not found" }, { status: 404 });
    const [quotation, contract, documents] = await Promise.all([
      authenticatedSeaceJson(tenantId, `/cotizacion/cotizaciones/listar-completo?id_contrato=${id}`),
      authenticatedSeaceJson(tenantId, `/contratacion/contrataciones/obtener-completo?id_contrato=${id}`),
      query(
        `SELECT id,filename,mime,doc_class,categoria,size_original_bytes,raw_file_json FROM contract_documents WHERE id_contrato=$1 ORDER BY categoria,id`,
        [id],
      ),
    ]);
    return NextResponse.json({ quotation, contract, documents: documents.rows, syncedAt: new Date().toISOString() });
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof SeaceUnavailable) return seaceUnavailableJson("SEACE no respondió con la cotización.");
    const code = error instanceof Error ? error.message : "SEACE_ERROR";
    const status = code === "SEACE_NOT_CONNECTED" || code === "SEACE_REQUIRES_2FA" ? 409 : 502;
    return NextResponse.json({ code, error: status === 409 ? "Conecta tu cuenta SEACE para preparar la cotización." : "SEACE no respondió con la cotización." }, { status });
  }
}
