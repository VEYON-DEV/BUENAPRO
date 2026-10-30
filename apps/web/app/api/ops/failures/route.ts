import type { NextRequest } from "next/server";
import { opsApiGuard } from "@/server/auth/ops";
import { listOpsCompanies } from "@/server/services/ops/companies";
import { getFailures } from "@/server/services/ops/failures";
import { parseOpsFilters } from "@/server/services/ops/filters";
import { opsBadRequest, opsJson, opsServerError } from "../_utils";

export const dynamic = "force-dynamic";

/** Fallas de un día de Lima (hoy por omisión), por paso y motivo, con hasta 50 elementos por grupo. */
export async function GET(request: NextRequest) {
  const blocked = await opsApiGuard();
  if (blocked) return blocked;
  const filters = parseOpsFilters(request.nextUrl.searchParams);
  const invalid = filters.errors.filter((name) => name !== "range");
  if (invalid.length) return opsBadRequest(invalid);
  try {
    const failures = await getFailures(filters, await listOpsCompanies());
    return opsJson({ day: filters.day, company: filters.company, ...failures });
  } catch (error) {
    return opsServerError(error);
  }
}
