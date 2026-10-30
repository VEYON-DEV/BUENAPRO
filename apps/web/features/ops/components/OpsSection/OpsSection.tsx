import styles from "./OpsSection.module.css";

/** Sección del panel: título, descripción corta y un espacio opcional a la derecha para controles. */
export function OpsSection({
  id,
  title,
  description,
  aside,
  children,
}: {
  id: string;
  title: string;
  description?: React.ReactNode;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={`${id}-title`} className={styles.section} id={id}>
      <header className={styles.header}>
        <div className={styles.copy}>
          <h2 id={`${id}-title`}>{title}</h2>
          {description ? <p>{description}</p> : null}
        </div>
        {aside ? <div className={styles.aside}>{aside}</div> : null}
      </header>
      {children}
    </section>
  );
}
