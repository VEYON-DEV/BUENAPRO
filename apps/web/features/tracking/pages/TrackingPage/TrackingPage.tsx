import Link from "next/link";
import { AppShell } from "@/features/shell";
import { EmptyState } from "@/components/ui/EmptyState";
import { Table } from "@/components/ui/Table";
import { PageHeader } from "@/components/ui/PageHeader";
import { formatDeadline, formatShortDateTime } from "@/lib/format/date";
import { formatMoney } from "@/lib/format/money";
import { trackingLabels } from "@/lib/constants/states";
import { query } from "@/server/db/client";
import styles from "./TrackingPage.module.css";

type TrackingRow = {
  id: number;
  id_contrato: number;
  user_state: keyof typeof trackingLabels;
  monto_ofertado: string | number | null;
  codigo: string;
  entidad_nombre: string;
  descripcion: string;
  fec_fin_cotizacion: string | Date | null;
  tasks_count: number;
  tasks_done_count: number;
  next_step: string | null;
  responsable_nombre: string | null;
  responsable_email: string | null;
  has_application: boolean;
};

function getDestination(row: TrackingRow) {
  return row.has_application && (row.user_state === "en_preparacion" || row.user_state === "postulada")
    ? `/postulaciones/${row.id}`
    : `/oportunidad/${row.id_contrato}`;
}

function getProgress(row: TrackingRow) {
  if (!row.tasks_count) return 0;
  return Math.round((row.tasks_done_count / row.tasks_count) * 100);
}

function getNextStep(row: TrackingRow) {
  if (row.next_step) return row.next_step;
  if (row.user_state === "interesada") return "Decidir si iniciar la postulación";
  if (row.user_state === "postulada") return "Esperar resultado del proceso";
  if (row.user_state === "ganada") return "Coordinar el inicio del servicio";
  return "Revisar el estado del proceso";
}

function isUrgent(value: string | Date | null) {
  if (!value) return false;
  const hours = (new Date(value).getTime() - Date.now()) / 36e5;
  return hours >= 0 && hours <= 48;
}

export async function TrackingPage({ tenantId }: { tenantId: string }) {
  const result = await query<TrackingRow>(
    `
    SELECT
      m.id,
      m.id_contrato,
      m.user_state,
      m.monto_ofertado,
      c.codigo,
      c.entidad_nombre,
      c.descripcion,
      c.fec_fin_cotizacion,
      count(mt.id)::int AS tasks_count,
      count(mt.id) FILTER (WHERE mt.status = 'done')::int AS tasks_done_count,
      (
        SELECT pending.title
        FROM match_tasks pending
        WHERE pending.match_id = m.id AND pending.status <> 'done'
        ORDER BY pending.created_at, pending.id
        LIMIT 1
      ) AS next_step,
      u.name AS responsable_nombre,
      u.email AS responsable_email,
      (ad.id IS NOT NULL) AS has_application
    FROM matches m
    JOIN company_profiles cp ON cp.id = m.profile_id
    JOIN seace_contracts c ON c.id_contrato = m.id_contrato
    LEFT JOIN match_tasks mt ON mt.match_id = m.id
    LEFT JOIN users u ON u.id = m.responsable_id
    LEFT JOIN application_drafts ad ON ad.match_id = m.id
    WHERE cp.tenant_id = $1
      AND m.user_state <> 'inbox'
    GROUP BY m.id, c.id_contrato, u.id, ad.id
    ORDER BY c.fec_fin_cotizacion ASC NULLS LAST, m.updated_at DESC
    LIMIT 100
    `,
    [tenantId],
  );
  const rows = result.rows;
  const preparingCount = rows.filter((row) => row.user_state === "en_preparacion").length;
  const urgentCount = rows.filter((row) => isUrgent(row.fec_fin_cotizacion)).length;

  return (
    <AppShell title="Postulaciones">
      <PageHeader
        title="Postulaciones"
        eyebrow="Cartera de procesos"
        description="Prioriza cierres, identifica bloqueos y continúa cada postulación desde un solo lugar."
        actions={<div className={styles.summary} aria-label="Resumen de seguimiento">
          <span><strong>{rows.length}</strong> en seguimiento</span>
          <span><strong>{preparingCount}</strong> en preparación</span>
          <span className={urgentCount ? styles.urgentSummary : undefined}><strong>{urgentCount}</strong> cierran pronto</span>
        </div>}
      />

      {!rows.length ? (
        <EmptyState title="Aún no hay oportunidades en seguimiento" action={{ label: "Ver oportunidades", href: "/feed" }}>
          Marca una oportunidad como interesada o comienza una postulación desde su detalle.
        </EmptyState>
      ) : (
        <section className={styles.portfolio} aria-labelledby="portfolio-title">
          <div className={styles.portfolioHeader}>
            <div>
              <h2 id="portfolio-title">Procesos activos</h2>
              <p>Ordenados por fecha de cierre. Selecciona un proceso para continuar.</p>
            </div>
            <span>{rows.length} {rows.length === 1 ? "proceso" : "procesos"}</span>
          </div>

          <Table embedded className={styles.table}>
              <thead>
                <tr>
                  <th>Proceso</th>
                  <th>Estado</th>
                  <th>Cierre</th>
                  <th>Avance</th>
                  <th>Monto</th>
                  <th>Responsable</th>
                  <th>Próximo paso</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const href = getDestination(row);
                  const progress = getProgress(row);
                  const urgent = isUrgent(row.fec_fin_cotizacion);
                  return (
                    <tr key={row.id}>
                      <td data-label="Proceso" className={styles.processCell}>
                        <Link className={styles.processLink} href={href} aria-label={`Abrir ${row.codigo}: ${row.descripcion}`}>
                          <span className={styles.code}>{row.codigo}</span>
                          <strong>{row.descripcion}</strong>
                          <span className={styles.entity}>{row.entidad_nombre}</span>
                        </Link>
                      </td>
                      <td data-label="Estado">
                        <Link className={styles.cellLink} href={href} tabIndex={-1} aria-hidden="true">
                          <span className={`${styles.state} ${styles[`state_${row.user_state}`] ?? ""}`}>
                            {trackingLabels[row.user_state]}
                          </span>
                        </Link>
                      </td>
                      <td data-label="Cierre">
                        <Link className={styles.cellLink} href={href} tabIndex={-1} aria-hidden="true">
                          <strong className={urgent ? styles.urgent : styles.deadline}>{formatDeadline(row.fec_fin_cotizacion)}</strong>
                          <span className={styles.secondary}>{formatShortDateTime(row.fec_fin_cotizacion)}</span>
                        </Link>
                      </td>
                      <td data-label="Avance">
                        <Link className={styles.cellLink} href={href} tabIndex={-1} aria-hidden="true">
                          <span className={styles.progressLabel}>{row.tasks_done_count} de {row.tasks_count}</span>
                          <span className={styles.progressTrack} aria-hidden="true"><span style={{ width: `${progress}%` }} /></span>
                        </Link>
                      </td>
                      <td data-label="Monto">
                        <Link className={`${styles.cellLink} ${styles.amount}`} href={href} tabIndex={-1} aria-hidden="true">
                          {formatMoney(row.monto_ofertado, "Por definir")}
                        </Link>
                      </td>
                      <td data-label="Responsable">
                        <Link className={styles.cellLink} href={href} tabIndex={-1} aria-hidden="true">
                          <span className={styles.responsible}>{row.responsable_nombre || row.responsable_email || "Sin asignar"}</span>
                        </Link>
                      </td>
                      <td data-label="Próximo paso">
                        <Link className={`${styles.cellLink} ${styles.nextStep}`} href={href} tabIndex={-1} aria-hidden="true">
                          <span>{getNextStep(row)}</span>
                          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5.5 5.5-5.5 5.5" /></svg>
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
          </Table>
        </section>
      )}
    </AppShell>
  );
}
