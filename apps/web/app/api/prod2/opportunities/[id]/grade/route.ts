import { NextRequest, NextResponse } from "next/server";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import {
  MANUAL_GRADING_ERROR_STATUS,
  ManualGradingError,
  majorGradingStatus,
  manualGradingUsage,
  requestMajorGrading,
} from "@/server/services/manualGrading";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
// Mismo formato que la ficha: un bigint sin ceros a la izquierda. Se conserva como texto para no perder precisión.
const CONVOCATORIA_ID = /^[1-9]\d{0,18}$/;
const MAX_BIGINT = "9223372036854775807";

function isValidConvocatoriaId(value: string) {
  // Con 19 dígitos la comparación de texto equivale a la numérica; así un id fuera de rango no llega a la consulta.
  return CONVOCATORIA_ID.test(value) && (value.length < MAX_BIGINT.length || value <= MAX_BIGINT);
}

function isValidProfileId(value: unknown): value is string | null | undefined {
  return value == null || (typeof value === "string" && UUID.test(value));
}

/** Estado de la última calificación manual de esta convocatoria y uso de hoy de la empresa. */
export async function GET(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  try {
    const tenantId = await requireTenantId();
    const idConvocatoria = (await context.params).id;
    if (!isValidConvocatoriaId(idConvocatoria)) return NextResponse.json({ error: "Convocatoria inválida" }, { status: 400 });
    const profileId = request.nextUrl.searchParams.get("profile_id");
    if (!isValidProfileId(profileId)) return NextResponse.json({ error: "Perfil inválido" }, { status: 400 });
    const data = await majorGradingStatus({ tenantId, idConvocatoria, profileId });
    return NextResponse.json({ data }, { headers: { "cache-control": "no-store" } });
  } catch (error) {
    if (error instanceof Response) return error;
    return NextResponse.json({ error: "No se pudo consultar el estado de la calificación." }, { status: 500 });
  }
}

/**
 * Pide la calificación manual. Mayores nunca evalúa en la web: 202 { data: { status: "queued", request, usage } }
 * con la solicitud nueva o la que ya estaba en curso. Las precondiciones no consumen cupo:
 * 404 sin convocatoria publicada; 409 jev_below_60, convocatoria_cerrada, already_graded o profile_not_found;
 * 429 al llegar al tope diario.
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
  const idConvocatoria = (await context.params).id;
  if (!isValidConvocatoriaId(idConvocatoria)) return NextResponse.json({ error: "Convocatoria inválida" }, { status: 400 });
  const body = await request.json().catch(() => ({}));
  const profileId = body?.profile_id;
  if (!isValidProfileId(profileId)) return NextResponse.json({ error: "Perfil inválido" }, { status: 400 });

  try {
    const outcome = await requestMajorGrading({ tenantId, idConvocatoria, profileId, actorId });
    const usage = await manualGradingUsage(tenantId);
    return NextResponse.json({ data: { status: "queued", request: outcome.request, usage } }, { status: 202 });
  } catch (error) {
    if (error instanceof ManualGradingError) {
      const usage = error.code === "daily_limit_reached" ? { used: error.used, limit: error.limit } : undefined;
      return NextResponse.json(
        { error: error.message, code: error.code, ...(usage ? { usage } : {}) },
        { status: MANUAL_GRADING_ERROR_STATUS[error.code] },
      );
    }
    if (error instanceof Response) return error;
    return NextResponse.json({ error: "No se pudo pedir la calificación.", code: "request_failed" }, { status: 500 });
  }
}
