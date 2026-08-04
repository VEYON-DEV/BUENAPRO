import Link from "next/link";
import { formatMoney } from "@/lib/format/money";
import { MarketTrendChart } from "../MarketTrendChart";
import styles from "./MarketOverview.module.css";

function Ranking({ title, subtitle, rows, kind }: { title: string; subtitle: string; rows: any[]; kind?: "supplier" }) {
  const maximum = Math.max(...rows.map((row) => Number(row.total ?? row.awards ?? 0)), 1);
  return (
    <section className={styles.ranking}>
      <header><h2>{title}</h2><p>{subtitle}</p></header>
      <ol>
        {rows.map((row, index) => {
          const value = Number(row.total ?? row.awards ?? 0);
          const content = <><span className={styles.rankPosition}>{index + 1}</span><span className={styles.rankName}>{row.name}</span><span className={styles.rankTrack}><i style={{ width: `${Math.max(4, value / maximum * 100)}%` }} /></span><strong>{value.toLocaleString("es-PE")}</strong></>;
          return <li key={row.ruc ?? row.id ?? row.name}>{kind === "supplier" ? <Link href={`/mercado/empresas/${row.ruc}`}>{content}</Link> : <div>{content}</div>}</li>;
        })}
      </ol>
    </section>
  );
}

export function MarketOverview({ data }: { data: any }) {
  const summary = data.summary;
  const total = Number(summary.total ?? 0);
  const deserted = Number(summary.desiertos ?? 0);
  return (
    <>
      <section className={styles.metrics} aria-label="Resumen del mercado filtrado">
        <div><span>Procesos</span><strong>{total.toLocaleString("es-PE")}</strong><small>{Number(summary.adjudicados ?? 0).toLocaleString("es-PE")} adjudicados</small></div>
        <div><span>Tasa desierta</span><strong>{total ? Math.round(deserted / total * 100) : 0}%</strong><small>{deserted.toLocaleString("es-PE")} procesos sin ganador</small></div>
        <div><span>Competidores</span><strong>{Number(summary.suppliers ?? 0).toLocaleString("es-PE")}</strong><small>Proveedores adjudicados</small></div>
        <div><span>Entidades</span><strong>{Number(summary.entities ?? 0).toLocaleString("es-PE")}</strong><small>Compradores identificados</small></div>
        <div className={styles.price}><span>Precio mediano</span><strong>{formatMoney(summary.median_price, "Sin precios")}</strong><small>Rango central {formatMoney(summary.price_q1, "-")} a {formatMoney(summary.price_q3, "-")}</small></div>
      </section>
      <div className={styles.trendGrid}>
        <MarketTrendChart data={data.trend} />
        <Ranking title="Demanda por región" subtitle="Procesos publicados" rows={data.regions} />
      </div>
      <div className={styles.rankGrid}>
        <Ranking title="Entidades compradoras" subtitle="Mayor actividad en el filtro" rows={data.entities} />
        <Ranking title="Empresas adjudicadas" subtitle="Ganadores más frecuentes" rows={data.suppliers.slice(0, 8)} kind="supplier" />
      </div>
    </>
  );
}
