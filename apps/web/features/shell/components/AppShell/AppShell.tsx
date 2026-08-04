import { IconRail } from "../IconRail";
import { TopBar } from "../TopBar";
import styles from "./AppShell.module.css";

export function AppShell({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <div className={styles.shell}>
      <a className={styles.skipLink} href="#main-content">Ir al contenido</a>
      <IconRail />
      <div className={styles.workspace}>
        <TopBar />
        <main className={styles.main} id="main-content">{children}</main>
      </div>
    </div>
  );
}
