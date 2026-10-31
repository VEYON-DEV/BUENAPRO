import { FileCheck2, Search, Tag, X } from "lucide-react";
import { Input } from "@/components/ui/Input";
import { COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";
import { scoreFilterHref } from "../../model/analysisPresentation";
import { prod2ListingHref } from "../../model/prod2Listing";
import type { Prod2LineOption } from "../../model/prod2Listing";
import { Prod2LineFilter } from "../Prod2LineFilter";
import styles from "./Prod4OpportunityToolbar.module.css";

function filterHref(defaults: Record<string, string>, object: string, source: "prod2" | "prod4") {
  const next = new URLSearchParams(defaults);
  next.set("source", source);
  next.delete("page");
  if (object) next.set("object", object);
  else next.delete("object");
  return `/feed?${next.toString()}`;
}

// «No cerradas» ya no se ofrece: «Activas» es lo que se puede trabajar hoy. Un enlace viejo con deadline=open sigue
// funcionando y, mientras esté en esa vista, muestra su opción para que se pueda ver y dejar.
function deadlineChoices(deadline: string) {
  const choices = [["all", "Todas"], ["active", "Activas"], ["closed", "Cerradas"]];
  return deadline === "open" ? [...choices, ["open", "No cerradas"]] : choices;
}

export function Prod4OpportunityToolbar({ defaults: received, source = "prod4", lines = [] }: {
  defaults: Record<string, string>; source?: "prod2" | "prod4";
  /** Líneas activas del perfil (solo mayores); sin líneas no se ofrece el selector. */
  lines?: Prod2LineOption[];
}) {
  // Los enlaces antiguos con commercial_* se ignoran: este buscador ya no filtra por categoría.
  const defaults = Object.fromEntries(Object.entries(received).filter(([key]) => !(COMMERCIAL_FILTER_KEYS as readonly string[]).includes(key)));
  const object = defaults.object ?? "";
  const deadline = defaults.deadline ?? "all";
  const line = defaults.line ?? "";
  const keyword = defaults.kw ?? "";
  // Cambiar de línea conserva todo lo demás (búsqueda, tipo, cierre, puntaje, palabra y perfil) y vuelve a la página 1.
  const lineHidden = Object.fromEntries(Object.entries(defaults).filter(([key, value]) => value !== "" && !["line", "page", "source"].includes(key)));
  return (
    <section className={styles.toolbar}>
      <form action="/feed" className={styles.searchForm}>
        <input name="source" type="hidden" value={source} />
        {source === "prod2" && defaults.profile_id ? <input name="profile_id" type="hidden" value={defaults.profile_id} /> : null}
        {object ? <input name="object" type="hidden" value={object} /> : null}
        {source === "prod2" && defaults.analysis === "done" ? <input name="analysis" type="hidden" value="done" /> : null}
        {source === "prod2" && defaults.with_score === "true" ? <input name="with_score" type="hidden" value="true" /> : null}
        {source === "prod2" ? <input name="deadline" type="hidden" value={deadline} /> : null}
        {source === "prod2" && line ? <input name="line" type="hidden" value={line} /> : null}
        {source === "prod2" && keyword ? <input name="kw" type="hidden" value={keyword} /> : null}
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
        {source === "prod2" ? <div aria-label="Cierre" className={`${styles.objectChoices} ${styles.deadlineChoices}`} role="group">
          {deadlineChoices(deadline).map(([value, label]) => (
            <a aria-current={deadline === value ? "page" : undefined} className={deadline === value ? styles.active : ""} href={prod2ListingHref(defaults, { deadline: value })} key={value}>
              {label}
            </a>
          ))}
        </div> : null}
        {source === "prod2" ? <a aria-current={defaults.with_score === "true" ? "page" : undefined}
          className={[styles.analysisFilter, defaults.with_score === "true" ? styles.active : ""].join(" ")}
          href={scoreFilterHref(defaults, source)}>
          <FileCheck2 aria-hidden="true" size={16} />Con puntaje
        </a> : null}
        {source === "prod2" && lines.length ? <Prod2LineFilter hidden={{ ...lineHidden, source }} lines={lines} selected={line} /> : null}
      </div>
      {source === "prod2" && keyword ? <div aria-label="Filtros activos" className={styles.activeFilters} role="group">
        <a aria-label={`Quitar el filtro de la palabra ${keyword}`} className={styles.removable} href={prod2ListingHref(defaults, { kw: null })} title="Quitar el filtro de palabra">
          <Tag aria-hidden="true" size={14} /><span>Palabra: <strong>{keyword}</strong></span><X aria-hidden="true" size={14} />
        </a>
      </div> : null}
    </section>
  );
}
