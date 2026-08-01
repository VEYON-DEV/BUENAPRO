import Link from "next/link";
import { formatMoney } from "@/lib/format/money";
import styles from "./MarketPanel.module.css";

export function MarketPanel({
  history,
  total,
  contractId,
}: {
  history: any;
  total: number;
  contractId?: number;
}) {
  const metrics = history?.metrics;
  const low = Number(metrics?.rango_frecuente?.min);
  const high = Number(metrics?.rango_frecuente?.max);
  const hasRange = Number.isFinite(low) && Number.isFinite(high);
  const signal =
    total > 0 && hasRange
      ? total < low
        ? "Tu oferta está por debajo del rango frecuente. Revisa costos y alcance."
        : total > high
          ? "Tu oferta supera el rango frecuente. Sustenta el diferencial antes de enviar."
          : "Tu oferta está dentro del rango frecuente de adjudicación."
      : "Completa precios para contrastar tu oferta con el mercado.";
  return (
    <section aria-live="polite" className={styles.panel} id="mercado">
      <div className={styles.head}>
        <div>
          <h2>Precio y mercado</h2>
          <p>Referencia histórica; no reemplaza tu estructura de costos.</p>
        </div>
        {metrics?.total_count ? (
          <strong>{metrics.total_count} comparables</strong>
        ) : null}
      </div>
      {metrics?.adjudicados_count ? (
        <>
          <dl className={styles.metrics}>
            <div>
              <dt>Rango frecuente</dt>
              <dd>
                {formatMoney(low)} – {formatMoney(high)}
              </dd>
            </div>
            <div>
              <dt>Mediana</dt>
              <dd>{formatMoney(metrics.precio_median)}</dd>
            </div>
            <div>
              <dt>Desiertos</dt>
              <dd>{metrics.desiertos_pct}%</dd>
            </div>
          </dl>
          <p className={styles.signal}>{signal}</p>
          <details className={styles.sources}>
            <summary>Ver comparables usados</summary>
            {history.comparables.slice(0, 5).map((row: any) => (
              <div key={row.id_contrato}>
                <span translate="no">{row.codigo_completo}</span>
                <strong>
                  {row.precio_total
                    ? formatMoney(row.precio_total)
                    : row.estado_resultado.toLowerCase()}
                </strong>
              </div>
            ))}
          </details>
        </>
      ) : (
        <p className={styles.empty}>
          Todavía no hay adjudicados comparables con precio.
        </p>
      )}
      <div className={styles.consultations}>
        <div>
          <h3>Consultas a la entidad</h3>
          <p>
            Consulta el registro oficial desde SEACE. El envío se habilitará
            cuando el endpoint oficial esté confirmado.
          </p>
        </div>
        {contractId ? (
          <Link href={`/oportunidad/${contractId}#seace-workspace`}>
            Revisar consultas oficiales
          </Link>
        ) : null}
      </div>
    </section>
  );
}
