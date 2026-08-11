import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { createKnowledgeItem } from "@/server/services/companyLibrary";

export async function POST(request: Request) {
  const tenantId = await requireTenantId();
  const data = await createKnowledgeItem(
    tenantId,
    await request.json().catch(() => ({})),
  );
  return data
    ? NextResponse.json({ data }, { status: 201 })
    : NextResponse.json({ error: "Agrega un titulo para guardar el dato." }, { status: 400 });
}
