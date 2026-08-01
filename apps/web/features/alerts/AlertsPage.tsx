import Link from "next/link";
import { AppShell } from "@/features/shell/components/AppShell";
import { AppIcon } from "@/components/ui/AppIcon";
import { formatDeadline, formatShortDateTime } from "@/lib/format/date";
import { query } from "@/server/db/client";
import { AlertPreferencesForm } from "./components/AlertPreferencesForm";
import styles from "./AlertsPage.module.css";

type AlertRow = {
  id: number;
  reason: string;
  status: string;
  created_at: string | Date;
  score: number | null;
  verdict: string | null;
  id_contrato: number | null;
  codigo: string | null;
  descripcion: string | null;
  entidad_nombre: string | null;
  fec_fin_cotizacion: string | Date | null;
};

function reasonLabel(reason: string) {
  if (reason === "verdict_change") return "Afinidad actualizada";
  return "Nueva oportunidad relevante";
}

export async function AlertsPage({ tenantId }: { tenantId: string }) {
  const [preference, alerts] = await Promise.all([
    query(
      `SELECT enabled,min_verdict,max_alerts_per_day
       FROM notification_preferences
       WHERE tenant_id=$1 AND channel='in_app' AND business_line_id IS NULL
       ORDER BY updated_at DESC LIMIT 1`,
      [tenantId],
    ),
    query<AlertRow>(
      `SELECT n.id,n.reason,n.status,n.created_at,m.score,m.verdict,c.id_contrato,
         c.codigo,c.descripcion,c.entidad_nombre,c.fec_fin_cotizacion
       FROM notifications n
       JOIN tenant_members tm ON tm.user_id=n.user_id AND tm.tenant_id=$1
       LEFT JOIN matches m ON m.id=n.match_id
       LEFT JOIN seace_contracts c ON c.id_contrato=m.id_contrato
       WHERE n.channel='in_app'
       ORDER BY n.created_at DESC,n.id DESC
       LIMIT 40`,
      [tenantId],
    ),
  ]);
  const pref = preference.rows[0] ?? { enabled: true, min_verdict: "verde", max_alerts_per_day: 5 };

  return (
    <AppShell title="Alertas">
      <header className={styles.header}>
        <div>
          <h1>Alertas</h1>
          <p>Oportunidades que superan el nivel de afinidad de tu radar.</p>
        </div>
        <span>{alerts.rows.length} recientes</span>
      </header>

      <AlertPreferencesForm preference={pref as any} />

      <section className={styles.history} aria-labelledby="alert-history-title">
        <div className={styles.sectionHeader}>
          <div><h2 id="alert-history-title">Actividad reciente</h2><p>Ordenada desde la coincidencia más nueva.</p></div>
        </div>
        {alerts.rows.length ? (
          <div className={styles.list}>
            {alerts.rows.map((alert) => {
              const content = (
                <>
                  <span className={styles.icon}><AppIcon name="bell" /></span>
                  <span className={styles.copy}>
                    <small>{reasonLabel(alert.reason)} · {formatShortDateTime(alert.created_at)}</small>
                    <strong>{alert.descripcion ?? "Oportunidad analizada"}</strong>
                    <span>{alert.codigo ?? "Sin código"}{alert.entidad_nombre ? ` · ${alert.entidad_nombre}` : ""}</span>
                  </span>
                  <span className={styles.match}>
                    {alert.score != null ? <strong>{Math.round(Number(alert.score))}% afinidad</strong> : <strong>Relevante</strong>}
                    <small>{formatDeadline(alert.fec_fin_cotizacion)}</small>
                  </span>
                  {alert.id_contrato ? <AppIcon name="arrow" /> : null}
                </>
              );
              return alert.id_contrato ? <Link href={`/oportunidad/${alert.id_contrato}`} key={alert.id}>{content}</Link> : <div key={alert.id}>{content}</div>;
            })}
          </div>
        ) : (
          <div className={styles.empty}>
            <span><AppIcon name="bell" /></span>
            <div><h3>Todavía no hay alertas</h3><p>Las nuevas oportunidades aparecerán aquí cuando superen la afinidad configurada.</p></div>
          </div>
        )}
      </section>
    </AppShell>
  );
}
