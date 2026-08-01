import { AppIcon } from "@/components/ui/AppIcon";
import type { MarketFilters } from "@/server/services/market";
import styles from "./MarketFilterBar.module.css";

type Option = { code?: string; id?: number; name: string; count: number };

function paramsWith(filters: MarketFilters, changes: Record<string, string>) {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.segment) params.set("segment", filters.segment);
  if (filters.result) params.set("result", filters.result);
  if (filters.department) params.set("department", filters.department);
  if (filters.entity) params.set("entity", filters.entity);
  if (filters.year) params.set("year", filters.year);
  if (filters.minPrice) params.set("min_price", filters.minPrice);
  if (filters.maxPrice) params.set("max_price", filters.maxPrice);
  params.set("scope", filters.scope);
  params.set("view", filters.view);
  Object.entries(changes).forEach(([key, value]) => value ? params.set(key, value) : params.delete(key));
  return `/mercado?${params.toString()}`;
}

export function MarketFilterBar({ filters, options }: { filters: MarketFilters; options: any }) {
  const segments = (options.segments ?? []) as Option[];
  const departments = (options.departments ?? []) as Option[];
  const entities = (options.entities ?? []) as Option[];
  const years = (options.years ?? []) as Array<{ year: number; count: number }>;
  return (
    <section className={styles.bar} aria-label="Filtros de mercado">
      <div className={styles.scope} aria-label="Alcance">
        <a className={filters.scope === "profile" ? styles.active : ""} href={paramsWith(filters, { scope: "profile", page: "" })}>Mis mercados</a>
        <a className={filters.scope === "all" ? styles.active : ""} href={paramsWith(filters, { scope: "all", page: "" })}>Todo el mercado</a>
      </div>
      <form className={styles.form} action="/mercado">
        <input type="hidden" name="scope" value={filters.scope} />
        <input type="hidden" name="view" value={filters.view} />
        <label className={styles.search}>
          <span className={styles.srOnly}>Buscar</span>
          <AppIcon name="search" />
          <input autoComplete="off" name="q" defaultValue={filters.q} placeholder="Servicio, código, entidad o proveedor…" />
        </label>
        <label>
          <span>Segmento</span>
          <select name="segment" defaultValue={filters.segment}>
            <option value="">Todos</option>
            {segments.map((item) => <option value={item.code} key={item.code}>{item.code} · {item.name} ({item.count})</option>)}
          </select>
        </label>
        <label>
          <span>Resultado</span>
          <select name="result" defaultValue={filters.result}>
            <option value="">Todos</option><option value="ADJUDICADO">Adjudicados</option><option value="DESIERTO">Desiertos</option><option value="SIN_RESULTADO">Sin resultado</option>
          </select>
        </label>
        <label>
          <span>Departamento</span>
          <select name="department" defaultValue={filters.department}>
            <option value="">Todo el Perú</option>
            {departments.map((item) => <option value={item.name} key={item.name}>{item.name} ({item.count})</option>)}
          </select>
        </label>
        <label>
          <span>Entidad</span>
          <select name="entity" defaultValue={filters.entity}>
            <option value="">Todas</option>
            {entities.map((item) => <option value={item.id} key={item.id}>{item.name} ({item.count})</option>)}
          </select>
        </label>
        <label>
          <span>Año</span>
          <select name="year" defaultValue={filters.year}>
            <option value="">Todos</option>
            {years.filter((item) => item.year).map((item) => <option value={item.year} key={item.year}>{item.year} ({item.count})</option>)}
          </select>
        </label>
        <div className={styles.priceFields}>
          <label><span>Precio desde</span><input autoComplete="off" name="min_price" inputMode="decimal" defaultValue={filters.minPrice} placeholder="S/ 0…" /></label>
          <label><span>Hasta</span><input autoComplete="off" name="max_price" inputMode="decimal" defaultValue={filters.maxPrice} placeholder="Sin límite…" /></label>
        </div>
        <button type="submit"><AppIcon name="filter" />Aplicar filtros</button>
        <a className={styles.clear} href={`/mercado?scope=${filters.scope}&view=${filters.view}`}>Limpiar</a>
      </form>
    </section>
  );
}
