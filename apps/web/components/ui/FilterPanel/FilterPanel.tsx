import { AppIcon } from "../AppIcon";
import { Input } from "../Input";
import styles from "./FilterPanel.module.css";

type Props = React.HTMLAttributes<HTMLDivElement>;

export function FilterPanel({ className, ...props }: Props) {
  return <section className={[styles.panel, className].filter(Boolean).join(" ")} {...props} />;
}

export function FilterTabs({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={[styles.tabs, className].filter(Boolean).join(" ")} role="tablist" {...props} />;
}

export function FilterTab({ active = false, className, href, children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean; href?: string }) {
  const classes = [styles.tab, active ? styles.tabActive : "", className].filter(Boolean).join(" ");
  if (href) {
    return <a className={classes} href={href} role="tab" aria-selected={active}>{children}</a>;
  }
  return (
    <button
      className={classes}
      role="tab"
      aria-selected={active}
      type="button"
      {...props}
    >{children}</button>
  );
}

export function FilterChips({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={[styles.chips, className].filter(Boolean).join(" ")} {...props} />;
}

export function FilterChip({ active = false, className, href, children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean; href?: string }) {
  const classes = [styles.chip, active ? styles.chipActive : "", className].filter(Boolean).join(" ");
  if (href) {
    return <a className={classes} href={href} aria-current={active ? "page" : undefined}>{children}</a>;
  }
  return (
    <button
      className={classes}
      aria-pressed={active}
      type="button"
      {...props}
    >{children}</button>
  );
}

export function FilterForm({ className, ...props }: React.FormHTMLAttributes<HTMLFormElement>) {
  return <form className={[styles.form, className].filter(Boolean).join(" ")} {...props} />;
}

export function FilterField({ label, htmlFor, className, children }: { label: string; htmlFor?: string; className?: string; children: React.ReactNode }) {
  return (
    <div className={[styles.field, className].filter(Boolean).join(" ")}>
      <label htmlFor={htmlFor}>{label}</label>
      {children}
    </div>
  );
}

export function FilterSearch({ className, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className={[styles.search, className].filter(Boolean).join(" ")}>
      <AppIcon name="search" />
      <Input type="search" {...props} />
    </div>
  );
}

export function FilterActions({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={[styles.actions, className].filter(Boolean).join(" ")} {...props} />;
}

export function FilterDisclosure({ summary, className, children }: { summary: string; className?: string; children: React.ReactNode }) {
  return (
    <details className={[styles.disclosure, className].filter(Boolean).join(" ")}>
      <summary>
        <AppIcon name="filter" />
        {summary}
      </summary>
      <div className={styles.disclosureBody}>{children}</div>
    </details>
  );
}
