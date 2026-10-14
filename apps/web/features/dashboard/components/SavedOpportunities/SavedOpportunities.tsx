import Link from "next/link";
import { ArrowRight, Bookmark } from "lucide-react";
import { formatDeadline, formatShortDateTime } from "@/lib/format/date";
import styles from "./SavedOpportunities.module.css";

type SavedOpportunity = {
  id_contrato: number;
  codigo: string;
  descripcion: string;
  entidad_nombre: string;
  departamento?: string | null;
  provincia?: string | null;
  fec_fin_cotizacion?: string | Date | null;
};

export function SavedOpportunities({ rows }: { rows: SavedOpportunity[] }) {
  return (
    <section className={styles.surface}>
      <header>
        <div>
          <h2>Oportunidades guardadas</h2>
          <p>Tu lista para revisar y convertir en postulación.</p>
        </div>
        <span>{rows.length} visibles</span>
      </header>

      {rows.length ? (
        <div className={styles.list}>
          <div className={styles.columns} aria-hidden="true">
            <span>Código</span><span>Objeto</span><span>Entidad</span><span>Cierre</span><span />
          </div>
          {rows.slice(0, 5).map((row) => (
            <Link href={`/oportunidad/${row.id_contrato}`} key={row.id_contrato}>
              <Bookmark aria-hidden="true" className={styles.bookmark} size={18} />
              <span className={styles.code}><strong>{row.codigo}</strong><small>Guardada</small></span>
              <span className={styles.object}>{row.descripcion}</span>
              <span className={styles.entity}><strong>{row.entidad_nombre}</strong><small>{[row.departamento, row.provincia].filter(Boolean).join(", ")}</small></span>
              <span className={styles.deadline}><strong>{formatDeadline(row.fec_fin_cotizacion)}</strong><small>{formatShortDateTime(row.fec_fin_cotizacion)}</small></span>
              <ArrowRight aria-hidden="true" className={styles.open} size={17} />
            </Link>
          ))}
        </div>
      ) : (
        <div className={styles.empty}>
          <span className={styles.emptyIcon}><Bookmark aria-hidden="true" size={22} /></span>
          <strong>Aún no guardaste oportunidades</strong>
          <p>Guarda contratos menores para revisarlos aquí y en el Cronograma. Los procedimientos guardados del Buscador SEACE se consultan en su pestaña del Cronograma.</p>
          <Link className={styles.emptyAction} href="/feed?source=prod6">Explorar contratos menores <ArrowRight aria-hidden="true" size={15} /></Link>
        </div>
      )}

      {rows.length ? <Link className={styles.footerLink} href="/feed?saved=true&deadline=all">
        Ver todas las guardadas <ArrowRight aria-hidden="true" size={15} />
      </Link> : null}
    </section>
  );
}
