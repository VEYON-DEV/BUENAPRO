import Link from "next/link";
import { Bell } from "lucide-react";
import styles from "./NotificationBell.module.css";

export function NotificationBell() {
  return (
    <Link className={styles.bell} href="/alertas" aria-label="Abrir alertas" title="Alertas">
      <Bell aria-hidden="true" size={17} strokeWidth={1.8} />
    </Link>
  );
}
