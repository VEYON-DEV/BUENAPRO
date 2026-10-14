import Link from "next/link";
import styles from "./SupplierRanking.module.css";

export function SupplierRanking({ rows }: { rows: Array<{ name: string; awards: number }> }) {
  const max = Math.max(1, ...rows.map((row) => Number(row.awards)));
  if (!rows.length) return <p className={styles.empty}>El ranking aparecera al cargar adjudicados.</p>;
  return (
    <div className={styles.list}>
      {rows.map((row, index) => (
        <Link href={`/mercado?view=suppliers&q=${encodeURIComponent(row.name)}`} key={`${row.name}-${index}`}>
          <small>{index + 1}</small><strong title={row.name}>{row.name}</strong>
          <i aria-hidden="true"><span style={{ width: `${Number(row.awards) / max * 100}%` }} /></i>
          <b>{row.awards}</b>
        </Link>
      ))}
    </div>
  );
}
