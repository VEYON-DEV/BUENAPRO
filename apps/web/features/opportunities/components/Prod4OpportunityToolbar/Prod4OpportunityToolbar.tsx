"use client";

import { FileCheck2, Search } from "lucide-react";
import { Input } from "@/components/ui/Input";
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
  return (
    <section className={styles.toolbar}>
      <form action="/feed" className={styles.searchForm}>
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
      </div>
    </section>
  );
}
