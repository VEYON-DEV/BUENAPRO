"use client";
import { useEffect, useRef, useState } from "react";
import { Save } from "lucide-react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { ProfileRecords, type RecordField } from "../ProfileRecords";
import { recordList, validateProfileRecords } from "../../model/records";
import { saveProfile } from "../../api/profile";
import styles from "./ProfileForm.module.css";

export type ProfileSection = "team" | "experience" | "resources" | "company";
const titles: Record<ProfileSection, [string, string]> = {
  team: ["Equipo profesional", "Formación, experiencia y respaldo de las personas con las que puedes postular."],
  experience: ["Experiencia acreditable", "Registra los contratos y montos que tu empresa puede sustentar."],
  resources: ["Recursos y certificaciones", "Equipamiento, certificados y seguros disponibles para cumplir los requisitos."],
  company: ["Datos de empresa", "Identidad e inscripciones que usa la evaluación de requisitos."],
};
const teamFields: RecordField[] = [
  { key: "role", label: "Rol o especialidad", required: true, placeholder: "Ej. Jefe de proyecto" },
  { key: "nombre", label: "Nombre del profesional", placeholder: "Opcional; no incluyas DNI" },
  { key: "grado", label: "Grado o título académico" }, { key: "carrera", label: "Carrera o formación" },
  { key: "experiencia_anios", label: "Años de experiencia", type: "number" },
  { key: "colegiatura", label: "Colegiatura y habilitación" },
  { key: "especialidad", label: "Experiencia específica", type: "textarea" },
];
const contracts: RecordField[] = [
  { key: "objeto", label: "Objeto del contrato", required: true }, { key: "entidad", label: "Cliente o entidad" },
  { key: "monto", label: "Monto acreditable (S/)", type: "number" }, { key: "anio", label: "Año", type: "number" },
  { key: "conformidad", label: "Conformidad o constancia" }, { key: "descripcion", label: "Alcance y observaciones", type: "textarea" },
];
const certificates: RecordField[] = [
  { key: "nombre", label: "Certificado o seguro", required: true }, { key: "entidad", label: "Entidad emisora" },
  { key: "valid_until", label: "Vigencia hasta", type: "date" }, { key: "descripcion", label: "Alcance y observaciones", type: "textarea" },
];

export function ProfileForm({ profile, section = "team", onDirtyChange, onBusyChange }: { profile: any | null; section?: ProfileSection; onDirtyChange?: (value: boolean) => void; onBusyChange?: (value: boolean) => void }) {
  const router = useRouter();
  const [draft, setDraft] = useState<any>(() => ({ ...profile, identity_json: { ...profile?.identity_json }, econ_experience_json: { ...profile?.econ_experience_json } }));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState(false);
  const [baseline, setBaseline] = useState(profile);
  const feedback = useRef<HTMLParagraphElement>(null);
  useEffect(() => { if (error) feedback.current?.focus(); }, [error, status]);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange?.(busy || uploading); }, [busy, uploading, onBusyChange]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    const guard = (event: MouseEvent) => {
      const anchor = (event.target as HTMLElement)?.closest("a");
      if (!anchor || anchor.target === "_blank" || !anchor.href || anchor.hasAttribute("download")) return;
      const destination = new URL(anchor.href);
      if (destination.origin === window.location.origin && destination.pathname === window.location.pathname) return;
      if (!window.confirm("Tienes cambios sin guardar. ¿Salir del perfil y descartarlos?")) { event.preventDefault(); event.stopImmediatePropagation(); }
    };
    document.addEventListener("click", guard, true);
    return () => { window.removeEventListener("beforeunload", warn); document.removeEventListener("click", guard, true); };
  }, [dirty]);
  function update(key: string, value: unknown) { setDraft((current: any) => ({ ...current, [key]: value })); setDirty(true); setStatus(""); }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const invalid = !/^\d{11}$/.test(draft.ruc ?? "") || !String(draft.razon_social ?? "").trim() ? "Completa el RUC de 11 dígitos y la razón social en Empresa." : Object.values(draft.econ_experience_json ?? {}).some(value => typeof value === "number" && (!Number.isFinite(value) || value < 0)) ? "La experiencia económica no puede ser negativa. Revísala en Experiencia." : [["team_json", "role"], ["experience_json", "objeto"], ["certifications_json", "nombre"], ["equipment_json", "nombre"], ["hireable_roles_json", "role"]].map(([key, primary]) => validateProfileRecords(draft[key], primary, baseline?.[key])).find(Boolean);
    if (invalid) { setError(true); setStatus(invalid); return; }
    setBusy(true); setStatus(""); setError(false);
    try {
      const { data } = await saveProfile({
        ruc: draft.ruc, razon_social: draft.razon_social,
        identity_json: draft.identity_json ?? {}, finance_json: draft.finance_json ?? {},
        econ_experience_json: draft.econ_experience_json ?? {}, team_json: draft.team_json ?? [],
        experience_json: draft.experience_json ?? [], hireable_roles_json: draft.hireable_roles_json ?? [],
        equipment_json: draft.equipment_json ?? [], certifications_json: draft.certifications_json ?? [],
      });
      setDraft(data); setBaseline(data); setDirty(false); setStatus("Perfil guardado. El radar recalculará las coincidencias; las evaluaciones documentales se actualizan al reevaluar.");
      router.refresh();
    } catch (err) { setError(true); setStatus(err instanceof Error ? err.message : "No se pudo guardar. Tus cambios siguen disponibles para reintentar."); }
    finally { setBusy(false); }
  }
  const records = (key: string, config: Omit<React.ComponentProps<typeof ProfileRecords>, "records" | "onChange" | "disabled" | "onUploading">) =>
    <ProfileRecords {...config} records={recordList(draft[key])} onChange={value => update(key, value)} disabled={busy || uploading} onUploading={setUploading} />;
  return <form onSubmit={submit} className={styles.form}>
    <header className={styles.header}><div><h2>{titles[section][0]}</h2><p>{titles[section][1]}</p></div>
      {dirty && <Button type="submit" disabled={busy || uploading}><Save size={16} />{busy ? "Guardando…" : "Guardar cambios"}</Button>}
    </header>
    <fieldset disabled={busy || uploading} className={styles.body}>
      {section === "team" && <>
        {records("team_json", { title: "Profesionales disponibles", description: "Añade una ficha por profesional. Despliega su rol para completar o editar los datos.", addLabel: "Agregar profesional", primary: "role", fields: teamFields, professional: true })}
        <details className={styles.disclosure}><summary>Perfiles que puedes incorporar <span>{recordList(draft.hireable_roles_json).length} registrados</span></summary>
          {records("hireable_roles_json", { title: "Perfiles contratables", description: "Se distinguen de tu equipo disponible; no acreditan cumplimiento por sí solos.", addLabel: "Agregar perfil", primary: "role", fields: [{ key: "role", label: "Rol contratable", required: true }, { key: "descripcion", label: "Disponibilidad y condiciones", type: "textarea" }] })}
        </details>
      </>}
      {section === "experience" && <>
        <div className={styles.fields}>{["servicios", "bienes"].map(kind => <label key={kind}>Experiencia acreditable en {kind} (S/)<Input type="number" min="0" step="any" value={draft.econ_experience_json?.[kind] ?? ""} onChange={event => update("econ_experience_json", { ...draft.econ_experience_json, [kind]: event.target.value === "" ? null : Number(event.target.value) })} /></label>)}</div>
        <p className={styles.helper}>Registra solo montos que puedas respaldar. Agregar contratos no suma ni verifica automáticamente este monto.</p>
        {records("experience_json", { title: "Contratos previos", description: "Separa cada contrato y adjunta su conformidad o constancia.", addLabel: "Agregar contrato", primary: "objeto", fields: contracts })}
      </>}
      {section === "resources" && <>
        {records("certifications_json", { title: "Certificaciones y seguros de empresa", description: "Las capacitaciones personales se registran dentro de cada profesional.", addLabel: "Agregar certificación", primary: "nombre", fields: certificates })}
        {records("equipment_json", { title: "Equipamiento disponible", description: "Registra cada recurso y sus características, sin listas separadas por comas.", addLabel: "Agregar equipo", primary: "nombre", fields: [{ key: "nombre", label: "Equipo o recurso", required: true }, { key: "cantidad", label: "Cantidad", type: "number" }, { key: "descripcion", label: "Características y disponibilidad", type: "textarea" }] })}
      </>}
      {section === "company" && <div className={styles.fields}>
        <label>RUC<Input required value={draft.ruc ?? ""} onChange={event => update("ruc", event.target.value)} readOnly={Boolean(profile?.ruc)} inputMode="numeric" /></label>
        <label>Razón social<Input required value={draft.razon_social ?? ""} onChange={event => update("razon_social", event.target.value)} autoComplete="organization" /></label>
        <label className={styles.wide}>CCI<Input value={draft.identity_json?.cci ?? ""} onChange={event => update("identity_json", { ...draft.identity_json, cci: event.target.value })} /></label>
        <div className={styles.wide}><strong className={styles.label}>Inscripciones RNP</strong><div className={styles.checks}>{["bienes", "servicios", "obras", "consultoría de obras"].map(kind => <label key={kind}><input type="checkbox" checked={(draft.identity_json?.rnp ?? []).includes(kind)} onChange={event => {
          const current = Array.isArray(draft.identity_json?.rnp) ? draft.identity_json.rnp : [];
          update("identity_json", { ...draft.identity_json, rnp: event.target.checked ? [...current, kind] : current.filter((item: string) => item !== kind) });
        }} />{kind}</label>)}</div></div>
      </div>}
    </fieldset>
    <footer className={styles.footer}><p ref={feedback} tabIndex={-1} className={error ? styles.error : styles.feedback} role="status" aria-live="polite">{status || (uploading ? "Subiendo archivo a tu biblioteca…" : dirty ? "Tienes cambios sin guardar. Puedes cambiar de sección sin perderlos." : "Los datos declarados alimentan la evaluación; no sustituyen una verificación de los documentos.")}</p>
      {dirty && <Button type="submit" disabled={busy || uploading}>{busy ? "Guardando…" : "Guardar cambios"}</Button>}
    </footer>
  </form>;
}
