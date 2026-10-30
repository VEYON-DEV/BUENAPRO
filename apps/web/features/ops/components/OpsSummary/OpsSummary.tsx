import { TriangleAlert, Coins, Hand, Hourglass } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import styles from "./OpsSummary.module.css";

export type OpsSummaryItem = {
  label: string;
  value: string;
  detail?: string;
  tone: "neutral" | "bad" | "good";
  href?: string;
  icon: "pending" | "failures" | "cost" | "manual";
};

const icons: Record<OpsSummaryItem["icon"], LucideIcon> = {
  pending: Hourglass,
  failures: TriangleAlert,
  cost: Coins,
  manual: Hand,
};

/** Franja de lo más urgente del día. Cada dato lleva su etiqueta y, cuando hay a dónde ir, un enlace. */
export function OpsSummary({ items }: { items: OpsSummaryItem[] }) {
  return (
    <ul aria-label="Resumen de hoy" className={styles.summary}>
      {items.map((item) => {
        const Icon = icons[item.icon];
        const body = (
          <>
            <span aria-hidden="true" className={`${styles.icon} ${styles[item.tone]}`}>
              <Icon size={16} strokeWidth={1.9} />
            </span>
            <span className={styles.copy}>
              <span className={styles.label}>{item.label}</span>
              <strong className={styles.value}>{item.value}</strong>
              {item.detail ? <small className={styles.detail}>{item.detail}</small> : null}
            </span>
          </>
        );
        return (
          <li key={item.label}>
            {item.href ? <a className={styles.item} href={item.href}>{body}</a> : <div className={styles.item}>{body}</div>}
          </li>
        );
      })}
    </ul>
  );
}

