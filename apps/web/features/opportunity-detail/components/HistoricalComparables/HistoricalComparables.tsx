import { AppIcon } from "@/components/ui/AppIcon";
import { formatMoney } from "@/lib/format/money";
import { formatShortDateTime } from "@/lib/format/date";
import styles from "./HistoricalComparables.module.css";

export function HistoricalComparables({ history }: { history: any }) {
  const metrics = history?.metrics;
  const rows = history?.comparables ?? [];
  const comparableRow = (row: any) => (
    <article key={row.id_contrato}>
      <div className={styles.code}>
        <strong translate="no">{row.codigo_completo}</strong>
        <span className={styles[row.estado_resultado.toLowerCase()]}>
          {row.estado_resultado === "SIN_RESULTADO" ? "Sin resultado" : row.estado_resultado.toLowerCase()}
        </span>
      </div>
      <p>{row.descripcion}</p>
      <div className={styles.meta}>
        <span>{row.entity_name}</span>
        <span>{formatShortDateTime(row.fec_publica)}</span>
        {row.precio_total != null ? <strong>{formatMoney(row.precio_total)}</strong> : <span>Sin precio</span>}
      </div>
      <div className={styles.reasons}>{row.reasons.slice(0, 3).map((reason: string) => <span key={reason}>{reason}</span>)}</div>
      {row.source_document_url ? <a href={row.source_document_url} target="_blank" rel="noreferrer">Ver TDR antiguo <AppIcon name="arrow" /></a> : null}
    </article>
  );
  return (
    <section className={styles.section} id="comparables">
      <div className={styles.heading}>
        <div>
          <h2>Histórico comparable</h2>
          <p>
            Referencias SEACE que ayudan a decidir si conviene y cuánto ofertar.
          </p>
        </div>
        {metrics?.total_count ? (
          <strong>{metrics.total_count} similares</strong>
        ) : null}
      </div>
      {!rows.length ? (
        <div className={styles.empty}>
          <AppIcon name="track" />
          <div>
            <strong>Aún no hay comparables sólidos</strong>
            <span>
              El histórico aparecerá cuando coincidan CUBSO o señales de tu
              perfil.
            </span>
          </div>
        </div>
      ) : (
        <>
          <dl className={styles.summary}>
            <div className={styles.price}>
              <dt>Rango frecuente</dt>
              <dd>
                {formatMoney(metrics.rango_frecuente.min)} –{" "}
                {formatMoney(metrics.rango_frecuente.max)}
              </dd>
              <small>25%–75% de adjudicados</small>
            </div>
            <div>
              <dt>Precio mediano</dt>
              <dd>{formatMoney(metrics.precio_median)}</dd>
              <small>Solo adjudicados</small>
            </div>
            <div>
              <dt>Adjudicados</dt>
              <dd>{metrics.adjudicados_count}</dd>
              <small>Con resultado conocido</small>
            </div>
            <div>
              <dt>Desiertos</dt>
              <dd>{metrics.desiertos_pct}%</dd>
              <small>{metrics.desiertos_count} procesos</small>
            </div>
          </dl>

          {metrics.proveedores_frecuentes?.length ? (
            <div className={styles.suppliers}>
              <span>Proveedores frecuentes</span>
              {metrics.proveedores_frecuentes
                .slice(0, 4)
                .map((supplier: any) => (
                  <strong key={supplier.name}>
                    {supplier.name} <small>{supplier.count}</small>
                  </strong>
                ))}
            </div>
          ) : null}

          <div className={styles.list}>
            {rows.slice(0, 3).map(comparableRow)}
            {rows.length > 3 ? (
              <details className={styles.moreComparables}>
                <summary>Ver {Math.min(rows.length, 8) - 3} comparables más</summary>
                <div>{rows.slice(3, 8).map(comparableRow)}</div>
              </details>
            ) : null}
          </div>
        </>
      )}
    </section>
  );
}
