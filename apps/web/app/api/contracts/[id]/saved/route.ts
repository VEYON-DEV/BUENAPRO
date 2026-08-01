import { NextResponse } from "next/server";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import { asPositiveInteger } from "@/server/services/crud";
import {
  saveContractForTenant,
  unsaveContractForTenant,
} from "@/server/services/savedContracts";

function contractId(value: string) {
  try {
    return asPositiveInteger(value, "id");
  } catch {
    return null;
  }
}

export async function PUT(_request: Request, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const actorId = await currentUserId();
  const id = contractId((await context.params).id);
  if (!id) return NextResponse.json({ error: "Invalid contract id" }, { status: 400 });

  const result = await saveContractForTenant(tenantId, id, actorId);
  if ("error" in result) return NextResponse.json({ error: result.error }, { status: result.status });
  return NextResponse.json({ data: result.data });
}

export async function DELETE(_request: Request, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const id = contractId((await context.params).id);
  if (!id) return NextResponse.json({ error: "Invalid contract id" }, { status: 400 });

  const result = await unsaveContractForTenant(tenantId, id);
  return NextResponse.json({ data: result.data });
}
