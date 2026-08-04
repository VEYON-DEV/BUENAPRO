import Link from "next/link";
import { ArrowRight } from "lucide-react";
import styles from "./DailyMetric.module.css";

export function DailyMetric({
  href,
  icon,
  label,
  value,
  detail,
  tone = "violet",
}: {
  href: string;
  icon: React.ReactNode;
  label: string;
  value: string;
  detail: string;
  tone?: "violet" | "blue" | "green";
}) {
  return (
    <Link className={styles.metric} href={href}>
      <span className={`${styles.icon} ${styles[tone]}`}>{icon}</span>
      <span className={styles.copy}>
        <span>{label}</span>
        <strong>{value}</strong>
        <small>{detail}</small>
      </span>
      <ArrowRight aria-hidden="true" className={styles.arrow} size={17} />
    </Link>
  );
}
