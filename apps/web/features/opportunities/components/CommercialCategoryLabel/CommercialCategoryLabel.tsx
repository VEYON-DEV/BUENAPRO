import { commercialCategories, type CommercialCategoryPresentation } from "@/lib/commercialTaxonomy";
import styles from "./CommercialCategoryLabel.module.css";

type Props = Partial<CommercialCategoryPresentation> & { compact?: boolean };

export function CommercialCategoryLabel({ commercial_categories = [], commercial_classification_source = null,
  commercial_classification_state = "unclassified", compact = false }: Props) {
  const categories = commercialCategories.categories.filter(category => commercial_categories.includes(category.id));
  const labels = categories.length ? categories.map(category => category.label)
    : [commercial_classification_state === "other" ? "Otros" : commercial_classification_state === "unclear" ? "Por confirmar" : "Por clasificar"];
  const preliminary = commercial_classification_source === "title";
  const context = preliminary ? "Por título: clasificación preliminar; las bases pueden precisar la categoría."
    : commercial_classification_source === "document" ? "Según la clasificación de las bases." : "Todavía no hay una clasificación disponible.";
  const explanation = `${labels.join("; ")}. ${context}`;
  return <span aria-label={explanation} className={[styles.root, compact ? styles.compact : ""].join(" ")} title={explanation}>
    <span className={styles.srOnly}>{explanation}</span>
    <span aria-hidden="true" className={styles.labels}>
      {(compact ? labels.slice(0, 1) : labels).map(label => <span className={styles.label} key={label}>{label}</span>)}
      {compact && labels.length > 1 ? <span className={styles.more}>+{labels.length - 1}</span> : null}
    </span>
    {preliminary ? <span aria-hidden="true" className={styles.source}>Por título</span> : null}
  </span>;
}
