"use client";

import { useState } from "react";
import { AppIcon } from "@/components/ui/AppIcon";
import { Button } from "@/components/ui/Button";
import {
  FilterActions,
  FilterField,
  FilterForm,
  FilterPanel,
  FilterSearch,
  FilterTab,
  FilterTabs,
} from "@/components/ui/FilterPanel";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
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
  const [mobileOpen, setMobileOpen] = useState(false);
  const segments = (options.segments ?? []) as Option[];
  const departments = (options.departments ?? []) as Option[];
  const entities = (options.entities ?? []) as Option[];
  const years = (options.years ?? []) as Array<{ year: number; count: number }>;
  const hasAdvancedFilters = Boolean(filters.segment || filters.entity || filters.year || filters.minPrice || filters.maxPrice);
  return (
    <FilterPanel className={styles.bar} aria-label="Filtros de mercado">
      <FilterTabs aria-label="Alcance">
        <FilterTab active={filters.scope === "profile"} href={paramsWith(filters, { scope: "profile", page: "" })}>Mis mercados</FilterTab>
        <FilterTab active={filters.scope === "all"} href={paramsWith(filters, { scope: "all", page: "" })}>Todo el mercado</FilterTab>
      </FilterTabs>
      <button
        className={styles.mobileToggle}
        type="button"
        aria-expanded={mobileOpen}
        aria-controls="market-filters"
        onClick={() => setMobileOpen((current) => !current)}
      >
        <AppIcon name="filter" />
        {mobileOpen ? "Ocultar filtros" : "Filtrar mercado"}
      </button>
      <FilterForm className={[styles.form, mobileOpen ? styles.mobileOpen : ""].filter(Boolean).join(" ")} action="/mercado" id="market-filters">
        <input type="hidden" name="scope" value={filters.scope} />
        <input type="hidden" name="view" value={filters.view} />
        <FilterField className={styles.search} htmlFor="market-search" label="Buscar">
          <FilterSearch id="market-search" autoComplete="off" name="q" defaultValue={filters.q} placeholder="Servicio, codigo, entidad o proveedor…" />
        </FilterField>
        <FilterField htmlFor="market-result" label="Resultado">
          <Select id="market-result" name="result" defaultValue={filters.result}>
            <option value="">Todos</option><option value="ADJUDICADO">Adjudicados</option><option value="DESIERTO">Desiertos</option><option value="SIN_RESULTADO">Sin resultado</option>
          </Select>
        </FilterField>
        <FilterField htmlFor="market-department" label="Departamento">
          <Select id="market-department" name="department" defaultValue={filters.department}>
            <option value="">Todo el Perú</option>
            {departments.map((item) => <option value={item.name} key={item.name}>{item.name} ({item.count})</option>)}
          </Select>
        </FilterField>
        <FilterActions><Button type="submit"><AppIcon name="filter" />Aplicar filtros</Button></FilterActions>
        <a className={styles.clear} href={`/mercado?scope=${filters.scope}&view=${filters.view}`}>Limpiar</a>
        <details className={styles.advanced} open={hasAdvancedFilters || undefined}>
          <summary>
            <span><AppIcon name="filter" /> Filtros avanzados</span>
            {hasAdvancedFilters ? <b>Activos</b> : null}
          </summary>
          <div className={styles.advancedGrid}>
            <FilterField htmlFor="market-segment" label="Segmento">
              <Select id="market-segment" name="segment" defaultValue={filters.segment}>
                <option value="">Todos</option>
                {segments.map((item) => <option value={item.code} key={item.code}>{item.code} · {item.name} ({item.count})</option>)}
              </Select>
            </FilterField>
            <FilterField htmlFor="market-entity" label="Entidad">
              <Select id="market-entity" name="entity" defaultValue={filters.entity}>
                <option value="">Todas</option>
                {entities.map((item) => <option value={item.id} key={item.id}>{item.name} ({item.count})</option>)}
              </Select>
            </FilterField>
            <FilterField htmlFor="market-year" label="Año">
              <Select id="market-year" name="year" defaultValue={filters.year}>
                <option value="">Todos</option>
                {years.filter((item) => item.year).map((item) => <option value={item.year} key={item.year}>{item.year} ({item.count})</option>)}
              </Select>
            </FilterField>
            <FilterField label="Precio desde"><Input autoComplete="off" name="min_price" inputMode="decimal" defaultValue={filters.minPrice} placeholder="S/ 0…" /></FilterField>
            <FilterField label="Precio hasta"><Input autoComplete="off" name="max_price" inputMode="decimal" defaultValue={filters.maxPrice} placeholder="Sin límite…" /></FilterField>
          </div>
        </details>
      </FilterForm>
    </FilterPanel>
  );
}
