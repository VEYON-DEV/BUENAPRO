import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { listProd2OpportunitiesForTenant, parseProd2ListParams, Prod2RequestError } from "@/server/services/prod2Opportunities";

export async function GET(request: NextRequest) {
  try {
    const tenantId = await requireTenantId();
    return NextResponse.json(await listProd2OpportunitiesForTenant(tenantId, parseProd2ListParams(request.nextUrl.searchParams)));
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof Prod2RequestError) return NextResponse.json({ error: error.message }, { status: error.status });
    throw error;
  }
}
