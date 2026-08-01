import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getHistoricalComparables } from "@/server/services/historicalComparables";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const id = Number((await context.params).id);
  if (!Number.isInteger(id) || id <= 0) {
    return NextResponse.json({ error: "Contrato inválido" }, { status: 400 });
  }
  const data = await getHistoricalComparables(tenantId, id);
  if (!data) return NextResponse.json({ error: "Not found" }, { status: 404 });
  return NextResponse.json(data);
}
