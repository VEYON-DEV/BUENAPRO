import { NextRequest, NextResponse } from "next/server";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import {
  directFailureReason,
  ManualGradingError,
  manualGradingUsage,
  minorGradingStatus,
  requestMinorGrading,
} from "@/server/services/manualGrading";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const ERROR_STATUS = {
  daily_limit_reached: 429,
  profile_not_found: 409,
  contract_not_found: 404,
} as const;

function parseContractId(value: string) {
  const idContrato = Number(value);
  return Number.isSafeInteger(idContrato) && idContrato > 0 ? idContrato : null;
}

function isValidProfileId(value: unknown): value is string | null | undefined {
  return value == null || (typeof value === "string" && UUID.test(value));
}

/** Estado de la última calificación manual de esta licitación y uso de hoy de la empresa. */
export async function GET(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  try {
    const tenantId = await requireTenantId();
    const idContrato = parseContractId((await context.params).id);
    if (!idContrato) return NextResponse.json({ error: "Contrato inválido" }, { status: 400 });
    const profileId = request.nextUrl.searchParams.get("profile_id");
    if (!isValidProfileId(profileId)) return NextResponse.json({ error: "Perfil inválido" }, { status: 400 });
    const data = await minorGradingStatus({ tenantId, idContrato, profileId });
    return NextResponse.json({ data }, { headers: { "cache-control": "no-store" } });
  } catch (error) {
    if (error instanceof Response) return error;
    return NextResponse.json({ error: "No se pudo consultar el estado de la calificación." }, { status: 500 });
  }
}

/**
 * Pide la calificación manual. 200 { data: { status: "done", result, usage } } si se evaluó en el momento;
 * 202 { data: { status: "queued", request, usage } } si queda en cola o ya había una en curso.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  let tenantId: string;
  let actorId: string | null;
  try {
    tenantId = await requireTenantId();
    actorId = await currentUserId();
  } catch (error) {
    if (error instanceof Response) return error;
    throw error;
  }
  const idContrato = parseContractId((await context.params).id);
  if (!idContrato) return NextResponse.json({ error: "Contrato inválido" }, { status: 400 });
  const body = await request.json().catch(() => ({}));
  const profileId = body?.profile_id;
  if (!isValidProfileId(profileId)) return NextResponse.json({ error: "Perfil inválido" }, { status: 400 });

  try {
    const outcome = await requestMinorGrading({
      tenantId,
      idContrato,
      profileId,
      actorId,
      force: Boolean(body?.force),
    });
    const usage = await manualGradingUsage(tenantId);
    if (outcome.status === "done") {
      return NextResponse.json({ data: { status: "done", result: outcome.data, usage } });
    }
    return NextResponse.json({ data: { status: "queued", request: outcome.request, usage } }, { status: 202 });
  } catch (error) {
    if (error instanceof ManualGradingError) {
      const usage = error.code === "daily_limit_reached" ? { used: error.used, limit: error.limit } : undefined;
      return NextResponse.json(
        { error: error.message, code: error.code, ...(usage ? { usage } : {}) },
        { status: ERROR_STATUS[error.code] },
      );
    }
    if (error instanceof Response) return error;
    const message = error instanceof Error ? error.message : "No se pudo completar la calificación.";
    return NextResponse.json({ error: message, code: "grading_failed", reason: directFailureReason(error) }, { status: 422 });
  }
}
