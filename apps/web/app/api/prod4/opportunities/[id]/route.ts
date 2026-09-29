import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getProd4OpportunityForTenant } from "@/server/services/prod4Opportunities";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  if (!/^[1-9]\d{0,18}$/.test(id)) {
    return NextResponse.json({ error: "Invalid procedure ID" }, { status: 400 });
  }
  const result = await getProd4OpportunityForTenant(tenantId, id);
  if (!result) return NextResponse.json({ error: "Not found" }, { status: 404 });
  return NextResponse.json(result);
}
