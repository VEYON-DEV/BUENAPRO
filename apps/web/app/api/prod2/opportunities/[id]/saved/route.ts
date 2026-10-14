import { NextResponse } from "next/server";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import {
  isMissingSavedOpportunitiesTable,
  saveProd2OpportunityForTenant,
  unsaveProd2OpportunityForTenant,
} from "@/server/services/savedOpportunities";

const convocatoriaPattern = /^[1-9]\d{0,18}$/;
function validConvocatoriaId(id: string): boolean {
  return convocatoriaPattern.test(id) && BigInt(id) <= 9223372036854775807n;
}
const unavailable = () => NextResponse.json({ error: "Guardar del Buscador SEACE aún no está habilitado." }, { status: 503 });

export async function PUT(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!validConvocatoriaId(id)) return NextResponse.json({ error: "Invalid convocatoria ID" }, { status: 400 });
  try {
    const tenantId = await requireTenantId();
    const result = await saveProd2OpportunityForTenant(tenantId, id, await currentUserId());
    if ("error" in result) return NextResponse.json({ error: result.error }, { status: result.status });
    return NextResponse.json({ data: result.data });
  } catch (error) {
    if (error instanceof Response) return error;
    if (isMissingSavedOpportunitiesTable(error)) return unavailable();
    throw error;
  }
}

export async function DELETE(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!validConvocatoriaId(id)) return NextResponse.json({ error: "Invalid convocatoria ID" }, { status: 400 });
  try {
    const tenantId = await requireTenantId();
    return NextResponse.json({ data: (await unsaveProd2OpportunityForTenant(tenantId, id)).data });
  } catch (error) {
    if (error instanceof Response) return error;
    if (isMissingSavedOpportunitiesTable(error)) return unavailable();
    throw error;
  }
}
