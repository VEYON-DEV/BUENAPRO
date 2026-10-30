import { AppShell } from "@/features/shell";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { listOpsCompanies } from "@/server/services/ops/companies";
import { getCosts, getManualQuota } from "@/server/services/ops/costs";
import { getDailyFunnel, getPendingJevNow } from "@/server/services/ops/daily";
import { getFailures } from "@/server/services/ops/failures";
import { addDays, opsHref, parseOpsFilters } from "@/server/services/ops/filters";
import { OPS_SOURCES } from "@/server/services/ops/types";
import type { OpsCompany } from "@/server/services/ops/types";
import { CostsTable } from "../../components/CostsTable";
import { DailyFunnelTable } from "../../components/DailyFunnelTable";
import { FailureGroups } from "../../components/FailureGroups";
import { OpsFilterBar } from "../../components/OpsFilterBar";
import { OpsSection } from "../../components/OpsSection";
import { OpsSummary } from "../../components/OpsSummary";
import type { OpsSummaryItem } from "../../components/OpsSummary";
import { QuotaTable } from "../../components/QuotaTable";
import { formatCount, formatDayLabel, formatUsd } from "../../model/format";
import styles from "./OpsPage.module.css";

type Loaded<T> = { ok: true; data: T } | { ok: false };

/** Cada sección carga por separado: si una consulta falla, el resto del panel sigue siendo útil. */
async function load<T>(task: () => Promise<T>): Promise<Loaded<T>> {
  try {
    return { ok: true, data: await task() };
  } catch (error) {
    console.error("ops section failed", error instanceof Error ? error.message : error);
    return { ok: false };
  }
}

function SectionError({ what }: { what: string }) {
  return (
    <EmptyState title={`No se pudo cargar ${what}`}>
      La consulta falló o excedió el tiempo límite. Recarga la página; el detalle quedó en el registro del servidor.
    </EmptyState>
  );
}

export async function OpsPage({ params }: { params: URLSearchParams }) {
  const filters = parseOpsFilters(params);
  const companiesResult = await load(listOpsCompanies);
  const companies: OpsCompany[] = companiesResult.ok ? companiesResult.data : [];

  const [menores, mayores, failures, costs, quota, pending] = await Promise.all([
    load(() => getDailyFunnel("menores", filters)),
    load(() => getDailyFunnel("mayores", filters)),
    load(() => getFailures(filters, companies)),
    load(() => getCosts(filters)),
    load(() => getManualQuota(filters, companies)),
    load(() => getPendingJevNow(filters.company)),
  ]);

  const focus = companies.find((company) => company.id === filters.company);
  const summary: OpsSummaryItem[] = [];
  if (pending.ok) {
    summary.push({
      label: "JEV pendientes ahora",
      value: formatCount(pending.data),
      detail: "menores, por empresa y contrato",
      tone: "neutral",
      icon: "pending",
    });
  }
  if (failures.ok) {
    const groups = OPS_SOURCES.flatMap((source) => failures.data[source]);
    const total = groups.reduce((sum, group) => sum + group.total, 0);
    summary.push({
      label: "Fallas del día",
      value: formatCount(total),
      detail: `${formatCount(groups.length)} grupo${groups.length === 1 ? "" : "s"} · ${formatDayLabel(filters.day)}`,
      tone: total > 0 ? "bad" : "good",
      href: "#fallas",
      icon: "failures",
    });
  }
  if (costs.ok) {
    const row = costs.data.rows.find((entry) => entry.day === filters.today);
    const minors = row ? row.cells.menores_jev.usd + row.cells.menores_lectura.usd + row.cells.menores_evaluacion.usd : 0;
    const majors = (row?.totalUsd ?? 0) - minors;
    summary.push({
      label: "Costo de hoy",
      value: formatUsd(row?.totalUsd ?? 0),
      detail: `menores ${formatUsd(minors)} · mayores ${formatUsd(majors)}`,
      tone: "neutral",
      href: "#costos",
      icon: "cost",
    });
  }
  if (quota.ok) {
    const row = quota.data.rows.find((entry) => entry.day === filters.today);
    const used = quota.data.columns.map((column) => ({ label: column.label, used: row?.used[column.tenantId] ?? 0 }));
    summary.push({
      label: "Calificaciones manuales hoy",
      value: formatCount(used.reduce((sum, entry) => sum + entry.used, 0)),
      detail: used.length ? used.map((entry) => `${entry.label} ${entry.used}/${quota.data.limit}`).join(" · ") : "sin empresas en el filtro",
      tone: "neutral",
      href: "#costos",
      icon: "manual",
    });
  }

  const dayHref = (day: string) => `${opsHref("/ops", { range: filters.range === 14 ? null : filters.range, company: filters.company, day: day === filters.today ? null : day })}#fallas`;

  return (
    <AppShell title="Operación">
      <div className={styles.page}>
        <PageHeader
          eyebrow="Interno · solo administradores"
          meta={`Últimos ${filters.range} días · ${formatDayLabel(filters.from)} al ${formatDayLabel(filters.to)}${focus ? ` · ${focus.label}` : ""}`}
          title="Operación"
          description="El pipeline completo de licitaciones menores (PROD6) y mayores (PROD2): qué entró, qué se leyó y evaluó, qué falló y cuánto costó."
        />
        <OpsFilterBar companies={companies} filters={filters} />
        {summary.length ? <OpsSummary items={summary} /> : null}

        <OpsSection
          description="Una fila por día de Lima. Cada número abre la lista de licitaciones que cuenta; desde ahí se llega a su traza."
          id="menores"
          title="Menores por día"
        >
          {menores.ok ? (
            <DailyFunnelTable caption="Embudo diario de licitaciones menores (PROD6)" company={filters.company} range={filters.range} table={menores.data} today={filters.today} />
          ) : (
            <SectionError what="el embudo de menores" />
          )}
        </OpsSection>

        <OpsSection
          description="Búsquedas por palabra clave, candidatas nuevas, JEV de afinidad, lectura y evaluación de convocatorias (PROD2)."
          id="mayores"
          title="Mayores por día"
        >
          {mayores.ok ? (
            <DailyFunnelTable caption="Embudo diario de licitaciones mayores (PROD2)" company={filters.company} range={filters.range} table={mayores.data} today={filters.today} />
          ) : (
            <SectionError what="el embudo de mayores" />
          )}
        </OpsSection>

        <OpsSection
          aside={
            <nav aria-label="Día de las fallas" className={styles.dayNav}>
              <a href={dayHref(addDays(filters.day, -1))}>← Día anterior</a>
              <strong>{formatDayLabel(filters.day)}</strong>
              {filters.day < filters.today ? <a href={dayHref(addDays(filters.day, 1))}>Día siguiente →</a> : null}
              {filters.day !== filters.today ? <a href={dayHref(filters.today)}>Hoy</a> : null}
            </nav>
          }
          description="Agrupadas por paso y motivo. Abre un grupo para ver hasta 50 elementos y saltar a su traza."
          id="fallas"
          title="Fallas"
        >
          {failures.ok ? (
            <div className={styles.failures}>
              {OPS_SOURCES.map((source) => (
                <FailureGroups company={filters.company} day={filters.day} groups={failures.data[source]} key={source} source={source} />
              ))}
            </div>
          ) : (
            <SectionError what="las fallas" />
          )}
        </OpsSection>

        <OpsSection
          description="Dólares por etapa. El costo de las evaluaciones de menores sale de la última evaluación guardada; «*» marca días con eventos sin costo registrado."
          id="costos"
          title="Costos por día"
        >
          <div className={styles.costs}>
            {costs.ok ? <CostsTable costs={costs.data} today={filters.today} /> : <SectionError what="los costos" />}
            <div className={styles.quota}>
              <h3 className={styles.quotaTitle}>Cupo de calificaciones manuales</h3>
              {quota.ok ? <QuotaTable quota={quota.data} today={filters.today} /> : <SectionError what="el cupo" />}
            </div>
          </div>
        </OpsSection>
      </div>
    </AppShell>
  );
}
