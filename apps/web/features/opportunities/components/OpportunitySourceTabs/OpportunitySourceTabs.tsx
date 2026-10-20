import { COMMERCIAL_FILTER_KEYS } from "@/lib/commercialTaxonomy";
import Link from "next/link";
import { Building2, RadioTower } from "lucide-react";
import styles from "./OpportunitySourceTabs.module.css";

export function OpportunitySourceTabs({ source, profileId, withScore = false, commercialFilters = {} }: { source: "prod2" | "prod6" | "prod4"; profileId?: string; withScore?: boolean; commercialFilters?: Record<string, string> }) {
  function href(target: "prod2" | "prod6") {
    const params = new URLSearchParams();
    if (target === "prod2") params.set("source", target);
    if (profileId) params.set("profile_id", profileId);
    if (withScore) params.set("with_score", "true");
    COMMERCIAL_FILTER_KEYS.forEach(key => { if (commercialFilters[key]) params.set(key, commercialFilters[key]); });
    return `/feed${params.size ? `?${params.toString()}` : ""}`;
  }
  return (
    <nav aria-label="Origen de oportunidades" className={styles.tabs}>
      <Link aria-current={source === "prod2" ? "page" : undefined} className={source === "prod2" ? styles.active : ""} href={href("prod2")}>
        <Building2 aria-hidden="true" size={17} /> Buscador SEACE
      </Link>
      <Link aria-current={source === "prod6" ? "page" : undefined} className={source === "prod6" ? styles.active : ""} href={href("prod6")}>
        <RadioTower aria-hidden="true" size={17} /> Contratos menores
      </Link>
    </nav>
  );
}
