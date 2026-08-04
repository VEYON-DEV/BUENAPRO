import { NotificationBell } from "../NotificationBell";
import { UserMenu } from "../UserMenu";
import styles from "./TopBar.module.css";

export function TopBar() {
  return (
    <header className={styles.topbar}>
      <div className={styles.actions}>
        <span className={styles.context}>SEACE</span>
        <NotificationBell />
        <UserMenu />
      </div>
    </header>
  );
}
