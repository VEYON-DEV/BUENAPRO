import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { RANGE_OPTIONS, opsHref } from "@/server/services/ops/filters";
import type { OpsFilters } from "@/server/services/ops/filters";
import type { OpsCompany } from "@/server/services/ops/types";
import styles from "./OpsFilterBar.module.css";

/** Rango y empresa del panel; ambos viajan en la URL, así que cada vista es un enlace compartible. */
export function OpsFilterBar({ filters, companies }: { filters: OpsFilters; companies: readonly OpsCompany[] }) {
  const href = (patch: { range?: number; company?: string | null }) =>
    opsHref("/ops", {
      range: (patch.range ?? filters.range) === 14 ? null : (patch.range ?? filters.range),
      company: "company" in patch ? patch.company : filters.company,
      day: filters.day === filters.today ? null : filters.day,
    });
  const knownCompany = filters.company == null || companies.some((company) => company.id === filters.company);
  return (
    <div aria-label="Filtros del panel" className={styles.bar} role="group">
      <div className={styles.field}>
        <span className={styles.label} id="ops-range-label">Rango</span>
        <SegmentedControl
          items={RANGE_OPTIONS.map((range) => ({ label: `${range} días`, href: href({ range }), active: range === filters.range }))}
        />
      </div>
      <div className={styles.field}>
        <span className={styles.label} id="ops-company-label">Empresa</span>
        <SegmentedControl
          items={[
            { label: "Todas", href: href({ company: null }), active: filters.company == null },
            ...companies.map((company) => ({ label: company.label, href: href({ company: company.id }), active: company.id === filters.company })),
            ...(knownCompany ? [] : [{ label: "Otra empresa", href: href({ company: filters.company }), active: true }]),
          ]}
        />
      </div>
      <p className={styles.note}>
        Días de Lima. Los pasos «compartido» son por licitación y no cambian con la empresa.
      </p>
    </div>
  );
}
