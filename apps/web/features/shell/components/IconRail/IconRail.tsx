import { NavIconButton } from "../NavIconButton";
import styles from "./IconRail.module.css";

const nav = [
  { href: "/feed", label: "Oportunidades", icon: "search" as const },
  { href: "/mercado", label: "Mercado", icon: "market" as const },
  { href: "/postulaciones", label: "Postulaciones", icon: "track" as const },
];

export function IconRail() {
  return (
    <aside className={styles.rail}>
      <a className={styles.brand} href="/feed" aria-label="BuenaPro">
        <span className={styles.mark}>BP</span>
        <span className={styles.wordmark}>BuenaPro</span>
      </a>
      <nav className={styles.nav}>
        {nav.map((item) => (
          <NavIconButton key={item.href} {...item} />
        ))}
      </nav>
    </aside>
  );
}
