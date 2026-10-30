import { Table } from "@/components/ui/Table";
import type { QuotaTable as QuotaData } from "@/server/services/ops/model";
import { formatDayLabel, ratio } from "../../model/format";
import styles from "./QuotaTable.module.css";

/** Calificaciones manuales usadas del tope diario de cada empresa (menores y mayores suman en el mismo tope). */
export function QuotaTable({ quota, today }: { quota: QuotaData; today: string }) {
  if (!quota.columns.length) {
    return <p className={styles.empty}>No hay empresas en el filtro para calcular el cupo.</p>;
  }
  return (
    <Table className={styles.table}>
      <caption className={styles.caption}>Calificaciones manuales usadas por empresa y día, de un tope de {quota.limit}</caption>
      <thead>
        <tr>
          <th className={styles.dayHead} scope="col">Día</th>
          {quota.columns.map((column) => (
            <th data-numeric key={column.tenantId} scope="col">{column.label}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {quota.rows.map((row) => (
          <tr className={row.day === today ? styles.today : undefined} key={row.day}>
            <th className={styles.day} scope="row">{formatDayLabel(row.day)}</th>
            {quota.columns.map((column) => {
              const used = row.used[column.tenantId] ?? 0;
              const level = used >= quota.limit ? styles.full : used >= quota.limit * 0.8 ? styles.high : "";
              return (
                <td className={[used === 0 ? styles.zero : "", level].filter(Boolean).join(" ")} data-numeric key={column.tenantId}>
                  <span>
                    {used}
                    <span className={styles.limit}> / {quota.limit}</span>
                    {used >= quota.limit ? <span className={styles.srOnly}> (tope alcanzado)</span> : null}
                  </span>
                  {used > 0 ? <span aria-hidden="true" className={styles.bar} style={{ width: `${ratio(used, quota.limit) * 100}%` }} /> : null}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
