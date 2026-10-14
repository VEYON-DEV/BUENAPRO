import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getProd2OpportunityForTenant, Prod2RequestError } from "@/server/services/prod2Opportunities";

export async function GET(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!/^[1-9]\d{0,18}$/.test(id)) return NextResponse.json({ error: "Invalid convocatoria ID" }, { status: 400 });
  try {
    const tenantId = await requireTenantId();
    const detail = await getProd2OpportunityForTenant(tenantId, id, request.nextUrl.searchParams.get("profile_id"));
    return detail ? NextResponse.json(detail) : NextResponse.json({ error: "Not found" }, { status: 404 });
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof Prod2RequestError) return NextResponse.json({ error: error.message }, { status: error.status });
    throw error;
  }
}
