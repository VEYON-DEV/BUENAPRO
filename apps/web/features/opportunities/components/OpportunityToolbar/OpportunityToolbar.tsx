"use client";

import { Bookmark, Clock3, FileCheck2, LayoutList, Search, SlidersHorizontal, Sparkles } from "lucide-react";
import { useState } from "react";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { CommercialFilterFields, CommercialFilterHiddenInputs } from "../CommercialFilterFields";
import { COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";
import { commercialDefaultsKey, commercialSelectionCount } from "../../model/commercialFilters";
import { scoreFilterHref } from "../../model/analysisPresentation";
import styles from "./OpportunityToolbar.module.css";

function hrefFor(entries: Record<string, string>, defaults: Record<string, string>) {
  const params = new URLSearchParams({ estado: "2", has_extraction: "true", deadline: "open" });
  ["source", "profile_id", "q", "analysis", "with_score", ...COMMERCIAL_FILTER_KEYS].forEach(key => { if (defaults[key]) params.set(key, defaults[key]); });
  if (defaults.objeto === "1" || defaults.objeto === "2") params.set("objeto", defaults.objeto);
  Object.entries(entries).forEach(([key, value]) => { if (value) params.set(key, value); else params.delete(key); });
  return `/feed?${params.toString()}`;
}

function typeHref(defaults: Record<string, string>, object: string) {
  const params = new URLSearchParams(defaults);
  params.delete("page");
  params.delete("open_only");
  params.delete("closing_before");
  if (object) params.set("objeto", object);
  else params.delete("objeto");
  return `/feed?${params.toString()}`;
}

function isActive(defaults: Record<string, string>, key: "all" | "forYou" | "closing" | "saved") {
  if (key === "saved") return defaults.saved === "true";
  if (key === "closing") return defaults.deadline === "24h";
  if (key === "all") return defaults.saved !== "true" && (defaults.deadline ?? "all") === "all";
  return defaults.saved !== "true" && !["24h", "week", "closed", "all"].includes(defaults.deadline ?? "all");
}

export function OpportunityToolbar({ defaults }: { defaults: Record<string, string> }) {
  const deadline = defaults.deadline ?? "all";
  const [filtersOpen, setFiltersOpen] = useState(Boolean(defaults.objeto || defaults.verdict || defaults.cotizar || deadline === "closed" || deadline === "week" || commercialSelectionCount(defaults) > 0));
  return (
    <section className={styles.toolbar}>
      <button aria-controls="opportunity-filters" aria-expanded={filtersOpen} className={styles.filterToggle} onClick={() => setFiltersOpen((value) => !value)} type="button"><SlidersHorizontal aria-hidden="true" size={17} /> Filtros{commercialSelectionCount(defaults) > 0 ? ` (${commercialSelectionCount(defaults)})` : ""}</button>
      <form className={styles.main} action="/feed">
        <CommercialFilterHiddenInputs defaults={defaults} />
        <input name="has_extraction" type="hidden" value="true" />
        <input name="estado" type="hidden" value={defaults.estado ?? "2"} />
        <input name="deadline" type="hidden" value={deadline} />
        {defaults.source ? <input name="source" type="hidden" value={defaults.source} /> : null}
        {defaults.profile_id ? <input name="profile_id" type="hidden" value={defaults.profile_id} /> : null}
        {defaults.analysis === "done" ? <input name="analysis" type="hidden" value="done" /> : null}
        {defaults.with_score === "true" ? <input name="with_score" type="hidden" value="true" /> : null}
        {defaults.objeto === "1" || defaults.objeto === "2" ? <input name="objeto" type="hidden" value={defaults.objeto} /> : null}
        {defaults.saved === "true" ? <input name="saved" type="hidden" value="true" /> : null}
        <label className={styles.search}>
          <Search aria-hidden="true" size={18} />
          <Input aria-label="Buscar oportunidades" autoComplete="off" defaultValue={defaults.q ?? ""} name="q" placeholder="Codigo, entidad u objeto" type="search" />
        </label>
        <nav className={styles.quick} aria-label="Vistas rápidas">
          <a aria-current={isActive(defaults, "all") ? "page" : undefined} className={isActive(defaults, "all") ? styles.active : ""} href={hrefFor({ deadline: "all", estado: "" }, defaults)}><LayoutList aria-hidden="true" size={17} /> Todo</a>
          <a aria-current={isActive(defaults, "forYou") ? "page" : undefined} className={isActive(defaults, "forYou") ? styles.active : ""} href={hrefFor({ deadline: "open" }, defaults)}><Sparkles aria-hidden="true" size={17} /> Para ti</a>
          <a aria-current={isActive(defaults, "closing") ? "page" : undefined} className={isActive(defaults, "closing") ? styles.active : ""} href={hrefFor({ deadline: "24h" }, defaults)}><Clock3 aria-hidden="true" size={17} /> Cierran pronto</a>
          <a aria-current={isActive(defaults, "saved") ? "page" : undefined} className={isActive(defaults, "saved") ? styles.active : ""} href={hrefFor({ deadline: "all", saved: "true" }, defaults)}><Bookmark aria-hidden="true" size={17} /> Guardadas</a>
          <a aria-current={defaults.with_score === "true" ? "page" : undefined} className={defaults.with_score === "true" ? styles.active : ""} href={scoreFilterHref(defaults)}><FileCheck2 aria-hidden="true" size={17} /> Con puntaje</a>
        </nav>
      </form>
      {filtersOpen ? <div className={styles.disclosure}>
        <form action="/feed" className={styles.filters} id="opportunity-filters">
          <input name="has_extraction" type="hidden" value="true" />
          {defaults.source ? <input name="source" type="hidden" value={defaults.source} /> : null}
          {defaults.profile_id ? <input name="profile_id" type="hidden" value={defaults.profile_id} /> : null}
          {defaults.analysis === "done" ? <input name="analysis" type="hidden" value="done" /> : null}
          {defaults.with_score === "true" ? <input name="with_score" type="hidden" value="true" /> : null}
          {defaults.objeto === "1" || defaults.objeto === "2" ? <input name="objeto" type="hidden" value={defaults.objeto} /> : null}
          {defaults.q ? <input name="q" type="hidden" value={defaults.q} /> : null}
          {defaults.saved === "true" ? <input name="saved" type="hidden" value="true" /> : null}
          <div className={styles.typeFilter}>
            <span>Tipo</span>
            <div aria-label="Tipo de contratación" className={styles.typeChoices} role="group">
              {[["", "Todos"], ["1", "Bienes"], ["2", "Servicios"]].map(([value, label]) => {
                const active = (defaults.objeto ?? "") === value;
                return <a aria-current={active ? "page" : undefined} className={active ? styles.typeActive : undefined} href={typeHref(defaults, value)} key={value}>{label}</a>;
              })}
            </div>
          </div>
          <label><span>Estado</span><Select defaultValue={defaults.estado ?? "2"} name="estado"><option value="2">Vigentes</option><option value="">Todos</option><option value="3">En evaluacion</option><option value="4">Culminados</option></Select></label>
          <label><span>Cierre</span><Select defaultValue={deadline} name="deadline"><option value="open">No cerradas</option><option value="24h">Proximas 24 h</option><option value="week">Esta semana</option><option value="closed">Ya cerradas</option><option value="all">Cualquier cierre</option></Select></label>
          <label><span>Afinidad</span><Select defaultValue={defaults.verdict ?? ""} name="verdict"><option value="">Todas</option><option value="verde">Alta</option><option value="ambar">Media</option><option value="gris">Por revisar</option></Select></label>
          <label><span>Cotizacion</span><Select defaultValue={defaults.cotizar ?? ""} name="cotizar"><option value="">Todas</option><option value="true">Cotizacion habilitada</option><option value="false">No habilitada</option></Select></label>
          <CommercialFilterFields defaults={defaults} key={commercialDefaultsKey(defaults)} />
          <button type="submit">Aplicar filtros</button>
          <a href="/feed">Limpiar</a>
        </form>
      </div> : null}
    </section>
  );
}
