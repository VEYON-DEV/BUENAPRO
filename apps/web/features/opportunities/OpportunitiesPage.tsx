import { AppShell } from "@/features/shell/components/AppShell";
import { Badge } from "@/components/ui/Badge";
import { Pagination } from "@/components/ui/Pagination";
import { getTenantOpportunityContext, listContractsForTenant } from "@/server/services/contracts";
import { OpportunityList } from "./components/OpportunityList";
import { OpportunityToolbar } from "./components/OpportunityToolbar";
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
  const activeLines = context.lines.slice(0, 3);
  const totalPages = Math.max(Math.ceil((meta.total ?? meta.count) / meta.page_size), 1);
  const deadline = params.get("deadline") ?? "open";
  const savedOnly = params.get("saved") === "true";
  const hasPrioritizedRows = rows.some((row) => row.match_id);

  return (
    <AppShell title="Oportunidades">
      <header className={styles.header}>
        <div>
          <div className={styles.titleRow}>
            <h1>Oportunidades</h1>
            <span className={styles.count}>
              {meta.total ?? meta.count} {resultLabel(deadline, savedOnly)}
            </span>
          </div>
          <p>
            {hasBusinessLines
              ? `Todas las coincidencias para ${context.razon_social}, de mayor a menor afinidad.`
              : "Configura tus líneas de negocio para priorizar las mejores coincidencias."}
          </p>
          {activeLines.length ? (
            <div className={styles.lines}>
              {activeLines.map((line) => (
                <span key={line.nombre}>{line.nombre}</span>
              ))}
            </div>
          ) : null}
        </div>
        <span className={styles.mode}>
          <Badge tone={hasPrioritizedRows ? "sage" : "brand"}>{hasPrioritizedRows ? "Priorizado" : "Explorar"}</Badge>
        </span>
      </header>
      <OpportunityToolbar defaults={params} />
      <OpportunityList rows={rows} />
      <Pagination
        page={meta.page}
        totalPages={totalPages}
        prevHref={meta.page > 1 ? pageHref(params, meta.page - 1) : undefined}
        nextHref={meta.page < totalPages ? pageHref(params, meta.page + 1) : undefined}
      />
    </AppShell>
  );
}
