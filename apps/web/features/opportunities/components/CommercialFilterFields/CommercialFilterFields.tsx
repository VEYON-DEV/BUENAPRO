"use client";

import { ChevronDown } from "lucide-react";
import { useState } from "react";
import { Select } from "@/components/ui/Select";
import { commercialTaxonomy, COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";
import { clearCommercialFiltersHref, commercialFilterValues, commercialSelectionCount } from "../../model/commercialFilters";
import styles from "./CommercialFilterFields.module.css";

type Option = { id: string; label: string };

function ChoiceGroup({ label, name, options, initial }: {
  label: string; name: string; options: Option[]; initial: string[];
}) {
  const [selected, setSelected] = useState(initial);
  return <details className={styles.group} open={initial.length > 0 || undefined}>
    <summary className={styles.summary}>
      <span><strong>{label}</strong><span>{selected.length ? `${selected.length} ${selected.length === 1 ? "selección" : "selecciones"}` : "Todos"}</span></span>
      <ChevronDown aria-hidden="true" size={16} />
    </summary>
    <input name={name} type="hidden" value={selected.join(",")} />
    <fieldset className={styles.choices}>
      <legend className={styles.srOnly}>{label}: selecciona una o varias opciones</legend>
      {options.map(option => <label className={styles.choice} key={option.id}>
        <input checked={selected.includes(option.id)} onChange={event => { const checked = event.target.checked; setSelected(previous => checked
          ? [...previous, option.id] : previous.filter(value => value !== option.id)); }} type="checkbox" />
        <span>{option.label}</span>
      </label>)}
    </fieldset>
  </details>;
}

export function CommercialFilterFields({ defaults }: { defaults: Record<string, string> }) {
  return <section aria-label="Clasificación comercial" className={styles.root}>
    <div className={styles.heading}><div><h3>Clasificación comercial</h3><p>Combina un rubro con el trabajo que buscas. Puedes elegir varias opciones.</p></div>
      {commercialSelectionCount(defaults) > 0 ? <a href={clearCommercialFiltersHref(defaults)}>Quitar clasificación</a> : null}
    </div>
    <div className={styles.fields}>
      <ChoiceGroup initial={commercialFilterValues(defaults, "commercial_area")} label="Rubro" name="commercial_area" options={commercialTaxonomy.areas.filter(option => option.id !== "sin_clasificar")} />
      <ChoiceGroup initial={commercialFilterValues(defaults, "commercial_work")} label="Tipo de trabajo" name="commercial_work" options={commercialTaxonomy.work_types} />
      <ChoiceGroup initial={commercialFilterValues(defaults, "commercial_specialty")} label="Especialidad" name="commercial_specialty" options={commercialTaxonomy.specialties} />
      <label className={styles.status}><span>Estado de clasificación</span><Select defaultValue={defaults.commercial_status ?? ""} name="commercial_status">
        <option value="">Todas</option>
        {commercialTaxonomy.support_statuses.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
        <option value="unclassified">Sin clasificar</option>
      </Select></label>
    </div>
    <p className={styles.note}>Las oportunidades sin clasificación siguen disponibles en «Sin clasificar».</p>
  </section>;
}

export function CommercialFilterHiddenInputs({ defaults }: { defaults: Record<string, string> }) {
  return <>{COMMERCIAL_FILTER_KEYS.map(key => defaults[key]
    ? <input key={key} name={key} type="hidden" value={defaults[key]} /> : null)}</>;
}
