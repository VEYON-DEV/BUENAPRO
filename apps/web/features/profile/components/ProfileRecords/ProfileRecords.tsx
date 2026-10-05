"use client";

import { useState } from "react";
import { ChevronDown, FileText, Plus, Trash2, Upload } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { uploadProfileEvidence } from "../../api/profile";
import { recordList, recordObject, recordText, updateRecord, type ProfileRecord } from "../../model/records";
import styles from "./ProfileRecords.module.css";

export type RecordField = { key: string; label: string; type?: "number" | "date" | "textarea" | "select"; required?: boolean; placeholder?: string; group?: string; max?: number; options?: { value: string; label: string }[] };
type Props = {
  title: string; description: string; addLabel: string; primary: string;
  fields: RecordField[]; records: ProfileRecord[]; onChange: (records: ProfileRecord[]) => void;
  professional?: boolean; disabled?: boolean; onUploading?: (busy: boolean) => void;
};

const certificates: RecordField[] = [
  { key: "nombre", label: "Certificación o capacitación", required: true },
  { key: "entidad", label: "Institución emisora" },
  { key: "fecha", label: "Fecha de emisión", type: "date" },
  { key: "valid_until", label: "Vigencia hasta", type: "date" },
  { key: "horas", label: "Horas de capacitación", type: "number" },
];
const experience: RecordField[] = [
  { key: "role", label: "Cargo o función", required: true },
  { key: "entidad", label: "Empresa o entidad" },
  { key: "fecha_inicio", label: "Desde", type: "date" },
  { key: "fecha_fin", label: "Hasta", type: "date" },
  { key: "descripcion", label: "Proyecto y responsabilidades", type: "textarea" },
];

export function ProfileRecords({ title, description, addLabel, primary, fields, records, onChange, professional, disabled, onUploading }: Props) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [uploading, setUploading] = useState<number | null>(null);
  const [error, setError] = useState("");
  const groups = [...new Set(fields.map(field => field.group ?? ""))];
  function change(index: number, key: string, value: unknown) {
    onChange(records.map((record, position) => position === index ? updateRecord(record, primary, key, value) : record));
  }
  async function upload(index: number, file?: File) {
    if (!file) return;
    if (!file.size || file.size > 10 * 1024 * 1024) { setError("El archivo debe pesar entre 1 byte y 10 MB."); return; }
    setUploading(index); onUploading?.(true); setError("");
    try {
      const document = await uploadProfileEvidence(file, recordText(records[index], primary, primary));
      change(index, "documents", [...recordList(recordObject(records[index], primary).documents), document]);
    } catch (err) { setError(err instanceof Error ? err.message : "No se pudo subir el archivo."); }
    finally { setUploading(null); onUploading?.(false); }
  }
  return <section className={styles.section}>
    <header className={styles.header}><div><h3>{title} <span>{records.length}</span></h3><p>{description}</p></div>
      <Button disabled={disabled || uploading !== null} variant="secondary" type="button" onClick={() => { onChange([...records, { [primary]: "" }]); setExpanded(records.length); }}><Plus size={16} aria-hidden="true" />{addLabel}</Button>
    </header>
    {!records.length && <p className={styles.empty}>Todavía no hay registros. Usa «{addLabel}» para completar esta sección.</p>}
    <div className={styles.list}>{records.map((record, index) => {
      const value = recordObject(record, primary);
      const label = recordText(record, primary, primary) || "Nuevo registro";
      const opened = expanded === index;
      const detail = professional ? [value.nombre, value.grado, value.carrera, value.experiencia_anios != null ? `${value.experiencia_anios} años de experiencia` : null] : fields.slice(1, 4).map(field => value[field.key]);
      return <article className={styles.record} key={index}>
        <div className={styles.row}>
          <button className={styles.toggle} type="button" disabled={disabled || uploading !== null} aria-expanded={opened} onClick={() => setExpanded(opened ? null : index)}>
            <span className={styles.marker} aria-hidden="true">{professional ? label.slice(0, 2).toUpperCase() : <FileText size={18} />}</span>
            <span className={styles.overview}><strong>{label}</strong><span>{detail.filter(Boolean).join(" · ") || "Completa los datos y agrega evidencia de respaldo"}</span></span>
            <ChevronDown className={opened ? styles.rotated : undefined} size={18} aria-hidden="true" />
          </button>
          <Button variant="ghost" type="button" aria-label={`Eliminar ${label}`} disabled={disabled || uploading !== null} onClick={() => {
            if (!window.confirm(`¿Quitar «${label}» del perfil? Los archivos seguirán en la biblioteca. Guarda los cambios para confirmar.`)) return;
            onChange(records.filter((_, position) => position !== index)); setExpanded(null);
          }}><Trash2 size={16} aria-hidden="true" /></Button>
        </div>
        {opened && <fieldset disabled={disabled || uploading !== null} className={styles.editor}>
          {groups.map(group => <fieldset key={group} className={styles.group}>
            {group && <legend>{group}</legend>}
            <div className={styles.fields}>{fields.filter(field => (field.group ?? "") === group).map(field => <label key={field.key} className={field.type === "textarea" ? styles.wide : undefined}>{field.label}{field.required ? " *" : ""}
              {field.type === "textarea" ? <textarea name={field.key} autoComplete="off" required={field.required} value={recordText(record, field.key, primary)} placeholder={field.placeholder} onChange={event => change(index, field.key, event.target.value)} rows={3} /> : field.type === "select" ? <Select aria-label={field.label} name={field.key} required={field.required} value={recordText(record, field.key, primary)} onChange={event => change(index, field.key, event.target.value)}><option value="">Sin indicar</option>{field.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</Select> : <Input name={field.key} autoComplete="off" required={field.required} type={field.type ?? "text"} min={field.type === "number" ? 0 : undefined} max={field.max} step={field.type === "number" ? "any" : undefined} placeholder={field.placeholder} value={recordText(record, field.key, primary)} onChange={event => change(index, field.key, field.type === "number" ? (event.target.value === "" ? null : Number(event.target.value)) : event.target.value)} />}
            </label>)}</div>
          </fieldset>)}
          {professional && <>
            <ProfileRecords title="Experiencia del profesional" description="Registra proyectos y funciones que puede acreditar." addLabel="Agregar experiencia" primary="role" fields={experience} records={recordList(value.experience)} onChange={items => change(index, "experience", items)} disabled={disabled} onUploading={onUploading} />
            <ProfileRecords title="Certificados y capacitaciones" description="Añade cada certificado y su respaldo por separado." addLabel="Agregar certificado" primary="nombre" fields={certificates} records={recordList(value.certifications)} onChange={items => change(index, "certifications", items)} disabled={disabled} onUploading={onUploading} />
          </>}
          <div className={styles.evidence}>
            <div><strong>Archivos de respaldo</strong><p>CV, constancias o certificados. Se guardan en tu biblioteca; no se extraen automáticamente.</p></div>
            <label className={styles.upload}><Upload size={16} aria-hidden="true" />{uploading === index ? "Subiendo…" : "Subir archivo"}<input name="file" type="file" aria-label={`Subir respaldo de ${label}`} accept=".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg" onChange={event => { void upload(index, event.target.files?.[0]); event.target.value = ""; }} /></label>
          </div>
          <div className={styles.documents}>{recordList(value.documents).map((item, n) => {
            const doc = recordObject(item, "title");
            return <a key={n} href={doc.id ? `/api/profile/library/documents/${encodeURIComponent(doc.id)}` : undefined} target="_blank" rel="noopener noreferrer"><FileText size={15} aria-hidden="true" />{doc.title || doc.filename || "Documento"}</a>;
          })}</div>
        </fieldset>}
      </article>;
    })}</div>
    <p role="status" aria-live="polite" className={styles.error}>{error}</p>
  </section>;
}
