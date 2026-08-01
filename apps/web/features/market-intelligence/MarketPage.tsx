import { AppShell } from "@/features/shell/components/AppShell";
import { Tabs } from "@/components/ui/Tabs";
import { getMarketIntelligence, parseMarketFilters } from "@/server/services/market";
import { MarketFilterBar } from "./components/MarketFilterBar";
import { MarketOverview } from "./components/MarketOverview";
import { MarketContracts } from "./components/MarketContracts";
import { MarketSuppliers } from "./components/MarketSuppliers";
import styles from "./MarketPage.module.css";

function tabHref(filters: any, view: string) {
  const params = new URLSearchParams();
  params.set("scope", filters.scope); params.set("view", view);
  if (filters.q) params.set("q", filters.q);
  if (filters.segment) params.set("segment", filters.segment);
  if (filters.result) params.set("result", filters.result);
  if (filters.department) params.set("department", filters.department);
  if (filters.entity) params.set("entity", filters.entity);
  if (filters.year) params.set("year", filters.year);
  if (filters.minPrice) params.set("min_price", filters.minPrice);
  if (filters.maxPrice) params.set("max_price", filters.maxPrice);
  return `/mercado?${params}`;
}

export async function MarketPage({ tenantId, params }: { tenantId: string; params: URLSearchParams }) {
  const filters = parseMarketFilters(params);
  const data = await getMarketIntelligence(tenantId, filters);
  const scopeLabel = filters.scope === "profile" ? "Tus líneas de negocio" : "Todos los segmentos disponibles";
  return (
    <AppShell title="Mercado">
      <header className={styles.header}>
        <div><h1>Inteligencia de mercado</h1><p>{scopeLabel} · precios, demanda y competidores</p></div>
        <span>{Number(data.summary.total ?? 0).toLocaleString("es-PE")} procesos analizados</span>
      </header>
      <MarketFilterBar filters={filters} options={data.options} />
      <Tabs items={[
        { key: "overview", label: "Resumen", href: tabHref(filters, "overview"), active: filters.view === "overview" },
        { key: "contracts", label: "Contratos", href: tabHref(filters, "contracts"), active: filters.view === "contracts" },
        { key: "suppliers", label: "Empresas", href: tabHref(filters, "suppliers"), active: filters.view === "suppliers" },
      ]} />
      <div className={styles.content}>
        {filters.view === "overview" ? <MarketOverview data={data} /> : null}
        {filters.view === "contracts" ? <MarketContracts rows={data.contracts} total={data.total_contracts} filters={filters} /> : null}
        {filters.view === "suppliers" ? <MarketSuppliers rows={data.suppliers} /> : null}
      </div>
    </AppShell>
  );
}
