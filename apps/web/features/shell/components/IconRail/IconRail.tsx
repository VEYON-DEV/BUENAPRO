"use client";

import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { BrandLogo } from "@/components/ui/BrandLogo";
import { NavIconButton } from "../NavIconButton";
import styles from "./IconRail.module.css";

const nav = [
  { href: "/", label: "Inicio", icon: "home" as const },
  { href: "/feed", label: "Oportunidades", icon: "search" as const },
  { href: "/mercado", label: "Mercado", icon: "market" as const },
  { href: "/postulaciones", label: "Postulaciones", icon: "applications" as const },
];

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

export function IconRail() {
  const [companyName, setCompanyName] = useState("Mi empresa");

  useEffect(() => {
    let active = true;
    fetch("/api/profile")
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        const name = payload?.data?.razon_social ?? payload?.razon_social;
        if (active && typeof name === "string" && name.trim()) setCompanyName(name);
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  return (
    <aside className={styles.rail}>
      <Link className={styles.brand} href="/" aria-label="BuenaPro">
        <BrandLogo className={styles.fullLogo} />
        <BrandLogo className={styles.compactLogo} compact />
      </Link>
      <nav className={styles.nav}>
        {nav.map((item) => (
          <NavIconButton key={item.href} {...item} />
        ))}
      </nav>
      <Link className={styles.company} href="/perfil" aria-label="Abrir Mi empresa y radar">
        <span className={styles.companyMark}>{initials(companyName)}</span>
        <span className={styles.companyCopy}>
          <strong>{companyName}</strong>
          <small>Mi empresa y radar</small>
        </span>
        <ChevronRight aria-hidden="true" className={styles.companyArrow} size={16} />
      </Link>
    </aside>
  );
}
