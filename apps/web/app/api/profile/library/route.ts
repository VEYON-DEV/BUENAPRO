import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { listCompanyLibrary } from "@/server/services/companyLibrary";

export async function GET() {
  const tenantId = await requireTenantId();
  return NextResponse.json({ data: await listCompanyLibrary(tenantId) });
}
