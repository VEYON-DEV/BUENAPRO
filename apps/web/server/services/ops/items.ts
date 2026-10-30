import { ITEMS_LIMIT } from "./filters.ts";
import type { ItemsQuery } from "./filters.ts";
import { opsQuery } from "./db.ts";
import { loadItemLabels } from "./itemLabels.ts";
import { buildItem, companyLabels } from "./model.ts";
import type { EventRow, OpsItem } from "./model.ts";
import { findStep, itemsSql } from "./steps.ts";
import type { StepDef } from "./steps.ts";
import type { OpsCompany } from "./types.ts";

export type ItemsResult = {
  step: StepDef;
  day: string;
  total: number;
  limit: number;
  items: OpsItem[];
};

type ItemEventRow = EventRow & { total: number };

/** La lista detrás de un número de la tabla diaria: mismas filas que cuenta ese número (hasta 200). */
export async function listItems(query: ItemsQuery, companies: readonly OpsCompany[]): Promise<ItemsResult> {
  const step = findStep(query.source, query.step);
  if (!step) throw new Error(`Unknown ops step: ${query.source}/${query.step}`);
  const rows = await opsQuery<ItemEventRow>(itemsSql(step), [query.day, query.day, step.shared ? null : query.company, ITEMS_LIMIT]);
  const labels = await loadItemLabels(
    query.source,
    rows.map((row) => row.item_id).filter((id): id is string => id != null),
  );
  const names = companyLabels(companies);
  return {
    step,
    day: query.day,
    total: rows[0]?.total ?? 0,
    limit: ITEMS_LIMIT,
    items: rows.map((row, index) => buildItem(query.source, step, row, index, labels, names, step.shared ? null : query.company)),
  };
}
