import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getFeed } from "@/server/services/feed";
import { CommercialFilterError } from "@/lib/commercialTaxonomy";

export async function GET(request: NextRequest) {
  try {
    const tenantId = await requireTenantId();
    const rows = await getFeed(tenantId, request.nextUrl.searchParams);
    return NextResponse.json({ data: rows });
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof CommercialFilterError) return NextResponse.json({ error: error.message }, { status: error.status });
    throw error;
  }
}
