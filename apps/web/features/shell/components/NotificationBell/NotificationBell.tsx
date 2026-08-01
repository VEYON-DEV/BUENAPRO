import Link from "next/link";
import { AppIcon } from "@/components/ui/AppIcon";
import styles from "./NotificationBell.module.css";

export function NotificationBell() {
  return (
    <Link className={styles.bell} href="/alertas" aria-label="Abrir alertas" title="Alertas">
      <AppIcon name="bell" />
    </Link>
  );
}
