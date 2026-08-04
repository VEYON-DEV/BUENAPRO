import styles from "./PageHeader.module.css";

export function PageHeader({
  title,
  description,
  eyebrow,
  meta,
  actions,
  children,
  className,
}: {
  title: string;
  description?: React.ReactNode;
  eyebrow?: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <header className={[styles.header, className].filter(Boolean).join(" ")}>
      <div className={styles.copy}>
        {eyebrow ? <div className={styles.eyebrow}>{eyebrow}</div> : null}
        <div className={styles.titleRow}>
          <h1>{title}</h1>
          {meta ? <div className={styles.meta}>{meta}</div> : null}
        </div>
        {description ? <p>{description}</p> : null}
        {children}
      </div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
    </header>
  );
}
