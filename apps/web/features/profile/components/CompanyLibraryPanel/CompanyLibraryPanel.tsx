"use client";

import { useEffect, useMemo, useState } from "react";
import { Archive, ChevronDown, Download, FileText, Plus, Search, ShieldCheck, Trash2, Upload } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { apiFetch } from "@/lib/api/client";
import styles from "./CompanyLibraryPanel.module.css";

type KnowledgeItem = {
  id: string;
  kind: string;
  title: string;
  description: string | null;
  valueText: string | null;
  tags: string[];
  usableForApplications: boolean;
  updatedAt: string;
};

type CompanyDocument = {
  id: string;
  title: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
  documentType: string;
  description: string | null;
  metadata?: unknown;
  tags: string[];
  validUntil: string | null;
  amount: number | null;
  entityName: string | null;
  usableForApplications: boolean;
  downloadUrl: string;
  updatedAt: string;
};

type LibraryData = {
  knowledge: KnowledgeItem[];
  documents: CompanyDocument[];
};

type Props = {
  initialLibrary: LibraryData;
};

function formatBytes(value: number) {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(value / 1024))} KB`;
}

function tagsFromInput(value: FormDataEntryValue | null) {
  return String(value ?? "")
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function matchesSearch(text: string, search: string) {
  return text.toLowerCase().includes(search.trim().toLowerCase());
}

function descriptionPreview(description: string) {
  const compact = description.replace(/\s+/g, " ").trim();
  if (compact.length <= 280) return compact;
  const boundary = compact.lastIndexOf(" ", 280);
  return `${compact.slice(0, boundary >= 180 ? boundary : 280).trimEnd()}…`;
}

function documentTextMetadata(metadata: unknown) {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return null;
  const stored = (metadata as Record<string, unknown>).document_text;
  if (!stored || typeof stored !== "object" || Array.isArray(stored)) return null;
  const value = stored as Record<string, unknown>;
  if (typeof value.full_text !== "string" || !value.full_text.trim()) return null;
  const pageCount = typeof value.page_count === "number" && Number.isInteger(value.page_count) && value.page_count > 0 ? value.page_count : null;
  const ocrPages = typeof value.ocr_pages === "number" && Number.isInteger(value.ocr_pages) && value.ocr_pages >= 0 && (pageCount == null || value.ocr_pages <= pageCount) ? value.ocr_pages : null;
  const reviewPages = Array.isArray(value.review_pages) ? new Set(value.review_pages.filter((page) => typeof page === "number" && Number.isInteger(page) && page > 0 && (pageCount == null || page <= pageCount))).size : 0;
  return { text: value.full_text, pageCount, ocrPages, reviewPages };
}

export function CompanyLibraryPanel({ initialLibrary }: Props) {
  const [library, setLibrary] = useState(initialLibrary);
  const [mode, setMode] = useState<"knowledge" | "document">("knowledge");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [openedDocumentTexts, setOpenedDocumentTexts] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    const refresh = () => { void apiFetch<{ data: LibraryData }>("/api/profile/library").then(result => setLibrary(result.data)).catch(() => setStatus("El archivo se guardó. Recarga la página para actualizar la biblioteca.")); };
    window.addEventListener("profile-library-changed", refresh);
    return () => window.removeEventListener("profile-library-changed", refresh);
  }, []);

  const filteredKnowledge = useMemo(
    () =>
      library.knowledge.filter((item) =>
        matchesSearch(
          [item.title, item.kind, item.description, item.valueText, ...item.tags].join(" "),
          search,
        ),
      ),
    [library.knowledge, search],
  );
  const filteredDocuments = useMemo(
    () =>
      library.documents.filter((document) =>
        matchesSearch(
          [
            document.title,
            document.filename,
            document.documentType,
            document.description,
            ...document.tags,
          ].join(" "),
          search,
        ),
      ),
    [library.documents, search],
  );

  async function createKnowledge(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setStatus("");
    const form = new FormData(event.currentTarget);
    try {
      const result = await apiFetch<{ data: KnowledgeItem }>("/api/profile/library/knowledge", {
        method: "POST",
        json: {
          title: String(form.get("title") ?? ""),
          kind: "dato",
          description: String(form.get("description") ?? ""),
          valueText: String(form.get("description") ?? ""),
          tags: tagsFromInput(form.get("tags")),
          usableForApplications: true,
        },
      });
      setLibrary((current) => ({
        ...current,
        knowledge: [result.data, ...current.knowledge],
      }));
      event.currentTarget.reset();
      setStatus("Dato guardado en la biblioteca.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "No se pudo guardar el dato.");
    } finally {
      setBusy(false);
    }
  }

  async function uploadDocument(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setStatus("");
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/profile/library/documents", {
        method: "POST",
        body: form,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error ?? "No se pudo subir el documento.");
      setLibrary((current) => ({
        ...current,
        documents: [payload.data, ...current.documents],
      }));
      event.currentTarget.reset();
      setStatus("Documento guardado en la biblioteca.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "No se pudo subir el documento.");
    } finally {
      setBusy(false);
    }
  }

  async function deleteKnowledge(id: string) {
    setBusy(true);
    setStatus("");
    try {
      await apiFetch(`/api/profile/library/knowledge/${id}`, { method: "DELETE" });
      setLibrary((current) => ({
        ...current,
        knowledge: current.knowledge.filter((item) => item.id !== id),
      }));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "No se pudo borrar el dato.");
    } finally {
      setBusy(false);
    }
  }

  async function deleteDocument(id: string) {
    setBusy(true);
    setStatus("");
    try {
      await apiFetch(`/api/profile/library/documents/${id}`, { method: "DELETE" });
      setLibrary((current) => ({
        ...current,
        documents: current.documents.filter((document) => document.id !== id),
      }));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "No se pudo borrar el documento.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={styles.panel}>
      <header className={styles.header}>
        <div>
          <span className={styles.kicker}><Archive size={15} aria-hidden="true" /> Biblioteca de postulación</span>
          <h2>Datos y evidencia para armar propuestas</h2>
          <p>Guarda informacion libre para que el agente la use al preparar propuestas.</p>
        </div>
        <div className={styles.search}>
          <Search size={15} aria-hidden="true" />
          <Input
            aria-label="Buscar en la biblioteca"
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Buscar por nombre o tag"
            value={search}
          />
        </div>
      </header>

      <div className={styles.summary} aria-label="Resumen de biblioteca">
        <div><strong>{library.knowledge.length}</strong><span>datos reutilizables</span></div>
        <div><strong>{library.documents.length}</strong><span>documentos</span></div>
        <div><strong>{library.knowledge.length + library.documents.length}</strong><span>fuentes para el agente</span></div>
      </div>

      <div className={styles.body}>
        <div className={styles.list}>
          <div className={styles.listHeader}>
            <h3>Contenido disponible</h3>
            <div className={styles.segmented} role="tablist" aria-label="Tipo de alta">
              <button aria-selected={mode === "knowledge"} onClick={() => setMode("knowledge")} type="button">Dato</button>
              <button aria-selected={mode === "document"} onClick={() => setMode("document")} type="button">Documento</button>
            </div>
          </div>

          <div className={styles.items}>
            {filteredKnowledge.map((item) => (
              <article className={styles.item} key={item.id}>
                <FileText size={17} aria-hidden="true" />
                <div>
                  <div className={styles.itemTitle}><strong>{item.title}</strong><Badge tone="brand">Dato</Badge></div>
                  {item.description ? <p>{item.description}</p> : null}
                  <div className={styles.tags}>{item.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                </div>
                <button aria-label={`Borrar ${item.title}`} disabled={busy} onClick={() => deleteKnowledge(item.id)} type="button"><Trash2 size={16} /></button>
              </article>
            ))}

            {filteredDocuments.map((document) => {
              const text = documentTextMetadata(document.metadata);
              return (
              <article className={styles.item} key={document.id}>
                <ShieldCheck size={17} aria-hidden="true" />
                <div>
                  <div className={styles.itemTitle}><strong>{document.title}</strong><Badge tone="sage">Documento</Badge></div>
                  <p>{[document.filename, formatBytes(document.sizeBytes)].filter(Boolean).join(" · ")}</p>
                  {document.description?.trim() ? (
                    document.description.trim().length > 280 ? (
                      <details className={styles.documentDescription}>
                        <summary>
                          <span className={styles.descriptionPreview}>{descriptionPreview(document.description)}</span>
                          <span className={styles.descriptionToggle}>Descripción completa <ChevronDown size={13} aria-hidden="true" /></span>
                        </summary>
                        <p>{document.description}</p>
                      </details>
                    ) : <p className={styles.descriptionText}>{document.description}</p>
                  ) : null}
                  {text ? (
                    <details className={styles.documentDescription} open={openedDocumentTexts.has(document.id)} onToggle={(event) => {
                      const open = event.currentTarget.open;
                      setOpenedDocumentTexts((current) => {
                        if (current.has(document.id) === open) return current;
                        const next = new Set(current);
                        if (open) next.add(document.id); else next.delete(document.id);
                        return next;
                      });
                    }}>
                      <summary>
                        <span className={styles.descriptionToggle}>Texto del documento <ChevronDown size={13} aria-hidden="true" /></span>
                      </summary>
                      {openedDocumentTexts.has(document.id) ? (
                        <>
                          <p>{[
                            text.pageCount == null ? "Páginas sin registrar" : `${new Intl.NumberFormat("es-PE").format(text.pageCount)} ${text.pageCount === 1 ? "página" : "páginas"}`,
                            text.ocrPages == null ? null : `${new Intl.NumberFormat("es-PE").format(text.ocrPages)} con OCR local`,
                          ].filter(Boolean).join(" · ")}</p>
                          {text.reviewPages > 0 ? <p className={styles.textReview}>{text.reviewPages} {text.reviewPages === 1 ? "página con texto limitado" : "páginas con texto limitado"}. Revisar el PDF.</p> : null}
                          <pre className={styles.documentTextBody} tabIndex={0} aria-label={`Texto de ${document.title}`}>{text.text}</pre>
                        </>
                      ) : null}
                    </details>
                  ) : null}
                  <div className={styles.tags}>{document.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                </div>
                <a aria-label={`Descargar ${document.title}`} href={document.downloadUrl}><Download size={16} /></a>
                <button aria-label={`Borrar ${document.title}`} disabled={busy} onClick={() => deleteDocument(document.id)} type="button"><Trash2 size={16} /></button>
              </article>
              );
            })}

            {!filteredKnowledge.length && !filteredDocuments.length ? (
              <div className={styles.empty}>
                <Archive size={20} aria-hidden="true" />
                <strong>No hay contenido con ese filtro</strong>
                <span>Agrega documentos o datos reutilizables para alimentar futuras postulaciones.</span>
              </div>
            ) : null}
          </div>
        </div>

        <aside className={styles.editor}>
          {mode === "knowledge" ? (
            <form onSubmit={createKnowledge}>
              <h3><Plus size={16} aria-hidden="true" /> Agregar dato</h3>
              <label>Nombre<Input name="title" placeholder="CCI, representante, experiencia..." required /></label>
              <label>Tags<Input name="tags" placeholder="banco, rnp, legal..." /></label>
              <label>Descripcion<textarea name="description" placeholder="Escribe aqui numeros, vigencia, contexto o condiciones." /></label>
              <Button disabled={busy} type="submit">{busy ? "Guardando..." : "Guardar dato"}</Button>
            </form>
          ) : (
            <form onSubmit={uploadDocument}>
              <h3><Upload size={16} aria-hidden="true" /> Subir documento</h3>
              <label>Archivo<input accept=".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg" name="file" required type="file" /></label>
              <input name="usableForApplications" type="hidden" value="on" />
              <label>Nombre<Input name="title" placeholder="RNP, factura, contrato..." required /></label>
              <label>Tags<Input name="tags" placeholder="experiencia, transporte, rnp..." /></label>
              <label>Descripcion<textarea name="description" placeholder="Describe uso, vigencia, monto, entidad u observaciones." /></label>
              <Button disabled={busy} type="submit">{busy ? "Subiendo..." : "Subir documento"}</Button>
            </form>
          )}
          <p className={styles.status} aria-live="polite">{status}</p>
        </aside>
      </div>
    </section>
  );
}
