import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { pool } from "@/server/db/client";
import { assertProd4AnalysisAvailable, getProd4AnalysisJob, Prod4AnalysisError } from "@/server/services/prod4MatchQueue";

const validId = (id: string) => /^[1-9]\d{0,15}$/.test(id) && Number.isSafeInteger(Number(id));
function failure(error: unknown) {
  return error instanceof Prod4AnalysisError
    ? NextResponse.json({ error: error.message, code: error.code }, { status: error.status })
    : NextResponse.json({ error: "No se pudo gestionar el análisis." }, { status: 500 });
}

export async function POST(_request: NextRequest, _context: { params: Promise<{ id: string }> }) {
  await requireTenantId();
  try { assertProd4AnalysisAvailable(); }
  catch (error) { return failure(error); }
}

export async function GET(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  const jobId = request.nextUrl.searchParams.get("job_id") ?? "";
  if (!validId(id) || !validId(jobId)) return NextResponse.json({ error: "Identificador inválido" }, { status: 400 });
  const client = await pool.connect();
  try { return NextResponse.json({ data: await getProd4AnalysisJob(client, tenantId, id, jobId) }); }
  catch (error) { return failure(error); }
  finally { client.release(); }
}
