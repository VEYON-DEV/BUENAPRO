"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ChevronDown, ChevronRight, FileText, Plus, Search, Trash2, Upload } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { uploadProfileEvidence } from "../../api/profile";
import { formatMoney, groupRecords, recordList, recordMatches, recordObject, recordText, updateRecord, type ProfileRecord } from "../../model/records";
import styles from "./ProfileRecords.module.css";

export type RecordField = { key: string; label: string; type?: "number" | "date" | "textarea" | "select"; required?: boolean; placeholder?: string; group?: string; max?: number; options?: { value: string; label: string }[] };
type Props = {
  title: string; description: string; addLabel: string; primary: string;
  fields: RecordField[]; records: ProfileRecord[]; onChange: (records: ProfileRecord[]) => void;
  /** Field used to group the list (e.g. "rubro"); spelling variants share a group. */
  groupBy?: string;
  /** Field shown as the row title when it differs from the primary field (e.g. specialty inside a rubro group). */
  titleKey?: string;
  /** Fields shown under the title in the list. */
  summary?: string[];
  professional?: boolean; money?: boolean; disabled?: boolean; onUploading?: (busy: boolean) => void;
  /** Compact accordion used for short lists nested inside another editor. */
  variant?: "master" | "inline";
};

const OBJECT_LABELS: Record<string, string> = { bienes: "Bienes", servicios: "Servicios", obras: "Obras", consultoria_obras: "Consultoría de obras" };
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
const NARROW = 760;
const SEARCH_FROM = 6;

function initials(text: string) {
  return text.split(/\s+/).filter(Boolean).slice(0, 2).map(word => word[0]).join("").toUpperCase() || "··";
}

export function ProfileRecords(props: Props) {
  return props.variant === "inline" ? <InlineRecords {...props} /> : <MasterRecords {...props} />;
}

function useEvidenceUpload(records: ProfileRecord[], primary: string, change: (index: number, key: string, value: unknown) => void, onUploading?: (busy: boolean) => void) {
  const [uploading, setUploading] = useState<number | null>(null);
  const [error, setError] = useState("");
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
  return { uploading, error, upload };
}

function describe(value: Record<string, any>, { professional, summary, fields }: Pick<Props, "professional" | "summary" | "fields">) {
  if (professional) return [value.nombre, value.grado || value.carrera, value.experiencia_anios != null && value.experiencia_anios !== "" ? `${value.experiencia_anios} años` : null];
  const keys = summary ?? fields.slice(1, 3).map(field => field.key);
  return keys.map(key => key === "tipo_objeto" ? OBJECT_LABELS[value[key]] : value[key]);
}

function FieldGrid({ fields, record, primary, onChange }: { fields: RecordField[]; record: ProfileRecord; primary: string; onChange: (key: string, value: unknown) => void }) {
  return <div className={styles.fields}>{fields.map(field => <label key={field.key} className={field.type === "textarea" ? styles.wide : undefined}>
    <span>{field.label}{field.required ? <span className={styles.required} aria-hidden="true"> *</span> : null}</span>
    {field.type === "textarea"
      ? <textarea name={field.key} autoComplete="off" required={field.required} value={recordText(record, field.key, primary)} placeholder={field.placeholder} onChange={event => onChange(field.key, event.target.value)} rows={3} />
      : field.type === "select"
        ? <Select aria-label={field.label} name={field.key} required={field.required} value={recordText(record, field.key, primary)} onChange={event => onChange(field.key, event.target.value)}><option value="">Sin indicar</option>{field.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</Select>
        : <Input name={field.key} autoComplete="off" required={field.required} type={field.type ?? "text"} min={field.type === "number" ? 0 : undefined} max={field.max} step={field.type === "number" ? "any" : undefined} placeholder={field.placeholder} value={recordText(record, field.key, primary)} onChange={event => onChange(field.key, field.type === "number" ? (event.target.value === "" ? null : Number(event.target.value)) : event.target.value)} />}
  </label>)}</div>;
}

function Evidence({ label, documents, uploading, onUpload }: { label: string; documents: unknown; uploading: boolean; onUpload: (file?: File) => void }) {
  const items = recordList(documents);
  return <div className={styles.evidence}>
    <div className={styles.evidenceHead}>
      <div><strong>Archivos de respaldo</strong><p>CV, constancias, contratos o certificados. Se guardan en tu biblioteca; no se leen automáticamente.</p></div>
      <label className={styles.upload}><Upload size={16} aria-hidden="true" />{uploading ? "Subiendo…" : "Subir archivo"}<input name="file" type="file" aria-label={`Subir respaldo de ${label}`} accept=".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg" onChange={event => { onUpload(event.target.files?.[0]); event.target.value = ""; }} /></label>
    </div>
    {items.length ? <ul className={styles.documents}>{items.map((item, n) => {
      const doc = recordObject(item, "title");
      return <li key={n}><a href={doc.id ? `/api/profile/library/documents/${encodeURIComponent(doc.id)}` : undefined} target="_blank" rel="noopener noreferrer"><FileText size={15} aria-hidden="true" /><span>{doc.title || doc.filename || "Documento"}</span></a></li>;
    })}</ul> : <p className={styles.noDocs}>Sin archivos vinculados.</p>}
  </div>;
}

function MasterRecords({ title, description, addLabel, primary, fields, records, onChange, groupBy, titleKey, summary, professional, money, disabled, onUploading }: Props) {
  const root = useRef<HTMLElement>(null);
  const editorTitle = useRef<HTMLHeadingElement>(null);
  const pendingFocus = useRef<"editor" | "list" | null>(null);
  const [narrow, setNarrow] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [mobileEditing, setMobileEditing] = useState(false);
  const [query, setQuery] = useState("");
  const [step, setStep] = useState(0);
  const change = (index: number, key: string, value: unknown) => onChange(records.map((record, position) => position === index ? updateRecord(record, primary, key, value) : record));
  const { uploading, error, upload } = useEvidenceUpload(records, primary, change, onUploading);
  const locked = disabled || uploading !== null;

  useEffect(() => {
    const node = root.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width < NARROW));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  // Wide layouts always show an editor: default to the first record in list order.
  const firstInOrder = groupRecords(records, primary, groupBy)[0]?.items[0]?.index ?? null;
  useEffect(() => {
    if (selected !== null && selected >= records.length) setSelected(records.length ? records.length - 1 : null);
    else if (!narrow && selected === null && firstInOrder !== null) setSelected(firstInOrder);
  }, [narrow, records.length, selected, firstInOrder]);

  useEffect(() => {
    if (!narrow || !pendingFocus.current) return;
    const destination = pendingFocus.current;
    const target = destination === "editor" ? editorTitle.current : root.current?.querySelector<HTMLButtonElement>(`[data-profile-record-index="${selected}"]`);
    if (!target) return;
    pendingFocus.current = null;
    target.focus({ preventScroll: true });
    const smooth = destination === "editor" && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    root.current?.scrollIntoView({ block: "start", behavior: smooth ? "smooth" : "auto" });
  }, [narrow, mobileEditing, selected]);

  function select(index: number) {
    if (narrow) pendingFocus.current = "editor";
    setSelected(index); setStep(0); setMobileEditing(true);
  }
  function backToList() {
    if (narrow) pendingFocus.current = "list";
    setMobileEditing(false);
  }
  function add() { setQuery(""); onChange([...records, { [primary]: "" }]); select(records.length); }
  function remove(index: number, label: string) {
    if (!window.confirm(`¿Quitar «${label}» del perfil? Los archivos seguirán en la biblioteca. Guarda los cambios para confirmar.`)) return;
    onChange(records.filter((_, position) => position !== index));
    if (narrow && records.length > 1) pendingFocus.current = "list";
    setSelected(records.length > 1 ? Math.min(index, records.length - 2) : null);
    setMobileEditing(false);
  }

  const visible = records.filter(record => recordMatches(record, primary, query));
  const groups = groupRecords(records, primary, groupBy).map(group => ({ ...group, items: group.items.filter(item => visible.includes(item.record)) })).filter(group => group.items.length);
  const current = selected !== null ? records[selected] : undefined;
  const value = current !== undefined ? recordObject(current, primary) : {};
  const heading = (record: ProfileRecord) => (titleKey && recordText(record, titleKey, primary)) || recordText(record, primary, primary);
  const label = current !== undefined ? heading(current) || "Nuevo registro" : "";
  const sections = [...new Set(fields.map(field => field.group ?? ""))];
  const steps = [
    ...(sections.length > 1 ? sections : ["Datos"]),
    ...(professional ? [`Experiencia · ${recordList(value.experience).length}`, `Certificados · ${recordList(value.certifications).length}`] : []),
    `Archivos · ${recordList(value.documents).length}`,
  ];
  const activeStep = Math.min(step, steps.length - 1);
  const showEditor = !narrow || mobileEditing;
  const showList = !narrow || !mobileEditing;

  return <section ref={root} className={styles.section}>
    <header className={styles.header}>
      <div><h3>{title} <span>{records.length}</span></h3><p>{description}</p></div>
      {records.length > 0 && <Button disabled={locked} variant="secondary" type="button" onClick={add}><Plus size={16} aria-hidden="true" />{addLabel}</Button>}
    </header>
    {!records.length ? <div className={styles.empty}><p>Todavía no hay registros.</p><Button disabled={locked} type="button" onClick={add}><Plus size={16} aria-hidden="true" />{addLabel}</Button></div> :
    <div className={[styles.master, narrow ? styles.narrow : "", records.length < SEARCH_FROM ? styles.short : ""].filter(Boolean).join(" ")}>
      {showList && <div className={styles.listPane}>
        {records.length >= SEARCH_FROM && <label className={styles.search}><Search size={16} aria-hidden="true" /><span className={styles.srOnly}>Buscar en {title.toLowerCase()}</span><input type="search" value={query} placeholder="Buscar…" onChange={event => setQuery(event.target.value)} /></label>}
        <div className={styles.list} role="list" aria-label={title}>
          {!groups.length && <p className={styles.noResults}>Sin resultados para «{query}».</p>}
          {groups.map(group => <div key={group.key || "all"} role="presentation">
            {groupBy && <p className={styles.groupHead}><span>{group.label}</span><span>{group.items.length}</span></p>}
            {group.items.map(({ record, index }) => {
              const item = recordObject(record, primary);
              const name = heading(record);
              const meta = describe(item, { professional, summary, fields }).filter(Boolean).join(" · ");
              const amount = money ? formatMoney(item.monto, item.moneda) : null;
              const active = index === selected && showEditor;
              return <div role="listitem" key={index}>
                <button type="button" data-profile-record-index={index} className={`${styles.row} ${active ? styles.active : ""}`} aria-current={active ? "true" : undefined} onClick={() => select(index)} disabled={locked}>
                  {professional ? <span className={styles.avatar} aria-hidden="true">{initials(name)}</span> : null}
                  <span className={styles.rowText}>
                    <strong className={name ? undefined : styles.untitled}>{name || "Sin título · completa este registro"}</strong>
                    {meta && <span>{meta}</span>}
                    {amount && narrow && <span className={styles.amountInline}>{amount}</span>}
                  </span>
                  {amount && !narrow && <span className={styles.amount}>{amount}</span>}
                  {narrow && <ChevronRight size={16} aria-hidden="true" className={styles.chevron} />}
                </button>
              </div>;
            })}
          </div>)}
        </div>
      </div>}
      {showEditor && current !== undefined && selected !== null && <div className={styles.editorPane} aria-label={`Editar ${label}`} role="region">
        <div className={styles.editorHead}>
          {narrow && <Button variant="ghost" type="button" onClick={backToList}><ArrowLeft size={16} aria-hidden="true" />Volver a la lista</Button>}
          <div className={styles.editorTitle}>
            <h4 ref={editorTitle} tabIndex={-1}>{label}</h4>
            {money && formatMoney(value.monto, value.moneda) && <p>{formatMoney(value.monto, value.moneda)}</p>}
          </div>
          <Button variant="ghost" type="button" aria-label={`Eliminar ${label}`} title="Eliminar registro" disabled={locked} onClick={() => remove(selected, label)}><Trash2 size={16} aria-hidden="true" /></Button>
        </div>
        <div className={styles.steps} role="group" aria-label="Secciones del registro">
          {steps.map((name, index) => <button key={name} type="button" aria-pressed={index === activeStep} disabled={locked} onClick={() => setStep(index)}>{name.split(" y ")[0]}</button>)}
        </div>
        <fieldset disabled={locked} className={styles.editor}>
          {activeStep < (sections.length > 1 ? sections.length : 1) && <FieldGrid fields={sections.length > 1 ? fields.filter(field => (field.group ?? "") === sections[activeStep]) : fields} record={current} primary={primary} onChange={(key, next) => change(selected, key, next)} />}
          {professional && steps[activeStep].startsWith("Experiencia") && <ProfileRecords variant="inline" title="Experiencia del profesional" description="Proyectos y funciones que puede acreditar." addLabel="Agregar experiencia" primary="role" fields={experience} records={recordList(value.experience)} onChange={items => change(selected, "experience", items)} disabled={disabled} onUploading={onUploading} />}
          {professional && steps[activeStep].startsWith("Certificados") && <ProfileRecords variant="inline" title="Certificados y capacitaciones" description="Cada certificado con su propio respaldo." addLabel="Agregar certificado" primary="nombre" fields={certificates} records={recordList(value.certifications)} onChange={items => change(selected, "certifications", items)} disabled={disabled} onUploading={onUploading} />}
          {activeStep === steps.length - 1 && <Evidence label={label} documents={value.documents} uploading={uploading === selected} onUpload={file => void upload(selected, file)} />}
        </fieldset>
        {activeStep < steps.length - 1 && <div className={styles.stepNav}><Button variant="secondary" type="button" onClick={() => setStep(activeStep + 1)}>Siguiente: {steps[activeStep + 1].split(" · ")[0].toLowerCase()}<ChevronRight size={16} aria-hidden="true" /></Button></div>}
      </div>}
    </div>}
    <p role="status" aria-live="polite" className={styles.error}>{error}</p>
  </section>;
}

function InlineRecords({ title, description, addLabel, primary, fields, records, onChange, disabled, onUploading }: Props) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const change = (index: number, key: string, value: unknown) => onChange(records.map((record, position) => position === index ? updateRecord(record, primary, key, value) : record));
  const { uploading, error, upload } = useEvidenceUpload(records, primary, change, onUploading);
  const locked = disabled || uploading !== null;
  return <section className={styles.inline}>
    <header className={styles.inlineHead}>
      <div><h5>{title}</h5><p>{description}</p></div>
      <Button disabled={locked} variant="secondary" type="button" onClick={() => { onChange([...records, { [primary]: "" }]); setExpanded(records.length); }}><Plus size={16} aria-hidden="true" />{addLabel}</Button>
    </header>
    {!records.length && <p className={styles.noDocs}>Sin registros todavía.</p>}
    {records.map((record, index) => {
      const value = recordObject(record, primary);
      const label = recordText(record, primary, primary) || "Nuevo registro";
      const opened = expanded === index;
      const meta = fields.slice(1, 3).map(field => value[field.key]).filter(Boolean).join(" · ");
      return <article className={styles.inlineItem} key={index}>
        <div className={styles.inlineRow}>
          <button type="button" className={styles.inlineToggle} aria-expanded={opened} disabled={locked} onClick={() => setExpanded(opened ? null : index)}>
            <span className={styles.rowText}><strong>{label}</strong>{meta && <span>{meta}</span>}</span>
            <ChevronDown size={16} aria-hidden="true" className={opened ? styles.rotated : undefined} />
          </button>
          <Button variant="ghost" type="button" aria-label={`Eliminar ${label}`} disabled={locked} onClick={() => {
            if (!window.confirm(`¿Quitar «${label}»? Guarda los cambios para confirmar.`)) return;
            onChange(records.filter((_, position) => position !== index)); setExpanded(null);
          }}><Trash2 size={16} aria-hidden="true" /></Button>
        </div>
        {opened && <fieldset disabled={locked} className={styles.inlineEditor}>
          <FieldGrid fields={fields} record={record} primary={primary} onChange={(key, next) => change(index, key, next)} />
          <Evidence label={label} documents={value.documents} uploading={uploading === index} onUpload={file => void upload(index, file)} />
        </fieldset>}
      </article>;
    })}
    <p role="status" aria-live="polite" className={styles.error}>{error}</p>
  </section>;
}
