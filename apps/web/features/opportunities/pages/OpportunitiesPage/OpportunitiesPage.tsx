import { AppShell } from "@/features/shell";
import { Pagination } from "@/components/ui/Pagination";
import { PageHeader } from "@/components/ui/PageHeader";
import { getTenantOpportunityContext, listContractsForTenant } from "@/server/services/contracts";
import { OpportunityList } from "../../components/OpportunityList";
import { OpportunityToolbar } from "../../components/OpportunityToolbar";
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
