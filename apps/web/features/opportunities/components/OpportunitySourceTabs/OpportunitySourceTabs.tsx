import Link from "next/link";
import { Building2, RadioTower } from "lucide-react";
import styles from "./OpportunitySourceTabs.module.css";

export function OpportunitySourceTabs({ source }: { source: "prod6" | "prod4" }) {
  return (
    <nav aria-label="Origen de oportunidades" className={styles.tabs}>
      <Link aria-current={source === "prod6" ? "page" : undefined} className={source === "prod6" ? styles.active : ""} href="/feed">
        <RadioTower aria-hidden="true" size={17} /> Contratos menores
      </Link>
      <Link aria-current={source === "prod4" ? "page" : undefined} className={source === "prod4" ? styles.active : ""} href="/feed?source=prod4">
        <Building2 aria-hidden="true" size={17} /> Concursos SEACE
      </Link>
    </nav>
  );
}
