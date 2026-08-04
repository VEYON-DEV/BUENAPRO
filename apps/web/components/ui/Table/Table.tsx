import styles from "./Table.module.css";

export function Table({ children, className, embedded = false }: { children: React.ReactNode; className?: string; embedded?: boolean }) {
  return (
    <div className={[styles.wrap, embedded ? styles.embedded : ""].filter(Boolean).join(" ")}>
      <table className={[styles.table, className].filter(Boolean).join(" ")}>{children}</table>
    </div>
  );
}
