"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BarChart3, ClipboardCheck, House, Search } from "lucide-react";
import styles from "./NavIconButton.module.css";

const icons = { home: House, search: Search, market: BarChart3, applications: ClipboardCheck };
type NavIconName = keyof typeof icons;

export function NavIconButton({ href, label, icon }: { href: string; label: string; icon: NavIconName }) {
  const pathname = usePathname();
  const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
  const Icon = icons[icon];
  return (
    <Link className={[styles.link, active ? styles.active : ""].join(" ")} href={href} title={label}>
      <Icon aria-hidden="true" size={20} strokeWidth={1.8} />
      <span>{label}</span>
    </Link>
  );
}
