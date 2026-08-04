import styles from "./MarketTrendChart.module.css";

type TrendPoint = {
  month: string | Date;
  adjudicados: number | string;
  desiertos: number | string;
};

function monthLabel(value: string | Date) {
  return new Intl.DateTimeFormat("es-PE", { month: "short", timeZone: "UTC" })
    .format(new Date(value))
    .replace(".", "");
}

export function MarketTrendChart({ data }: { data: TrendPoint[] }) {
  const values = data.flatMap((point) => [Number(point.adjudicados), Number(point.desiertos)]);
  const maximum = Math.max(...values, 1);

  return (
    <section className={styles.panel} aria-labelledby="market-trend-title">
      <div className={styles.header}>
        <div>
          <h2 id="market-trend-title">Actividad del mercado</h2>
          <p>Resultados mensuales en tus rubros</p>
        </div>
        <div className={styles.legend} aria-label="Leyenda">
          <span><i className={styles.awarded} />Adjudicados</span>
          <span><i className={styles.deserted} />Desiertos</span>
        </div>
      </div>
      <div className={styles.scroll}>
        <div className={styles.chart} role="img" aria-label="Adjudicados y desiertos por mes">
          <div className={styles.guides} aria-hidden="true"><i /><i /><i /></div>
          {data.map((point) => {
            const adjudicados = Number(point.adjudicados);
            const desiertos = Number(point.desiertos);
            return (
              <div className={styles.month} key={String(point.month)}>
                <div className={styles.bars}>
                  <span
                    className={styles.awardedBar}
                    style={{ height: adjudicados ? `${Math.max(3, (adjudicados / maximum) * 100)}%` : 0 }}
                    title={`${adjudicados} adjudicados`}
                  />
                  <span
                    className={styles.desertedBar}
                    style={{ height: desiertos ? `${Math.max(3, (desiertos / maximum) * 100)}%` : 0 }}
                    title={`${desiertos} desiertos`}
                  />
                </div>
                <span className={styles.monthLabel}>{monthLabel(point.month)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
