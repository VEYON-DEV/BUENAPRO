"use client";

import { Search } from "lucide-react";
import { Input } from "@/components/ui/Input";
import styles from "./Prod4OpportunityToolbar.module.css";

function filterHref(defaults: Record<string, string>, object: string) {
  const next = new URLSearchParams(defaults);
  next.set("source", "prod4");
  next.delete("page");
  if (object) next.set("object", object);
  else next.delete("object");
  return `/feed?${next.toString()}`;
}

export function Prod4OpportunityToolbar({ defaults }: { defaults: Record<string, string> }) {
  const object = defaults.object ?? "";
  return (
    <section className={styles.toolbar}>
      <form action="/feed" className={styles.searchForm}>
        <input name="source" type="hidden" value="prod4" />
        {object ? <input name="object" type="hidden" value={object} /> : null}
        <label className={styles.search}>
          <Search aria-hidden="true" size={18} />
          <Input aria-label="Buscar concursos de tecnología" autoComplete="off" defaultValue={defaults.q ?? ""} name="q" placeholder="Ej.: licencia de software…" type="search" />
        </label>
        <button className={styles.searchButton} type="submit">Buscar</button>
      </form>
      <div aria-label="Objeto de contratación" className={styles.objectChoices} role="group">
        {[["", "Todos"], ["good", "Bienes"], ["service", "Servicios"]].map(([value, label]) => (
          <a aria-current={object === value ? "page" : undefined} className={object === value ? styles.active : ""} href={filterHref(defaults, value)} key={value}>
            {label}
          </a>
        ))}
      </div>
    </section>
  );
}
