import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getMarketIntelligence, parseMarketFilters } from "@/server/services/market";

export async function GET(request: NextRequest) {
  const tenantId = await requireTenantId();
  return NextResponse.json(await getMarketIntelligence(tenantId, parseMarketFilters(request.nextUrl.searchParams)));
}
