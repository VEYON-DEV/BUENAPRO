import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getProcurementTimeline, parseTimelineParams } from "@/server/services/procurementTimeline";

export async function GET(request: NextRequest) {
  try {
    const tenantId = await requireTenantId();
    const params = parseTimelineParams(request.nextUrl.searchParams);
    const result = await getProcurementTimeline(tenantId, params);
    return NextResponse.json(result, { headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    if (error instanceof Response && (error.status === 401 || error.status === 403)) {
      return NextResponse.json({ error: "Se requiere una sesión válida" }, { status: error.status, headers: { "Cache-Control": "private, no-store" } });
    }
    if (error instanceof Error && /^(source|q) must/.test(error.message)) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
}
