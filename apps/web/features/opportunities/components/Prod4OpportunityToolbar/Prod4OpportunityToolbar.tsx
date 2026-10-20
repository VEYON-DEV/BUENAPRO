"use client";

import { useState } from "react";
import { FileCheck2, Search, SlidersHorizontal } from "lucide-react";
import { Input } from "@/components/ui/Input";
import { CommercialFilterFields, CommercialFilterHiddenInputs } from "../CommercialFilterFields";
import { COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";
import { commercialDefaultsKey, commercialSelectionCount } from "../../model/commercialFilters";
import { scoreFilterHref } from "../../model/analysisPresentation";
import styles from "./Prod4OpportunityToolbar.module.css";

function filterHref(defaults: Record<string, string>, object: string, source: "prod2" | "prod4") {
  const next = new URLSearchParams(defaults);
  next.set("source", source);
  next.delete("page");
  if (object) next.set("object", object);
  else next.delete("object");
  return `/feed?${next.toString()}`;
}

export function Prod4OpportunityToolbar({ defaults, source = "prod4" }: { defaults: Record<string, string>; source?: "prod2" | "prod4" }) {
  const object = defaults.object ?? "";
  const count = commercialSelectionCount(defaults);
  const [filtersOpen, setFiltersOpen] = useState(count > 0);
  return (
    <section className={styles.toolbar}>
      <form action="/feed" className={styles.searchForm}>
        {source === "prod2" ? <CommercialFilterHiddenInputs defaults={defaults} /> : null}
        <input name="source" type="hidden" value={source} />
        {source === "prod2" && defaults.profile_id ? <input name="profile_id" type="hidden" value={defaults.profile_id} /> : null}
        {object ? <input name="object" type="hidden" value={object} /> : null}
        {source === "prod2" && defaults.analysis === "done" ? <input name="analysis" type="hidden" value="done" /> : null}
        {source === "prod2" && defaults.with_score === "true" ? <input name="with_score" type="hidden" value="true" /> : null}
        <label className={styles.search}>
          <Search aria-hidden="true" size={18} />
          <Input aria-label={source === "prod2" ? "Buscar procedimientos SEACE" : "Buscar concursos de tecnología"} autoComplete="off" defaultValue={defaults.q ?? ""} name="q" placeholder="Ej.: licencia de software…" type="search" />
        </label>
        <button className={styles.searchButton} type="submit">Buscar</button>
      </form>
      <div className={styles.secondaryFilters}>
        <div aria-label="Objeto de contratación" className={styles.objectChoices} role="group">
          {[["", "Todos"], ["good", "Bienes"], ["service", "Servicios"]].map(([value, label]) => (
            <a aria-current={object === value ? "page" : undefined} className={object === value ? styles.active : ""} href={filterHref(defaults, value, source)} key={value}>
              {label}
            </a>
          ))}
        </div>
        {source === "prod2" ? <a aria-current={defaults.with_score === "true" ? "page" : undefined}
          className={[styles.analysisFilter, defaults.with_score === "true" ? styles.active : ""].join(" ")}
          href={scoreFilterHref(defaults, source)}>
          <FileCheck2 aria-hidden="true" size={16} />Con puntaje
        </a> : null}
        {source === "prod2" ? <button aria-controls="prod2-commercial-filters" aria-expanded={filtersOpen} className={styles.analysisFilter}
          onClick={() => setFiltersOpen(previous => !previous)} type="button">
          <SlidersHorizontal aria-hidden="true" size={16} />Filtros{count ? ` (${count})` : ""}
        </button> : null}
      </div>
      {source === "prod2" && filtersOpen ? <form action="/feed" className={styles.filtersPanel} id="prod2-commercial-filters">
        {Object.entries(defaults).filter(([key]) => ![...COMMERCIAL_FILTER_KEYS, "page", "open_only", "closing_before"].includes(key)).map(([key, value]) =>
          <input key={key} name={key} type="hidden" value={value} />)}
        {!defaults.source ? <input name="source" type="hidden" value="prod2" /> : null}
        <CommercialFilterFields defaults={defaults} key={commercialDefaultsKey(defaults)} />
        <div className={styles.filterActions}><button className={styles.searchButton} type="submit">Aplicar filtros</button></div>
      </form> : null}
    </section>
  );
}
