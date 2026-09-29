import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { listProd4OpportunitiesForTenant, parseProd4ListParams } from "@/server/services/prod4Opportunities";

export async function GET(request: NextRequest) {
  const tenantId = await requireTenantId();
  try {
    const params = parseProd4ListParams(request.nextUrl.searchParams);
    return NextResponse.json(await listProd4OpportunitiesForTenant(tenantId, params));
  } catch (error) {
    if (error instanceof Error && /^(object|state|q|page|page_size) must/.test(error.message)) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
}
