"use client";

import { Bookmark, Clock3, Search, SlidersHorizontal, Sparkles } from "lucide-react";
import { useState } from "react";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import styles from "./OpportunityToolbar.module.css";

function hrefFor(entries: Record<string, string>, defaults: Record<string, string>) {
  const params = new URLSearchParams({ estado: "2", has_extraction: "true", deadline: "open" });
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

function isActive(defaults: Record<string, string>, key: "forYou" | "closing" | "saved") {
  if (key === "saved") return defaults.saved === "true";
  if (key === "closing") return defaults.deadline === "24h";
  return defaults.saved !== "true" && !["24h", "week", "closed", "all"].includes(defaults.deadline ?? "open");
}

export function OpportunityToolbar({ defaults }: { defaults: Record<string, string> }) {
  const deadline = defaults.deadline ?? "open";
  const [filtersOpen, setFiltersOpen] = useState(Boolean(defaults.objeto || defaults.verdict || defaults.cotizar || deadline === "closed" || deadline === "week"));
  return (
    <section className={styles.toolbar}>
      <button aria-controls="opportunity-filters" aria-expanded={filtersOpen} className={styles.filterToggle} onClick={() => setFiltersOpen((value) => !value)} type="button"><SlidersHorizontal aria-hidden="true" size={17} /> Filtros</button>
      <form className={styles.main} action="/feed">
        <input name="has_extraction" type="hidden" value="true" />
        <input name="estado" type="hidden" value={defaults.estado ?? "2"} />
        <input name="deadline" type="hidden" value={deadline} />
        {defaults.objeto === "1" || defaults.objeto === "2" ? <input name="objeto" type="hidden" value={defaults.objeto} /> : null}
        {defaults.saved === "true" ? <input name="saved" type="hidden" value="true" /> : null}
        <label className={styles.search}>
          <Search aria-hidden="true" size={18} />
          <Input aria-label="Buscar oportunidades" autoComplete="off" defaultValue={defaults.q ?? ""} name="q" placeholder="Codigo, entidad u objeto" type="search" />
        </label>
        <nav className={styles.quick} aria-label="Vistas rápidas">
          <a aria-current={isActive(defaults, "forYou") ? "page" : undefined} className={isActive(defaults, "forYou") ? styles.active : ""} href={hrefFor({ deadline: "open" }, defaults)}><Sparkles aria-hidden="true" size={17} /> Para ti</a>
          <a aria-current={isActive(defaults, "closing") ? "page" : undefined} className={isActive(defaults, "closing") ? styles.active : ""} href={hrefFor({ deadline: "24h" }, defaults)}><Clock3 aria-hidden="true" size={17} /> Cierran pronto</a>
          <a aria-current={isActive(defaults, "saved") ? "page" : undefined} className={isActive(defaults, "saved") ? styles.active : ""} href={hrefFor({ deadline: "all", saved: "true" }, defaults)}><Bookmark aria-hidden="true" size={17} /> Guardadas</a>
        </nav>
      </form>
      {filtersOpen ? <div className={styles.disclosure}>
        <form action="/feed" className={styles.filters} id="opportunity-filters">
          <input name="has_extraction" type="hidden" value="true" />
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
          <button type="submit">Aplicar</button>
          <a href="/feed">Limpiar</a>
        </form>
      </div> : null}
    </section>
  );
}
