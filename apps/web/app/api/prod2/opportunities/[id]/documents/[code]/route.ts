import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getProd2DocumentUrl, Prod2RequestError } from "@/server/services/prod2Opportunities";

export async function GET(_request: Request, context: { params: Promise<{ id: string; code: string }> }) {
  try {
    await requireTenantId();
    const { id, code } = await context.params;
    if (!/^[1-9]\d{0,18}$/.test(id) || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(code)) {
      return NextResponse.json({ error: "Invalid document identity" }, { status: 400 });
    }
    const url = await getProd2DocumentUrl(id, code);
    if (!url) return NextResponse.json({ error: "El documento no está disponible. Consulta la ficha en SEACE." }, { status: 404 });
    return NextResponse.redirect(url, { headers: { "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer" } });
  } catch (error) {
    if (error instanceof Response) return error;
    if (error instanceof Prod2RequestError) return NextResponse.json({ error: error.message }, {
      status: error.status, headers: { "Cache-Control": "private, no-store" },
    });
    throw error;
  }
}
