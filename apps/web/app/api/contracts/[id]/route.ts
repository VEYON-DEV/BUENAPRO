import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { ContractRequestError, getContractForTenant } from "@/server/services/contracts";

export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const tenantId = await requireTenantId();
    const { id } = await context.params;
    const data = await getContractForTenant(tenantId, Number(id), new URL(request.url).searchParams.get("profile_id"));
    if (!data) return NextResponse.json({ error: "Not found" }, { status: 404 });
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof ContractRequestError) return NextResponse.json({ error: error.message }, { status: error.status });
    throw error;
  }
}
