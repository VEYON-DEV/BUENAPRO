import Link from "next/link";
import {
  ArrowRight,
  BarChart3,
  Bookmark,
  CalendarClock,
  ClipboardCheck,
} from "lucide-react";
import { EmptyState } from "@/components/ui/EmptyState";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { AppShell } from "@/features/shell";
import { getDashboard } from "@/server/services/dashboard";
import { DailyMetric } from "../../components/DailyMetric";
import { MarketActivityChart } from "../../components/MarketActivityChart";
import { SavedOpportunities } from "../../components/SavedOpportunities";
import { SupplierRanking } from "../../components/SupplierRanking";
import styles from "./DashboardPage.module.css";

function greeting() {
  const hour = Number(
    new Intl.DateTimeFormat("es-PE", {
      hour: "2-digit",
      hour12: false,
      timeZone: "America/Lima",
    }).format(new Date()),
  );
  if (hour < 12) return "Buenos días";
  if (hour < 19) return "Buenas tardes";
  return "Buenas noches";
}

export async function DashboardPage({ tenantId }: { tenantId: string }) {
  const data = await getDashboard(tenantId);
  const saved: any = data.action_summary;
  const tracking: any = data.tracking_summary;
  const market: any = data.market;
  const closingSoon = Number(saved.closing_48h ?? 0);

  return (
    <AppShell title="Inicio">
      <div className={styles.page}>
        <header className={styles.header}>
          <div>
            <h1>{greeting()}</h1>
            <p>{data.profile?.razon_social ?? "Tu radar de contratación"}</p>
          </div>
          <Link href="/feed?saved=true&deadline=all">
            Ver guardadas <ArrowRight aria-hidden="true" size={16} />
          </Link>
        </header>

        {!data.profile ? (
          <EmptyState
            title="Completa tu perfil para activar el radar"
            action={{ label: "Ir a perfil", href: "/perfil" }}
          >
            BuenaPro necesita tus líneas de negocio para ordenar oportunidades y comparables.
          </EmptyState>
        ) : (
          <>
            <section className={styles.alert} aria-label="Cierres próximos de oportunidades guardadas">
              <span className={styles.alertIcon}><CalendarClock aria-hidden="true" size={25} /></span>
              <div className={styles.alertCopy}>
                <strong>{closingSoon} {closingSoon === 1 ? "guardada cierra" : "guardadas cierran"} pronto</strong>
                <span>Oportunidades de tu lista que vencen en las próximas 48 horas.</span>
              </div>
              <div className={styles.alertCounts}>
                <span>{Number(saved.closing_24h ?? 0)} en 24 h</span>
                <span>{Math.max(0, closingSoon - Number(saved.closing_24h ?? 0))} en 48 h</span>
              </div>
              <Link href="/feed?saved=true&deadline=week">
                Revisar guardadas <ArrowRight aria-hidden="true" size={17} />
              </Link>
            </section>

            <section className={styles.metrics} aria-label="Resumen diario">
              <DailyMetric
                detail="Lista personal de oportunidades"
                href="/feed?saved=true&deadline=all"
                icon={<Bookmark aria-hidden="true" size={21} />}
                label="Guardadas vigentes"
                value={Number(saved.active ?? 0).toLocaleString("es-PE")}
              />
              <DailyMetric
                detail={`${Number(tracking.with_draft ?? 0)} con borrador`}
                href="/postulaciones?estado=en_preparacion"
                icon={<ClipboardCheck aria-hidden="true" size={21} />}
                label="En preparación"
                tone="blue"
                value={Number(tracking.preparing ?? 0).toLocaleString("es-PE")}
              />
              <DailyMetric
                detail={`${Number(market.suppliers ?? 0).toLocaleString("es-PE")} proveedores adjudicados`}
                href="/mercado"
                icon={<BarChart3 aria-hidden="true" size={21} />}
                label="Mercado analizado"
                tone="green"
                value={Number(market.total ?? 0).toLocaleString("es-PE")}
              />
            </section>

            <div className={styles.workspace}>
              <SavedOpportunities rows={data.actions as any[]} />
              <aside className={styles.insights} aria-label="Contexto de mercado">
                <GlassSurface tone="work" padding="large" className={styles.marketPanel}>
                  <div className={styles.panelHead}>
                    <div><h2>Actividad del mercado</h2><p>Resultados en tus líneas de negocio.</p></div>
                    <Link href="/mercado" aria-label="Abrir análisis de mercado"><ArrowRight size={17} /></Link>
                  </div>
                  <div className={styles.marketSummary}>
                    <span><strong>{Number(market.adjudicados ?? 0).toLocaleString("es-PE")}</strong><small>Adjudicados</small></span>
                    <span><strong>{market.desiertos_pct ?? 0}%</strong><small>Tasa desierta</small></span>
                  </div>
                  <MarketActivityChart rows={data.market_trend as any[]} />
                </GlassSurface>

                <GlassSurface tone="work" padding="large">
                  <div className={styles.panelHead}>
                    <div><h2>Competidores frecuentes</h2><p>Empresas con más adjudicaciones.</p></div>
                    <Link href="/mercado?view=suppliers" aria-label="Ver empresas"><ArrowRight size={17} /></Link>
                  </div>
                  <SupplierRanking rows={data.frequent_suppliers as any[]} />
                </GlassSurface>
              </aside>
            </div>
          </>
        )}
      </div>
    </AppShell>
  );
}
