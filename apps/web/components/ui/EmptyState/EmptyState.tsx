import { Inbox, type LucideIcon } from "lucide-react";
import { buttonClassName } from "@/components/ui/Button";
import styles from "./EmptyState.module.css";

export function EmptyState({
  title,
  children,
  action,
  icon: Icon = Inbox,
}: {
  title: string;
  children?: React.ReactNode;
  action?: { label: string; href?: string };
  icon?: LucideIcon;
}) {
  return (
    <div className={styles.empty}>
      <span className={styles.mark} aria-hidden="true"><Icon size={20} strokeWidth={1.8} /></span>
      <h2>{title}</h2>
      {children ? <p>{children}</p> : null}
      {action?.href ? <a className={buttonClassName("primary", "normal", styles.link)} href={action.href}>{action.label}</a> : null}
    </div>
  );
}
