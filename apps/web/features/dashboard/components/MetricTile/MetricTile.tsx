import Link from "next/link";
import styles from "./MetricTile.module.css";

type MetricTileProps = {
  label: string;
  value: number | string;
  caption: string;
  href?: string;
  accent?: boolean;
};

export function MetricTile({ label, value, caption, href, accent = false }: MetricTileProps) {
  const content = (
    <>
      <span>{label}</span>
      <strong className={accent ? styles.accent : undefined}>{value}</strong>
      <small>{caption}</small>
    </>
  );

  return href ? (
    <Link className={styles.tile} href={href}>{content}</Link>
  ) : (
    <article className={styles.tile}>{content}</article>
  );
}
