import { NextRequest, NextResponse } from "next/server";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import { analyzeMatchDirect } from "@/server/services/matchAnalysis";

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const tenantId = await requireTenantId();
  const actorId = await currentUserId();
  const { id } = await context.params;
  const idContrato = Number(id);
  if (!Number.isSafeInteger(idContrato) || idContrato <= 0) {
    return NextResponse.json({ error: "Contrato invalido" }, { status: 400 });
  }
  const body = await request.json().catch(() => ({}));
  const profileId = body?.profile_id;
  if (profileId != null && (typeof profileId !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(profileId))) {
    return NextResponse.json({ error: "Perfil inválido" }, { status: 400 });
  }

  try {
    const data = await analyzeMatchDirect({
      tenantId,
      idContrato,
      profileId,
      actorId,
      force: Boolean(body?.force),
    });
    return NextResponse.json({ data });
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : "No se pudo completar el análisis.";
    const status = message.includes("perfil activo")
      ? 409
      : message.includes("licitación")
        ? 404
        : 422;
    return NextResponse.json({ error: message }, { status });
  }
}
