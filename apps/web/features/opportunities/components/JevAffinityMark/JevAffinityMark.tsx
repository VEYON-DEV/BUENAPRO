import { Sparkles } from "lucide-react";
import { JEV_DISCLAIMER, type JevAffinity } from "@/lib/jevAffinity";
import styles from "./JevAffinityMark.module.css";

type JevAffinityMarkProps = {
  affinity: JevAffinity;
  /** `row`: solo porcentaje y rótulo; `inspector`: añade subcategoría, umbral y aclaración. */
  variant?: "row" | "inspector";
};

/** Afinidad comercial preliminar JEV del perfil activo. No evalúa requisitos ni elegibilidad. */
export function JevAffinityMark({ affinity, variant = "row" }: JevAffinityMarkProps) {
  if (variant === "row") {
    return (
      <span className={styles.row} data-jev-affinity title={affinity.description}>
        <strong>{affinity.valueLabel}</strong>
        <span>{affinity.label}</span>
      </span>
    );
  }
  return (
    <section className={styles.inspector} data-jev-affinity aria-label={affinity.label}>
      <div className={styles.headline}>
        <span className={styles.value}>
          <strong>{affinity.score}</strong>
          <small>/ 100</small>
        </span>
        <div className={styles.copy}>
          <span className={styles.title}><Sparkles aria-hidden="true" size={14} />{affinity.label}</span>
          {affinity.subcategoryLabel ? <span className={styles.chip}>{affinity.subcategoryLabel}</span> : null}
        </div>
      </div>
      <p className={styles.note}>
        {affinity.threshold != null ? `Umbral de aprobación: ${Math.round(affinity.threshold)}. ` : ""}{JEV_DISCLAIMER}
      </p>
    </section>
  );
}
