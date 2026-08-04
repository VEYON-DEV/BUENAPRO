import styles from "./MarketActivityChart.module.css";

const monthFormatter = new Intl.DateTimeFormat("es-PE", { month: "short", timeZone: "America/Lima" });

export function MarketActivityChart({ rows }: { rows: Array<{ month: string | Date; adjudicados: number; desiertos: number }> }) {
  const max = Math.max(1, ...rows.flatMap((row) => [Number(row.adjudicados), Number(row.desiertos)]));
  return (
    <div className={styles.chart} aria-label="Actividad historica mensual">
      <div className={styles.legend}><span><i className={styles.won} /> Adjudicados</span><span><i className={styles.deserted} /> Desiertos</span></div>
      <div className={styles.plot}>
        {rows.map((row) => (
          <div className={styles.month} key={String(row.month)}>
            <div className={styles.bars}>
              <span className={styles.wonBar} style={{ height: `${Math.max(3, Number(row.adjudicados) / max * 100)}%` }} title={`${row.adjudicados} adjudicados`} />
              <span className={styles.desertedBar} style={{ height: `${Math.max(3, Number(row.desiertos) / max * 100)}%` }} title={`${row.desiertos} desiertos`} />
            </div>
            <small>{monthFormatter.format(new Date(row.month)).replace(".", "")}</small>
          </div>
        ))}
      </div>
    </div>
  );
}
