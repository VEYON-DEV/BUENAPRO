import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import {
  deleteKnowledgeItem,
  updateKnowledgeItem,
} from "@/server/services/companyLibrary";

type Params = Promise<{ itemId: string }>;

export async function PATCH(request: Request, context: { params: Params }) {
  const tenantId = await requireTenantId();
  const { itemId } = await context.params;
  const data = await updateKnowledgeItem(
    tenantId,
    itemId,
    await request.json().catch(() => ({})),
  );
  return data
    ? NextResponse.json({ data })
    : NextResponse.json({ error: "Dato no encontrado" }, { status: 404 });
}

export async function DELETE(_request: Request, context: { params: Params }) {
  const tenantId = await requireTenantId();
  const { itemId } = await context.params;
  const deleted = await deleteKnowledgeItem(tenantId, itemId);
  return deleted
    ? NextResponse.json({ data: { id: deleted.id } })
    : NextResponse.json({ error: "Dato no encontrado" }, { status: 404 });
}
