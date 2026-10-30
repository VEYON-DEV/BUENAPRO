import { Table } from "@/components/ui/Table";
import type { CostsTable as CostsData } from "@/server/services/ops/model";
import { COST_BUCKETS } from "@/server/services/ops/steps";
import { formatCostCell, formatDayLabel, formatUsd, ratio } from "../../model/format";
import styles from "./CostsTable.module.css";

const STAGE_LABEL = { jev: "JEV", lectura: "Lectura", evaluacion: "Evaluación", embeddings: "Embeddings" } as const;
const SOURCE_LABEL = { menores: "Menores", mayores: "Mayores" } as const;

/** Costo diario en USD por origen y etapa. «*» marca días con eventos cuyo costo no quedó registrado. */
export function CostsTable({ costs, today }: { costs: CostsData; today: string }) {
  const groups = (["menores", "mayores"] as const).map((source) => ({
    source,
    buckets: costs.buckets.filter((bucket) => COST_BUCKETS[bucket].source === source),
  }));
  const peak = costs.rows.reduce((max, row) => Math.max(max, row.totalUsd), 0);
  return (
    <Table className={styles.table}>
      <caption className={styles.caption}>Costo diario en dólares por origen y etapa</caption>
      <thead>
        <tr>
          <th className={styles.dayHead} rowSpan={2} scope="col">Día</th>
          {groups.map((group) => (
            <th className={styles.group} colSpan={group.buckets.length} key={group.source} scope="colgroup">{SOURCE_LABEL[group.source]}</th>
          ))}
          <th className={styles.group} rowSpan={2} scope="col">Total</th>
        </tr>
        <tr>
          {groups.flatMap((group) =>
            group.buckets.map((bucket, position) => (
              <th className={position === 0 ? styles.groupStart : undefined} data-numeric key={bucket} scope="col">
                {STAGE_LABEL[COST_BUCKETS[bucket].stage]}
              </th>
            )),
          )}
        </tr>
      </thead>
      <tbody>
        {costs.rows.map((row) => (
          <tr className={row.day === today ? styles.today : undefined} key={row.day}>
            <th className={styles.day} scope="row">{formatDayLabel(row.day)}</th>
            {groups.flatMap((group) =>
              group.buckets.map((bucket, position) => {
                const cell = row.cells[bucket];
                return (
                  <td
                    className={[position === 0 ? styles.groupStart : "", cell.n === 0 ? styles.zero : ""].filter(Boolean).join(" ")}
                    data-numeric
                    key={bucket}
                    title={cell.n ? `${cell.priced} de ${cell.n} eventos con costo registrado` : undefined}
                  >
                    {formatCostCell(cell.usd, cell.n, cell.priced)}
                  </td>
                );
              }),
            )}
            <td className={styles.total} data-numeric>
              <span className={styles.totalValue}>{row.totalUsd > 0 ? formatUsd(row.totalUsd) : "—"}</span>
              <span aria-hidden="true" className={styles.bar} style={{ width: `${ratio(row.totalUsd, peak) * 100}%` }} />
            </td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <th className={styles.day} scope="row">Total</th>
          {groups.flatMap((group) =>
            group.buckets.map((bucket, position) => (
              <td className={position === 0 ? styles.groupStart : undefined} data-numeric key={bucket}>
                {costs.totals[bucket] > 0 ? formatUsd(costs.totals[bucket]) : "—"}
              </td>
            )),
          )}
          <td data-numeric>{formatUsd(costs.totalUsd)}</td>
        </tr>
      </tfoot>
    </Table>
  );
}
