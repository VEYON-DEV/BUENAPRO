import type { NextRequest } from "next/server";
import { opsApiGuard } from "@/server/auth/ops";
import { listOpsCompanies } from "@/server/services/ops/companies";
import { getCosts, getManualQuota } from "@/server/services/ops/costs";
import { getDailyFunnel, getPendingJevNow } from "@/server/services/ops/daily";
import { parseOpsFilters } from "@/server/services/ops/filters";
import { opsBadRequest, opsJson, opsServerError } from "../_utils";

export const dynamic = "force-dynamic";

/** Embudo diario de menores y mayores, costos y cupo de calificaciones manuales. Solo lectura. */
export async function GET(request: NextRequest) {
  const blocked = await opsApiGuard();
  if (blocked) return blocked;
  const filters = parseOpsFilters(request.nextUrl.searchParams);
  const invalid = filters.errors.filter((name) => name !== "day");
  if (invalid.length) return opsBadRequest(invalid);
  try {
    const companies = await listOpsCompanies();
    const [menores, mayores, costs, quota, pendingJevNow] = await Promise.all([
      getDailyFunnel("menores", filters),
      getDailyFunnel("mayores", filters),
      getCosts(filters),
      getManualQuota(filters, companies),
      getPendingJevNow(filters.company),
    ]);
    return opsJson({
      range: filters.range,
      from: filters.from,
      to: filters.to,
      company: filters.company,
      companies,
      pendingJevNow,
      menores,
      mayores,
      costs,
      quota,
    });
  } catch (error) {
    return opsServerError(error);
  }
}
