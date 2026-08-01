import Link from "next/link";
import { AppIcon } from "@/components/ui/AppIcon";
import { formatMoney } from "@/lib/format/money";
import styles from "./MarketSuppliers.module.css";

export function MarketSuppliers({ rows }: { rows: any[] }) {
  return (
    <section className={styles.section}>
      <header><h2>Empresas adjudicadas</h2><p>Competidores con más resultados ganados dentro del filtro.</p></header>
      {rows.length ? <div className={styles.list}>
        {rows.map((row, index) => <Link href={`/mercado/empresas/${row.ruc}`} key={row.ruc}>
          <span className={styles.position}>{index + 1}</span>
          <span className={styles.identity}><strong>{row.name}</strong><small>RUC {row.ruc}</small></span>
          <span><small>Adjudicaciones</small><strong>{Number(row.awards).toLocaleString("es-PE")}</strong></span>
          <span><small>Monto acumulado</small><strong>{formatMoney(row.amount, "-")}</strong></span>
          <span><small>Precio mediano</small><strong>{formatMoney(row.median_price, "-")}</strong></span>
          <span><small>Cobertura</small><strong>{row.entities} entidades · {row.regions} regiones</strong></span>
          <AppIcon name="arrow" />
        </Link>)}
      </div> : <p className={styles.empty}>No hay empresas adjudicadas con estos filtros.</p>}
    </section>
  );
}
