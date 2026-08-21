import { NextResponse } from "next/server";
import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import {
  getApplication,
  markApplicationSubmitted,
  updateApplication,
} from "@/server/services/applications";

export async function GET(
  _request: Request,
  context: { params: Promise<{ matchId: string }> },
) {
  const tenantId = await requireTenantId();
  const matchId = Number((await context.params).matchId);
  if (!Number.isSafeInteger(matchId))
    return NextResponse.json({ error: "Invalid application" }, { status: 400 });
  const data = await getApplication(tenantId, matchId);
  return data
    ? NextResponse.json({ data })
    : NextResponse.json({ error: "Not found" }, { status: 404 });
}

export async function PATCH(
  request: Request,
  context: { params: Promise<{ matchId: string }> },
) {
  const tenantId = await requireTenantId();
  const actorId = await currentUserId();
  const matchId = Number((await context.params).matchId);
  if (!Number.isSafeInteger(matchId))
    return NextResponse.json({ error: "Invalid application" }, { status: 400 });
  const body = await request.json();
  if (body.status === "submitted") {
    const result = await markApplicationSubmitted(tenantId, matchId, actorId);
    if (!result)
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    if ("error" in result)
      return NextResponse.json({ error: result.error }, { status: 409 });
    return NextResponse.json(result);
  }
  const data = await updateApplication(
    tenantId,
    matchId,
    body,
    actorId,
  );
  return data
    ? NextResponse.json({ data })
    : NextResponse.json({ error: "Not found" }, { status: 404 });
}
