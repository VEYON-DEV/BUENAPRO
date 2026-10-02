"use client";

import { useEffect, useState, type ReactNode } from "react";
import { Building2, BriefcaseBusiness, Files, Radar, ShieldCheck, UsersRound } from "lucide-react";
import { ProfileForm } from "../ProfileForm";
import type { ProfileSection } from "../ProfileForm/ProfileForm";
import styles from "./ProfileWorkspace.module.css";

const sections = [
  { id: "radar", label: "Radar", icon: Radar },
  { id: "team", label: "Equipo", icon: UsersRound },
  { id: "experience", label: "Experiencia", icon: BriefcaseBusiness },
  { id: "resources", label: "Recursos", icon: ShieldCheck },
  { id: "library", label: "Documentos", icon: Files },
  { id: "company", label: "Empresa", icon: Building2 },
];
export function ProfileWorkspace({ profile, radar, library, connection }: { profile: any; radar: ReactNode; library: ReactNode; connection: ReactNode }) {
  const [section, setSection] = useState("radar");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const readHash = () => { const id = window.location.hash.slice(1); if (sections.some(item => item.id === id)) setSection(id); };
    readHash(); window.addEventListener("hashchange", readHash);
    return () => window.removeEventListener("hashchange", readHash);
  }, []);
  const isForm = !["radar", "library"].includes(section);
  return <div className={styles.workspace}>
    <nav className={styles.nav} aria-label="Secciones del perfil">{sections.map(item => <button key={item.id} type="button" disabled={busy} aria-pressed={section === item.id} onClick={() => {
      setSection(item.id); window.history.replaceState(null, "", `#${item.id}`);
    }}><item.icon size={18} aria-hidden="true" /><span>{item.label}</span></button>)}<span className={styles.state}>{dirty ? "Cambios sin guardar" : "Perfil de empresa"}</span></nav>
    <div hidden={section !== "radar"} className={styles.radar}>{radar}</div>
    <div hidden={!isForm}><ProfileForm profile={profile} section={isForm ? section as ProfileSection : "team"} onDirtyChange={setDirty} onBusyChange={setBusy} /></div>
    <div hidden={section !== "library"}>{library}</div>
    <div hidden={section !== "company"} className={styles.connection}>{connection}</div>
  </div>;
}
