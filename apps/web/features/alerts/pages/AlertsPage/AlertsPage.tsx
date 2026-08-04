"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Bell, RefreshCw, Settings2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { AppShell } from "@/features/shell";
import { getAlertInbox } from "../../api/inbox";
import { AlertInbox } from "../../components/AlertInbox";
import type { AlertItem } from "../../model/types";
import styles from "./AlertsPage.module.css";

export function AlertsPage({ tenantId: _tenantId }: { tenantId: string }) {
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setAlerts(await getAlertInbox());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo cargar la bandeja");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const unread = alerts.filter((alert) => !alert.read_at).length;
  const recommended = alerts.filter((alert) => {
    const verdict = alert.verdict ?? alert.payload?.verdict ?? alert.payload?.veredicto;
    return verdict === "verde" || verdict === "ambar";
  }).length;

  return (
    <AppShell title="Alertas">
      <PageHeader
        title="Alertas"
        description="Oportunidades que el radar evaluó y considera relevantes para tu empresa."
        meta={`${unread} sin leer`}
        actions={
          <nav className={styles.viewNav} aria-label="Alertas y configuración">
            <Link className={styles.active} href="/alertas" aria-current="page"><Bell size={17} /> Alertas</Link>
            <Link href="/configuracion"><Settings2 size={17} /> Configuración</Link>
          </nav>
        }
      />

      <div className={styles.summary} aria-label="Resumen de alertas">
        <div><strong>{alerts.length}</strong><span>Evaluaciones recibidas</span></div>
        <div><strong>{recommended}</strong><span>Recomendadas por el radar</span></div>
        <div><strong>{unread}</strong><span>Pendientes de revisar</span></div>
        <p>El radar conserva el resultado completo y muestra aquí solo lo necesario para decidir.</p>
      </div>

      {loading ? (
        <section className={styles.loading} aria-live="polite"><RefreshCw aria-hidden="true" size={20} /> Cargando evaluaciones…</section>
      ) : error ? (
        <section className={styles.error} role="alert">
          <div><strong>No pudimos cargar las alertas</strong><span>{error}</span></div>
          <Button onClick={() => void load()} size="compact" type="button" variant="secondary"><RefreshCw size={15} /> Reintentar</Button>
        </section>
      ) : <AlertInbox alerts={alerts} />}
    </AppShell>
  );
}
