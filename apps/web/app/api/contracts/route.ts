import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { ContractRequestError, listContractsForTenant } from "@/server/services/contracts";

export async function GET(request: NextRequest) {
  try {
    const tenantId = await requireTenantId();
    const result = await listContractsForTenant(tenantId, request.nextUrl.searchParams);
    return NextResponse.json(result);
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof ContractRequestError) return NextResponse.json({ error: error.message }, { status: error.status });
    throw error;
  }
}
