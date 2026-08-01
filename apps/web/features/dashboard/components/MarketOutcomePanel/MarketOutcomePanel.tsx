import { formatMoney } from "@/lib/format/money";
import styles from "./MarketOutcomePanel.module.css";

type Market = {
  total: number;
  adjudicados?: number | string;
  desiertos?: number | string;
  desiertos_pct?: number;
  precio_median?: number | string | null;
};

export function MarketOutcomePanel({ market }: { market: Market }) {
  const awarded = Number(market.adjudicados ?? 0);
  const deserted = Number(market.desiertos ?? 0);
  const outcomeTotal = awarded + deserted;
  const awardedWidth = outcomeTotal ? (awarded / outcomeTotal) * 100 : 0;
  const desertedWidth = outcomeTotal ? 100 - awardedWidth : 0;

  return (
    <section className={styles.panel} aria-labelledby="market-outcome-title">
      <div>
        <h2 id="market-outcome-title">Resultado histórico</h2>
        <p>Composición de tus rubros</p>
      </div>
      <div className={styles.total}>
        <strong>{market.total.toLocaleString("es-PE")}</strong>
        <span>procesos analizados</span>
      </div>
      <div className={styles.composition} aria-label={`${awarded} adjudicados y ${deserted} desiertos`}>
        <span className={styles.awarded} style={{ width: `${awardedWidth}%` }} />
        <span className={styles.deserted} style={{ width: `${desertedWidth}%` }} />
      </div>
      <dl className={styles.breakdown}>
        <div><dt>Adjudicados</dt><dd>{awarded.toLocaleString("es-PE")}</dd></div>
        <div><dt>Desiertos</dt><dd>{market.desiertos_pct ?? 0}%</dd></div>
      </dl>
      <div className={styles.price}>
        <span>Precio mediano adjudicado</span>
        <strong>{formatMoney(market.precio_median, "Sin precios")}</strong>
      </div>
    </section>
  );
}
