import { formatMoney } from "@/lib/format/money";
import { formatDateTime } from "@/lib/format/date";
import { Table } from "@/components/ui/Table";
import styles from "./MarketContracts.module.css";

function pageHref(filters: any, page: number) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    const names: Record<string,string> = { minPrice: "min_price", maxPrice: "max_price" };
    if (value && key !== "page") params.set(names[key] ?? key, String(value));
  });
  params.set("view", "contracts"); params.set("page", String(page));
  return `/mercado?${params}`;
}

export function MarketContracts({ rows, total, filters }: { rows: any[]; total: number; filters: any }) {
  const pages = Math.max(1, Math.ceil(total / 20));
  return (
    <section className={styles.section}>
      <header><div><h2>Contratos históricos</h2><p>{total.toLocaleString("es-PE")} resultados para el filtro actual</p></div></header>
      {rows.length ? <Table embedded className={styles.table}><thead><tr><th>Contrato</th><th>Objeto</th><th>Entidad y región</th><th>Resultado</th><th>Ganador</th><th data-numeric>Precio</th><th>Publicación</th></tr></thead><tbody>
        {rows.map((row) => <tr key={row.id_contrato}>
          <td><strong>{row.codigo_completo}</strong><small>Segmento {row.cubso_segmento}</small></td>
          <td><span className={styles.description}>{row.descripcion}</span><small>{row.cubso_name}</small></td>
          <td><span>{row.entity_name}</span><small>{row.department ?? "Sin región"}</small></td>
          <td><span className={row.estado_resultado === "ADJUDICADO" ? styles.awarded : styles.deserted}>{row.estado_resultado === "ADJUDICADO" ? "Adjudicado" : row.estado_resultado === "DESIERTO" ? "Desierto" : "Sin resultado"}</span></td>
          <td>{row.supplier_ruc ? <a href={`/mercado/empresas/${row.supplier_ruc}`}>{row.supplier_name}</a> : <span className={styles.muted}>-</span>}</td>
          <td data-numeric><strong>{formatMoney(row.precio_total, "-")}</strong></td>
          <td><span>{formatDateTime(row.fec_publica)}</span>{row.source_document_url ? <a className={styles.document} href={row.source_document_url} target="_blank" rel="noreferrer">Ver documento</a> : null}</td>
        </tr>)}
      </tbody></Table> : <p className={styles.empty}>No encontramos contratos con estos filtros.</p>}
      {pages > 1 ? <nav className={styles.pagination} aria-label="Paginación"><a aria-disabled={filters.page <= 1} href={filters.page > 1 ? pageHref(filters, filters.page - 1) : undefined}>Anterior</a><span>Página {filters.page} de {pages}</span><a aria-disabled={filters.page >= pages} href={filters.page < pages ? pageHref(filters, filters.page + 1) : undefined}>Siguiente</a></nav> : null}
    </section>
  );
}
