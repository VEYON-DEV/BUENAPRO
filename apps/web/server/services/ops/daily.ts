import { opsQuery } from "./db.ts";
import { shapeDaily } from "./model.ts";
import type { CountRow, DailyTable } from "./model.ts";
import { columnSteps, countsSql } from "./steps.ts";
import type { OpsParams } from "./steps.ts";
import type { OpsFilters } from "./filters.ts";
import type { OpsSource } from "./types.ts";

export function dailyParams(filters: Pick<OpsFilters, "from" | "to" | "company">): OpsParams {
  return [filters.from, filters.to, filters.company, 0];
}

/** Embudo por día de un origen: un conteo por paso y día de Lima, agrupado en SQL. */
export async function getDailyFunnel(source: OpsSource, filters: Pick<OpsFilters, "from" | "to" | "company" | "days">): Promise<DailyTable> {
  const rows = await opsQuery<CountRow>(countsSql(columnSteps(source)), dailyParams(filters));
  return shapeDaily(source, filters.days, rows);
}

/** Decisiones JEV de menores que siguen pendientes ahora mismo (sin límite de días). */
export async function getPendingJevNow(company: string | null): Promise<number> {
  const rows = await opsQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM prod6_profile_decisions d
     WHERE d.decided_by = 'jev' AND d.decision = 'pending' AND ($1::uuid IS NULL OR d.profile_id = $1::uuid)`,
    [company],
  );
  return rows[0]?.n ?? 0;
}
