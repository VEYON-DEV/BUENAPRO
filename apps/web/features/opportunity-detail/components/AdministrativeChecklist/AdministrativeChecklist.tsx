import { ChevronDown, ClipboardList } from "lucide-react";
import styles from "./AdministrativeChecklist.module.css";

type PreparationItem = { requisito?: string; label?: string };

export function AdministrativeChecklist({ items }: { items: PreparationItem[] }) {
  if (!items.length) return null;

  return (
    <details className={styles.checklist}>
      <summary>
        <ClipboardList aria-hidden="true" size={18} />
        <span><strong>Documentos para presentar</strong><small>{items.length} preparativos · No reducen el puntaje</small></span>
        <ChevronDown className={styles.chevron} aria-hidden="true" size={18} />
      </summary>
      <div className={styles.body}>
        <p>Prepara los formatos y constancias vigentes al presentar tu oferta. Su inclusión en esta lista no acredita una brecha de capacidad.</p>
        <ul>{items.map((item, index) => <li key={`${item.requisito ?? item.label}-${index}`}>{item.requisito ?? item.label}</li>)}</ul>
      </div>
    </details>
  );
}
