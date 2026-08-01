import Link from "next/link";
import styles from "./MarketOutcomePanel.module.css";

type Market = {
  total: number;
  adjudicados?: number | string;
  desiertos?: number | string;
  desiertos_pct?: number;
  suppliers?: number | string;
  entities?: number | string;
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
      <div className={styles.coverage}>
        <div><span>Proveedores</span><strong>{Number(market.suppliers ?? 0).toLocaleString("es-PE")}</strong></div>
        <div><span>Entidades</span><strong>{Number(market.entities ?? 0).toLocaleString("es-PE")}</strong></div>
      </div>
      <Link className={styles.action} href="/mercado">Analizar mercado</Link>
    </section>
  );
}
