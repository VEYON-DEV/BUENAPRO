"use client";

import { useState } from "react";
import { Select } from "@/components/ui/Select";
import { commercialCategories, COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";
import { clearCommercialFiltersHref, commercialCategorySelection, commercialSelectionCount } from "../../model/commercialFilters";
import styles from "./CommercialFilterFields.module.css";

export function CommercialFilterFields({ defaults }: { defaults: Record<string, string> }) {
  const [category, setCategory] = useState(() => commercialCategorySelection(defaults));
  return <section aria-label="Categoría" className={styles.root}>
    <label className={styles.field}>
      <span>Categoría</span>
      <Select name={category === "previous" ? undefined : "commercial_category"} onChange={event => setCategory(event.target.value)} value={category}>
        <option value="">Todas</option>
        {commercialCategories.categories.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
        {category === "previous" ? <option value="previous">Selección anterior</option> : null}
      </Select>
    </label>
    {category === "previous" ? <CommercialFilterHiddenInputs defaults={defaults} /> : null}
    {commercialSelectionCount(defaults) > 0 ? <a href={clearCommercialFiltersHref(defaults)}>Quitar categoría</a> : null}
  </section>;
}

export function CommercialFilterHiddenInputs({ defaults }: { defaults: Record<string, string> }) {
  return <>{COMMERCIAL_FILTER_KEYS.map(key => defaults[key]
    ? <input key={key} name={key} type="hidden" value={defaults[key]} /> : null)}</>;
}
