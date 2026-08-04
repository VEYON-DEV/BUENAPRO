"use client";

import Link from "next/link";
import { signOut } from "next-auth/react";
import { useEffect, useRef } from "react";
import { Bell, Building2, LogOut, Settings2 } from "lucide-react";
import styles from "./UserMenu.module.css";

export function UserMenu() {
  const menuRef = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    function closeOnOutsidePress(event: PointerEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) menuRef.current.open = false;
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && menuRef.current) {
        menuRef.current.open = false;
        menuRef.current.querySelector("summary")?.focus();
      }
    }
    document.addEventListener("pointerdown", closeOnOutsidePress);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePress);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  return (
    <details className={styles.menu} ref={menuRef}>
      <summary aria-label="Abrir menú de cuenta">
        <span className={styles.avatar} title="VEYON SAC">VY</span>
      </summary>
      <div className={styles.popover}>
        <div className={styles.identity}>
          <strong>VEYON SAC</strong>
          <span>Cuenta de empresa</span>
        </div>
        <Link href="/perfil" onClick={() => { if (menuRef.current) menuRef.current.open = false; }}>
          <Building2 size={16} /> Mi empresa y radar
        </Link>
        <Link href="/alertas" onClick={() => { if (menuRef.current) menuRef.current.open = false; }}>
          <Bell size={16} /> Alertas
        </Link>
        <Link href="/configuracion" onClick={() => { if (menuRef.current) menuRef.current.open = false; }}>
          <Settings2 size={16} /> Configuración
        </Link>
        <button type="button" onClick={() => signOut({ callbackUrl: "/login" })}>
          <LogOut size={16} /> Cerrar sesión
        </button>
      </div>
    </details>
  );
}
