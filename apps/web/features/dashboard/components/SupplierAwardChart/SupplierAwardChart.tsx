import styles from "./SupplierAwardChart.module.css";

type Supplier = {
  name: string;
  awards: number | string;
};

export function SupplierAwardChart({ suppliers }: { suppliers: Supplier[] }) {
  const maximum = Math.max(...suppliers.map((supplier) => Number(supplier.awards)), 1);

  return (
    <figure className={styles.chart} aria-label="Adjudicaciones por proveedor">
      {suppliers.map((supplier) => {
        const width = Math.max(8, Math.round((Number(supplier.awards) / maximum) * 100));
        return (
          <div className={styles.row} key={supplier.name}>
            <span className={styles.name} title={supplier.name} translate="no">{supplier.name}</span>
            <span className={styles.track} aria-hidden="true">
              <i style={{ width: `${width}%` }} />
            </span>
            <strong>{supplier.awards}</strong>
          </div>
        );
      })}
      <figcaption>Adjudicaciones registradas</figcaption>
    </figure>
  );
}
