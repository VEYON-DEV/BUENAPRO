import Link from "next/link";
import { AppShell } from "@/features/shell/components/AppShell";
import { AppIcon } from "@/components/ui/AppIcon";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { formatDeadline, formatShortDateTime } from "@/lib/format/date";
import { getDashboard } from "@/server/services/dashboard";
import { MarketOutcomePanel } from "./components/MarketOutcomePanel";
import { MarketTrendChart } from "./components/MarketTrendChart";
import { MetricTile } from "./components/MetricTile";
import { SupplierAwardChart } from "./components/SupplierAwardChart";
import styles from "./DashboardPage.module.css";

export async function DashboardPage({ tenantId }: { tenantId: string }) {
  const data = await getDashboard(tenantId);
  const actionSummary: any = data.action_summary;
  const trackingSummary: any = data.tracking_summary;

  return (
    <AppShell title="Inicio">
      <header className={styles.header}>
        <div>
          <h1>Panel de decisión</h1>
          <p>
            {data.profile?.razon_social
              ? `${data.profile.razon_social} · oportunidades y mercado`
              : "Oportunidades, postulaciones y mercado"}
          </p>
        </div>
        <Link href="/feed">
          <Button>Explorar oportunidades</Button>
        </Link>
      </header>

      {!data.profile ? (
        <EmptyState
          title="Completa tu perfil para activar el radar"
          action={{ label: "Ir a perfil", href: "/perfil" }}
        >
          BuenaPro necesita tus líneas de negocio para ordenar oportunidades y
          comparables.
        </EmptyState>
      ) : (
        <>
          <section className={styles.metrics} aria-label="Indicadores principales">
            <MetricTile
              label="Oportunidades vigentes"
              value={Number(actionSummary.active ?? 0).toLocaleString("es-PE")}
              caption="En tus líneas de negocio"
              href="/feed"
            />
            <MetricTile
              accent
              label="Cierran en 24 h"
              value={Number(actionSummary.closing_24h ?? 0).toLocaleString("es-PE")}
              caption={`${Number(actionSummary.closing_week ?? 0)} cierran esta semana`}
              href="/feed?deadline=24h"
            />
            <MetricTile
              label="En preparación"
              value={Number(trackingSummary.preparing ?? 0).toLocaleString("es-PE")}
              caption={`${Number(trackingSummary.with_draft ?? 0)} con borrador`}
              href="/postulaciones?estado=en_preparacion"
            />
            <MetricTile
              label="Histórico del rubro"
              value={Number(data.market.total ?? 0).toLocaleString("es-PE")}
              caption={`${Number(data.market.suppliers ?? 0).toLocaleString("es-PE")} proveedores identificados`}
              href="/mercado"
            />
          </section>

          <div className={styles.marketGrid}>
            <MarketTrendChart data={data.market_trend as any[]} />
            <MarketOutcomePanel market={data.market as any} />
          </div>

          <div className={styles.workGrid}>
            <section className={styles.panel}>
              <div className={styles.sectionHeader}>
                <div>
                  <h2>Cierres próximos</h2>
                  <p>Prioridad por fecha de cierre y afinidad</p>
                </div>
                <Link href="/feed?deadline=week">Ver semana</Link>
              </div>
              {data.actions.length ? (
                <div className={styles.radarList}>
                  {data.actions.slice(0, 6).map((row: any) => (
                    <Link
                      href={`/oportunidad/${row.id_contrato}`}
                      className={styles.radarRow}
                      key={row.id_contrato}
                    >
                      <span className={styles.urgency} aria-hidden="true">
                        <AppIcon name="clock" />
                      </span>
                      <span className={styles.contractCopy}>
                        <strong translate="no">{row.codigo}</strong>
                        <span>{row.descripcion}</span>
                        <small>{row.entidad_nombre}</small>
                      </span>
                      <span className={styles.deadline}>
                        <strong>{formatDeadline(row.fec_fin_cotizacion)}</strong>
                        <small>{formatShortDateTime(row.fec_fin_cotizacion)}</small>
                      </span>
                      <AppIcon name="arrow" />
                    </Link>
                  ))}
                </div>
              ) : (
                <p className={styles.empty}>No hay cierres próximos en tus rubros.</p>
              )}
            </section>

            <section className={styles.panel}>
              <div className={styles.sectionHeader}>
                <div>
                  <h2>Empresas adjudicadas</h2>
                  <p>Proveedores con más resultados ganados</p>
                </div>
                <Link href="/mercado?view=suppliers">Ver mercado</Link>
              </div>
              {data.frequent_suppliers.length ? (
                <SupplierAwardChart suppliers={data.frequent_suppliers as any[]} />
              ) : (
                <p className={styles.empty}>El ranking aparecerá al cargar adjudicados.</p>
              )}
            </section>
          </div>

          <section className={styles.panel}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>Postulaciones en curso</h2>
                <p>Trabajo preparado por tu equipo</p>
              </div>
              <Link href="/postulaciones">Ver postulaciones</Link>
            </div>
            {data.tracking.length ? (
              <div className={styles.preparationList}>
                {data.tracking.slice(0, 5).map((row: any) => (
                  <Link
                    href={row.has_draft ? `/postulaciones/${row.match_id}` : `/oportunidad/${row.id_contrato}`}
                    key={row.match_id}
                  >
                    <span>
                      <strong translate="no">{row.codigo}</strong>
                      <small>{row.descripcion}</small>
                    </span>
                    <span className={styles.state}>
                      {row.has_draft ? "Continuar borrador" : "Preparar oferta"}
                    </span>
                    <AppIcon name="arrow" />
                  </Link>
                ))}
              </div>
            ) : (
              <p className={styles.empty}>Todavía no hay postulaciones en preparación.</p>
            )}
          </section>
        </>
      )}
    </AppShell>
  );
}
