import styles from "./MetricPanel.module.css";

export function MetricPanel({ label, value, detail, icon, tone = "violet" }: {
  label: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
  icon?: React.ReactNode;
  tone?: "violet" | "green" | "amber";
}) {
  return (
    <article className={styles.panel}>
      <div className={[styles.icon, styles[tone]].join(" ")}>{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {detail ? <small>{detail}</small> : null}
      </div>
    </article>
  );
}
