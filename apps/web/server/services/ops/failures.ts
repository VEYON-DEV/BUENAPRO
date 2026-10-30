import { FAILURE_ITEMS_LIMIT } from "./filters.ts";
import type { OpsFilters } from "./filters.ts";
import { opsQuery } from "./db.ts";
import { loadItemLabels } from "./itemLabels.ts";
import { companyLabels, groupFailures } from "./model.ts";
import type { FailureGroup, FailureRow } from "./model.ts";
import { failureSteps, failuresSql } from "./steps.ts";
import { OPS_SOURCES } from "./types.ts";
import type { OpsCompany, OpsSource } from "./types.ts";

export type FailuresResult = Record<OpsSource, FailureGroup[]>;

/** Fallas de un día de Lima, por paso y motivo, con hasta 50 elementos por grupo. */
export async function getFailures(
  filters: Pick<OpsFilters, "day" | "company">,
  companies: readonly OpsCompany[],
): Promise<FailuresResult> {
  const labels = companyLabels(companies);
  const entries = await Promise.all(
    OPS_SOURCES.map(async (source) => {
      const rows = await opsQuery<FailureRow>(failuresSql(failureSteps(source)), [filters.day, filters.day, filters.company, FAILURE_ITEMS_LIMIT]);
      const itemLabels = await loadItemLabels(
        source,
        rows.map((row) => row.item_id).filter((id): id is string => id != null),
      );
      return [source, groupFailures(source, rows, itemLabels, labels, filters.company)] as const;
    }),
  );
  return Object.fromEntries(entries) as FailuresResult;
}
