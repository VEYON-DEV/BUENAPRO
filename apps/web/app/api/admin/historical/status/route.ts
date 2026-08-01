import { NextRequest, NextResponse } from "next/server";
import { adminError, unauthorizedUnlessInternal } from "@/app/api/admin/_utils";
import { getHistoricalStatus } from "@/server/services/admin";

export async function GET(request: NextRequest) {
  const unauthorized = unauthorizedUnlessInternal(request);
  if (unauthorized) return unauthorized;
  try {
    const raw = new URL(request.url).searchParams.get("segment");
    const segment = raw ? Number(raw) : undefined;
    return NextResponse.json(await getHistoricalStatus(segment));
  } catch (error) {
    return adminError(error);
  }
}
