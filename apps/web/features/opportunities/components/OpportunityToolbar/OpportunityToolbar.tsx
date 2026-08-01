import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import styles from "./OpportunityToolbar.module.css";

function hrefFor(entries: Record<string, string>) {
  const params = new URLSearchParams({
    estado: "2",
    has_extraction: "true",
    deadline: "open",
    ...entries,
  });
  Object.entries(entries).forEach(([key, value]) => {
    if (!value) params.delete(key);
  });
  return `/feed?${params.toString()}`;
}

type QuickFilter = {
  label: string;
  params: Record<string, string>;
  empty?: string[];
};

function active(defaults: URLSearchParams, filter: QuickFilter) {
  const matches = Object.entries(filter.params).every(([key, value]) => (defaults.get(key) ?? "") === value);
  const empty = (filter.empty ?? []).every((key) => !defaults.get(key));
  return matches && empty;
}

export function OpportunityToolbar({ defaults }: { defaults: URLSearchParams }) {
  const deadline = defaults.get("deadline") ?? "open";
  const quickFilters: QuickFilter[] = [
    { label: "Guardadas", params: { deadline: "all", saved: "true" }, empty: ["verdict", "has_amount", "cotizar"] },
    { label: "No cerradas", params: { deadline: "open" }, empty: ["verdict", "has_amount", "cotizar", "saved"] },
    { label: "Cierran en 24 h", params: { deadline: "24h" } },
    { label: "Esta semana", params: { deadline: "week" } },
    { label: "Ya cerradas", params: { deadline: "closed" } },
    { label: "Verdes", params: { deadline: "open", verdict: "verde" } },
    { label: "Revisión", params: { deadline: "open", verdict: "gris" } },
    { label: "Con monto", params: { deadline: "open", has_amount: "true" } },
    { label: "Cotizables", params: { deadline: "open", cotizar: "true" } },
  ];

  return (
    <form className={styles.toolbar}>
      <nav className={styles.quickFilters} aria-label="Filtros rapidos">
        {quickFilters.map((filter) => (
          <a
            className={active(defaults, filter) ? styles.activeFilter : ""}
            aria-current={active(defaults, filter) ? "page" : undefined}
            href={hrefFor(filter.params)}
            key={filter.label}
          >
            {filter.label}
          </a>
        ))}
      </nav>
      {defaults.get("saved") ? <input name="saved" type="hidden" value={defaults.get("saved") ?? ""} /> : null}
      <div className={styles.primary}>
        <label className={styles.search}>
          <span>Buscar</span>
          <Input defaultValue={defaults.get("q") ?? ""} name="q" placeholder="Codigo, entidad u objeto" />
        </label>
        <label>
          <span>Rubro</span>
          <Select defaultValue={defaults.get("bucket") ?? ""} name="bucket">
            <option value="">Mis rubros</option>
            <option value="tecnologia">Tecnologia</option>
            <option value="transporte">Transporte</option>
            <option value="legal">Legal</option>
          </Select>
        </label>
        <label>
          <span>Estado</span>
          <Select defaultValue={defaults.get("estado") ?? "2"} name="estado">
            <option value="2">Vigentes</option>
            <option value="">Todos</option>
            <option value="3">En evaluacion</option>
            <option value="4">Culminados</option>
          </Select>
        </label>
        <label>
          <span>Cierre</span>
          <Select defaultValue={deadline} name="deadline">
            <option value="open">No cerradas</option>
            <option value="all">Todos / cualquier cierre</option>
            <option value="24h">Cierran en 24 h</option>
            <option value="week">Cierran esta semana</option>
            <option value="closed">Ya cerradas</option>
          </Select>
        </label>
        <Button type="submit">Filtrar</Button>
      </div>
      <details className={styles.more}>
        <summary>Mas filtros</summary>
        <div className={styles.advanced}>
          <label>
            <span>Analisis</span>
            <Select defaultValue={defaults.get("has_extraction") ?? "true"} name="has_extraction">
              <option value="true">Con IA</option>
              <option value="">Todos</option>
              <option value="false">Sin IA</option>
            </Select>
          </label>
          <label>
            <span>Match</span>
            <Select defaultValue={defaults.get("verdict") ?? ""} name="verdict">
              <option value="">Todos</option>
              <option value="verde">Verde</option>
              <option value="ambar">Ambar</option>
              <option value="rojo">Rojo</option>
              <option value="gris">Revision</option>
            </Select>
          </label>
          <label>
            <span>Cotizacion</span>
            <Select defaultValue={defaults.get("cotizar") ?? ""} name="cotizar">
              <option value="">Todas</option>
              <option value="true">Permite cotizar</option>
              <option value="false">No cotizable</option>
            </Select>
          </label>
          <label>
            <span>Requisito</span>
            <Input defaultValue={defaults.get("facet") ?? ""} name="facet" placeholder="Ej.: licencia, equipo…" />
          </label>
          <label>
            <span>Rol</span>
            <Input defaultValue={defaults.get("role") ?? ""} name="role" placeholder="rol requerido" />
          </label>
          <a className={styles.clear} href="/feed">Limpiar filtros</a>
        </div>
      </details>
    </form>
  );
}
