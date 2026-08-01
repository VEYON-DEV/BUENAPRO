import { NextRequest, NextResponse } from "next/server";
import { adminError, badRequest, unauthorizedUnlessInternal } from "@/app/api/admin/_utils";
import { enqueueHistoricalBackfill } from "@/server/services/admin";

export async function POST(request: NextRequest) {
  const unauthorized = unauthorizedUnlessInternal(request);
  if (unauthorized) return unauthorized;
  try {
    const body = await request.json().catch(() => ({}));
    const segment = Number(body.segment ?? 81);
    const limit = body.limit == null ? undefined : Number(body.limit);
    const year = body.year == null ? new Date().getUTCFullYear() : Number(body.year);
    if (!Number.isInteger(segment) || segment < 1) return badRequest("segment inválido");
    if (limit != null && (!Number.isInteger(limit) || limit < 1 || limit > 10_000)) {
      return badRequest("limit debe estar entre 1 y 10000");
    }
    const job = await enqueueHistoricalBackfill({ segment, limit, year });
    if (!job) return NextResponse.json({ error: "Ya existe un backfill pendiente para el segmento" }, { status: 409 });
    return NextResponse.json({ data: job }, { status: 202 });
  } catch (error) {
    return adminError(error);
  }
}
