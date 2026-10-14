import { AppShell } from "@/features/shell";
import { Pagination } from "@/components/ui/Pagination";
import { PageHeader } from "@/components/ui/PageHeader";
import { EmptyState } from "@/components/ui/EmptyState";
import { getTenantOpportunityContext, listContractsForTenant } from "@/server/services/contracts";
import { listProd4OpportunitiesForTenant, parseProd4ListParams } from "@/server/services/prod4Opportunities";
import { listProd2OpportunitiesForTenant, parseProd2ListParams } from "@/server/services/prod2Opportunities";
import { OpportunityList } from "../../components/OpportunityList";
import { OpportunitySourceTabs } from "../../components/OpportunitySourceTabs";
import { OpportunityToolbar } from "../../components/OpportunityToolbar";
import { Prod4OpportunityList } from "../../components/Prod4OpportunityList";
import type { Prod4OpportunitySummary } from "../../components/Prod4OpportunityList";
import { Prod4OpportunityToolbar } from "../../components/Prod4OpportunityToolbar";
import styles from "./OpportunitiesPage.module.css";

function normalizeDeadline(params: URLSearchParams) {
  const deadline = params.get("deadline") ?? "open";
  params.set("deadline", deadline);

  if (deadline === "all") {
    params.delete("open_only");
    params.delete("closing_before");
    return;
  }
  if (deadline === "closed") {
    params.set("open_only", "false");
    params.delete("closing_before");
    return;
  }

  params.set("open_only", "true");
  if (deadline === "24h") {
    params.set("closing_before", new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString());
    return;
  }
  if (deadline === "week") {
    params.set("closing_before", new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString());
    return;
  }
  params.delete("closing_before");
}

function pageHref(params: URLSearchParams, page: number) {
  const next = new URLSearchParams(params);
  next.delete("open_only");
  next.delete("closing_before");
  next.set("page", String(page));
  return `/feed?${next.toString()}`;
}

function resultLabel(deadline: string, savedOnly: boolean) {
  if (savedOnly) return "oportunidades guardadas";
  if (deadline === "all") return "contratos en tus rubros";
  if (deadline === "closed") return "contratos cerrados en tus rubros";
  if (deadline === "24h") return "contratos que cierran en 24 h";
  if (deadline === "week") return "contratos que cierran esta semana";
  return "contratos no cerrados en tus rubros";
}

export async function OpportunitiesPage({ tenantId, params }: { tenantId: string; params: URLSearchParams }) {
  if (params.get("source") === "prod2") {
    try {
      const result = await listProd2OpportunitiesForTenant(tenantId, parseProd2ListParams(params));
      const totalPages = Math.max(Math.ceil(result.total / result.page_size), 1);
      return <AppShell title="Oportunidades">
        <PageHeader title="Oportunidades" meta={`${result.total} procedimientos consultados en SEACE`}
          description="Afinidad comercial y requisitos con evidencia. Confirma la modalidad y las fechas oficiales antes de participar." />
        <OpportunitySourceTabs source="prod2" />
        <Prod4OpportunityToolbar source="prod2" defaults={{ ...Object.fromEntries(params.entries()), ...(result.profile ? { profile_id: result.profile.id } : {}) }} />
        <Prod4OpportunityList source="prod2" profileId={result.profile?.id} rows={result.data as unknown as Prod4OpportunitySummary[]} />
        <div className={styles.pagination}><Pagination page={result.page} totalPages={totalPages}
          prevHref={result.page > 1 ? pageHref(params, result.page - 1) : undefined}
          nextHref={result.page < totalPages ? pageHref(params, result.page + 1) : undefined} /></div>
      </AppShell>;
    } catch {
      return <AppShell title="Oportunidades"><OpportunitySourceTabs source="prod2" />
        <EmptyState title="No se pudieron cargar las oportunidades" action={{ label: "Volver a intentar", href: "/feed?source=prod2" }}>
          Revisa el perfil seleccionado y vuelve a intentar la consulta.
        </EmptyState></AppShell>;
    }
  }
  if (params.get("source") === "prod4") {
    const [result, context] = await Promise.all([
      listProd4OpportunitiesForTenant(tenantId, parseProd4ListParams(params)),
      getTenantOpportunityContext(tenantId),
    ]);
    const totalPages = Math.max(Math.ceil(result.total / result.page_size), 1);
    const prod4PageHref = (page: number) => {
      const next = new URLSearchParams(params);
      next.set("source", "prod4");
      next.set("page", String(page));
      return `/feed?${next.toString()}`;
    };
    return (
      <AppShell title="Oportunidades">
        <PageHeader
          title="Oportunidades"
          meta={`${result.total} concursos de tecnología en el radar`}
          description={context.lines.length
            ? `Concursos de bienes y servicios ordenados por afinidad preliminar con ${context.razon_social}. Revisa las bases y el cronograma oficial antes de participar.`
            : "Configura tus líneas de negocio para ver afinidad. Revisa las bases y el cronograma oficial antes de participar."}
        />
        <OpportunitySourceTabs source="prod4" />
        <Prod4OpportunityToolbar defaults={Object.fromEntries(params.entries())} />
        <Prod4OpportunityList rows={result.data as unknown as Prod4OpportunitySummary[]} />
        <div className={styles.pagination}>
          <Pagination
            page={result.page}
            totalPages={totalPages}
            prevHref={result.page > 1 ? prod4PageHref(result.page - 1) : undefined}
            nextHref={result.page < totalPages ? prod4PageHref(result.page + 1) : undefined}
          />
        </div>
      </AppShell>
    );
  }
  if (!params.has("has_extraction")) params.set("has_extraction", "true");
  if (!params.has("estado")) params.set("estado", "2");
  normalizeDeadline(params);
  const context = await getTenantOpportunityContext(tenantId);
  const hasBusinessLines = context.lines.length > 0;
  const contracts = await listContractsForTenant(tenantId, params);
  const rows = contracts.data;
  const meta = contracts.meta;
  const totalPages = Math.max(Math.ceil((meta.total ?? meta.count) / meta.page_size), 1);
  const deadline = params.get("deadline") ?? "open";
  const savedOnly = params.get("saved") === "true";

  return (
    <AppShell title="Oportunidades">
      <PageHeader
        title="Oportunidades"
        meta={`${meta.total ?? meta.count} ${resultLabel(deadline, savedOnly)}`}
        description={hasBusinessLines
          ? `Todas las coincidencias para ${context.razon_social}, de mayor a menor afinidad.`
          : "Configura tus líneas de negocio para priorizar las mejores coincidencias."}
      />
      <OpportunitySourceTabs source="prod6" />
      <OpportunityToolbar defaults={Object.fromEntries(params.entries())} />
      <OpportunityList rows={rows} />
      <div className={styles.pagination}>
        <Pagination
          page={meta.page}
          totalPages={totalPages}
          prevHref={meta.page > 1 ? pageHref(params, meta.page - 1) : undefined}
          nextHref={meta.page < totalPages ? pageHref(params, meta.page + 1) : undefined}
        />
      </div>
    </AppShell>
  );
}
