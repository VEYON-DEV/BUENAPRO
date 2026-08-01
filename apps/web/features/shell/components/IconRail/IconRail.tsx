import { NavIconButton } from "../NavIconButton";
import styles from "./IconRail.module.css";

const nav = [
  { href: "/", label: "Inicio", icon: "home" as const },
  { href: "/feed", label: "Oportunidades", icon: "search" as const },
  { href: "/mercado", label: "Mercado", icon: "market" as const },
  { href: "/seguimiento", label: "Seguimiento", icon: "track" as const },
  { href: "/perfil", label: "Perfil", icon: "profile" as const },
  { href: "/configuracion", label: "Configuracion", icon: "settings" as const },
  { href: "/admin", label: "Admin", icon: "admin" as const },
];

export function IconRail() {
  return (
    <aside className={styles.rail}>
      <a className={styles.brand} href="/" aria-label="BuenaPro">
        <span className={styles.mark}>BP</span>
        <span className={styles.wordmark}>BuenaPro</span>
      </a>
      <nav className={styles.nav}>
        {nav.map((item) => (
          <NavIconButton key={item.href} {...item} />
        ))}
      </nav>
      <div className={styles.workspace}>
        <span>VEYON SAC</span>
        <strong>Pro</strong>
      </div>
    </aside>
  );
}
