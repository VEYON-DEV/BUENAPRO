import { manualGradingDailyLimit } from "@/server/services/manualGrading";
import { opsQuery } from "./db.ts";
import { shapeCosts, shapeQuota } from "./model.ts";
import type { CostRow, CostsTable, QuotaRow, QuotaTable } from "./model.ts";
import { costsSql } from "./steps.ts";
import type { OpsFilters } from "./filters.ts";
import { dailyParams } from "./daily.ts";
import type { OpsCompany } from "./types.ts";

export async function getCosts(filters: Pick<OpsFilters, "from" | "to" | "company" | "days">): Promise<CostsTable> {
  const rows = await opsQuery<CostRow>(costsSql(), dailyParams(filters));
  return shapeCosts(filters.days, rows);
}

/**
 * Calificaciones manuales por empresa y día de Lima. El tope es por tenant y suma menores y mayores
 * (MANUAL_GRADING_DAILY_LIMIT, 100 por omisión), igual que el contador de la web.
 */
export async function getManualQuota(
  filters: Pick<OpsFilters, "from" | "to" | "company" | "days">,
  companies: readonly OpsCompany[],
): Promise<QuotaTable> {
  const scope = filters.company ? companies.filter((company) => company.id === filters.company) : [...companies];
  const tenants = [...new Set(scope.map((company) => company.tenantId))];
  const rows = tenants.length
    ? await opsQuery<QuotaRow>(
        `SELECT r.tenant_id::text AS tenant_id, r.lima_day::text AS day, count(*)::int AS used
         FROM manual_grading_requests r
         WHERE r.lima_day BETWEEN $1::date AND $2::date AND r.tenant_id = ANY($3::uuid[])
         GROUP BY 1, 2`,
        [filters.from, filters.to, tenants],
      )
    : [];
  return shapeQuota(filters.days, scope, rows, manualGradingDailyLimit());
}
