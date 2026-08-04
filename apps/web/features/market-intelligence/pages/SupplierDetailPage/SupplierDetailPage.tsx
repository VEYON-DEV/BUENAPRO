import Link from "next/link";
import { AppShell } from "@/features/shell";
import { AppIcon } from "@/components/ui/AppIcon";
import { Table } from "@/components/ui/Table";
import { formatMoney } from "@/lib/format/money";
import { formatDateTime } from "@/lib/format/date";
import styles from "./SupplierDetailPage.module.css";

function CompactRanking({ title, rows }: { title: string; rows: any[] }) {
  return <section className={styles.ranking}><h2>{title}</h2>{rows.map((row) => <div key={row.code ?? row.name}><span>{row.name}</span><strong>{row.count}</strong></div>)}</section>;
}

export function SupplierDetailPage({ data }: { data: any }) {
  const supplier = data.supplier;
  return <AppShell title="Mercado">
    <header className={styles.header}>
      <Link href="/mercado?view=suppliers"><AppIcon name="arrow" />Volver a empresas</Link>
      <h1>{supplier.razon_social}</h1><p>RUC {supplier.ruc} · actividad adjudicada en SEACE</p>
    </header>
    <section className={styles.metrics}>
      <div><span>Adjudicaciones</span><strong>{supplier.total_awards}</strong></div>
      <div><span>Monto acumulado</span><strong>{formatMoney(supplier.total_awarded_amount)}</strong></div>
      <div><span>Precio mediano</span><strong>{formatMoney(data.median_price)}</strong></div>
      <div><span>Última aparición</span><strong>{formatDateTime(supplier.last_seen_at)}</strong></div>
    </section>
    <div className={styles.rankGrid}><CompactRanking title="Entidades donde gana" rows={data.entities} /><CompactRanking title="Regiones" rows={data.regions} /><CompactRanking title="Servicios adjudicados" rows={data.segments} /></div>
    <section className={styles.contracts}><header><h2>Licitaciones ganadas</h2><p>{data.contracts.length} resultados registrados</p></header><Table embedded className={styles.table}><thead><tr><th>Contrato</th><th>Servicio</th><th>Entidad</th><th>Región</th><th data-numeric>Precio</th><th>Fecha</th><th></th></tr></thead><tbody>
      {data.contracts.map((row: any) => <tr key={row.id_contrato}><td><strong>{row.codigo_completo}</strong></td><td><span>{row.descripcion}</span><small>{row.cubso_name}</small></td><td>{row.entity_name}</td><td>{row.department ?? "-"}</td><td data-numeric><strong>{formatMoney(row.precio_total,"-")}</strong></td><td>{formatDateTime(row.fec_publica)}</td><td>{row.source_document_url ? <a href={row.source_document_url} target="_blank" rel="noreferrer">Documento</a> : null}</td></tr>)}
    </tbody></Table></section>
  </AppShell>;
}
