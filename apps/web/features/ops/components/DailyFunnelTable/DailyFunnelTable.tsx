import type { CSSProperties } from "react";
import { Table } from "@/components/ui/Table";
import { opsHref } from "@/server/services/ops/filters";
import type { DailyTable } from "@/server/services/ops/model";
import { formatCount, formatDayLabel, ratio } from "../../model/format";
import styles from "./DailyFunnelTable.module.css";

/**
 * Una fila por día de Lima (el más nuevo primero) y una columna por paso del embudo. Cada número enlaza a la
 * lista de elementos que cuenta. La barra bajo el número es proporcional al máximo de su columna en el rango.
 */
export function DailyFunnelTable({
  table,
  today,
  company,
  range,
  caption,
}: {
  table: DailyTable;
  today: string;
  company: string | null;
  range: number;
  caption: string;
}) {
  const groupStarts = new Set<string>();
  let index = 0;
  for (const group of table.groups) {
    groupStarts.add(table.columns[index].key);
    index += group.span;
  }
  return (
    <Table className={styles.table}>
      <caption className={styles.caption}>{caption}</caption>
      <thead>
        <tr>
          <th className={styles.dayHead} rowSpan={2} scope="col">Día</th>
          {table.groups.map((group) => (
            <th className={styles.group} colSpan={group.span} key={group.label} scope="colgroup">
              {group.label}
              {group.shared ? <span className={styles.shared}>compartido</span> : null}
            </th>
          ))}
        </tr>
        <tr>
          {table.columns.map((column) => (
            <th className={groupStarts.has(column.key) ? styles.groupStart : undefined} data-numeric key={column.key} scope="col" title={column.hint}>
              {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {table.rows.map((row) => (
          <tr className={row.day === today ? styles.today : undefined} key={row.day}>
            <th className={styles.day} scope="row">
              {formatDayLabel(row.day)}
              {row.day === today ? <span className={styles.todayTag}>hoy</span> : null}
            </th>
            {table.columns.map((column) => {
              const value = row.values[column.key];
              const className = [styles[column.tone], groupStarts.has(column.key) ? styles.groupStart : ""].filter(Boolean).join(" ");
              if (value === 0) {
                return (
                  <td className={`${className} ${styles.zero}`} data-numeric key={column.key}>
                    <span aria-hidden="true">·</span>
                    <span className={styles.srOnly}>0</span>
                  </td>
                );
              }
              return (
                <td className={className} data-numeric key={column.key}>
                  <a
                    aria-label={`${column.title}, ${formatDayLabel(row.day)}: ${value}. Ver lista`}
                    className={styles.link}
                    href={opsHref("/ops/items", { source: table.source, step: column.key, day: row.day, company, range: range === 14 ? null : range })}
                    style={{ "--ratio": ratio(value, table.max[column.key]) } as CSSProperties}
                  >
                    {formatCount(value)}
                  </a>
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <th className={styles.day} scope="row">Total</th>
          {table.columns.map((column) => (
            <td className={groupStarts.has(column.key) ? styles.groupStart : undefined} data-numeric key={column.key}>
              {table.totals[column.key] ? formatCount(table.totals[column.key]) : <span aria-hidden="true">·</span>}
            </td>
          ))}
        </tr>
      </tfoot>
    </Table>
  );
}
