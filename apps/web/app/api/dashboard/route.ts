import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getDashboard } from "@/server/services/dashboard";

export async function GET() {
  return NextResponse.json(await getDashboard(await requireTenantId()));
}
