import type { NextRequest } from "next/server";
import { opsApiGuard, opsApiNotFound } from "@/server/auth/ops";
import { isTraceId, parseSource } from "@/server/services/ops/filters";
import { getTrace } from "@/server/services/ops/trace";
import { opsJson, opsServerError } from "../../../_utils";

export const dynamic = "force-dynamic";

/** Traza cronológica de una licitación: /api/ops/trace/menores/<id_contrato> o /mayores/<id_convocatoria>. */
export async function GET(_request: NextRequest, { params }: { params: Promise<{ source: string; id: string }> }) {
  const blocked = await opsApiGuard();
  if (blocked) return blocked;
  const { source: rawSource, id } = await params;
  const source = parseSource(rawSource);
  if (!source || !isTraceId(id)) return opsApiNotFound();
  try {
    const trace = await getTrace(source, id);
    return trace ? opsJson(trace) : opsApiNotFound();
  } catch (error) {
    return opsServerError(error);
  }
}
