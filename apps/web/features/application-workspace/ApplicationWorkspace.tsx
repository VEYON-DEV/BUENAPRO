"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@/features/shell/components/AppShell";
import { CopilotPanel } from "@/features/copilot";
import {
  getApplication,
  deleteAttachment,
  patchApplication,
  patchItem,
  patchRequirement,
  uploadAttachment,
} from "./api";
import type {
  ApplicationData,
  ApplicationItem,
  ApplicationRequirement,
} from "./types";
import { ProgressRail } from "./components/ProgressRail";
import { QuoteEditor } from "./components/QuoteEditor";
import { AttachmentUploader } from "./components/AttachmentUploader";
import { MarketPanel } from "./components/MarketPanel";
import styles from "./ApplicationWorkspace.module.css";

export function ApplicationWorkspace({ matchId }: { matchId: string }) {
  const [data, setData] = useState<ApplicationData | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [history, setHistory] = useState<any>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const load = useCallback(async () => {
    setError("");
    try {
      const application = await getApplication(matchId);
      setData(application);
      if (application.contractId) {
        fetch(`/api/contracts/${application.contractId}/history`)
          .then((response) => response.ok ? response.json() : null)
          .then(setHistory)
          .catch(() => setHistory(null));
      }
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "No se pudo cargar la postulación.",
      );
    }
  }, [matchId]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(
    () => () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    },
    [],
  );

  function queueDraft(patch: Partial<ApplicationData>) {
    setData((current) => (current ? { ...current, ...patch } : current));
    setSaving(true);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      try {
        await patchApplication(matchId, {
          validity_date: patch.validity,
          contact_email: patch.contactEmail,
          contact_phone: patch.contactPhone,
        });
      } catch (cause) {
        setError(
          cause instanceof Error
            ? cause.message
            : "No se guardaron los cambios.",
        );
      } finally {
        setSaving(false);
      }
    }, 550);
  }
  async function updateItem(
    item: ApplicationItem,
    patch: Partial<ApplicationItem>,
  ) {
    const computed =
      patch.unitPrice !== undefined
        ? {
            ...patch,
            total:
              patch.unitPrice == null ? null : item.quantity * patch.unitPrice,
          }
        : patch;
    setData((current) =>
      current
        ? {
            ...current,
            items: current.items.map((row) =>
              row.id === item.id ? { ...row, ...computed } : row,
            ),
          }
        : current,
    );
    setSaving(true);
    try {
      await patchItem(matchId, item.id, computed);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "No se pudo guardar el ítem.",
      );
    } finally {
      setSaving(false);
    }
  }
  async function updateRequirement(
    requirement: ApplicationRequirement,
    offered: string,
  ) {
    setData((current) =>
      current
        ? {
            ...current,
            requirements: current.requirements.map((row) =>
              row.id === requirement.id ? { ...row, offered } : row,
            ),
          }
        : current,
    );
    setSaving(true);
    try {
      await patchRequirement(matchId, requirement.id, { offered });
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "No se pudo guardar el requisito.",
      );
    } finally {
      setSaving(false);
    }
  }
  async function addAttachment(file: File) {
    setUploading(true);
    try {
      await uploadAttachment(matchId, file);
      await load();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "No se pudo subir el archivo.",
      );
    } finally {
      setUploading(false);
    }
  }
  async function removeAttachment(attachmentId: string) {
    setSaving(true);
    try {
      await deleteAttachment(matchId, attachmentId);
      setData((current) =>
        current
          ? {
              ...current,
              attachments: current.attachments.filter(
                (file) => file.id !== attachmentId,
              ),
            }
          : current,
      );
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "No se pudo eliminar el archivo.",
      );
    } finally {
      setSaving(false);
    }
  }
  const selectedTotal = useMemo(
    () =>
      data?.items
        .filter((i) => i.selected)
        .reduce(
          (sum, i) => sum + (i.total ?? (i.unitPrice ?? 0) * i.quantity),
          0,
        ) ?? 0,
    [data],
  );

  if (error && !data)
    return (
      <AppShell title="Postulación">
        <div className={styles.state} role="alert">
          <strong>No pudimos abrir esta postulación</strong>
          <p>{error}</p>
          <button onClick={() => void load()}>Reintentar</button>
          <Link href="/seguimiento">Volver a seguimiento</Link>
        </div>
      </AppShell>
    );
  if (!data)
    return (
      <AppShell title="Postulación">
        <div className={styles.loading} role="status">
          <span />
          Cargando expediente de postulación…
        </div>
      </AppShell>
    );

  return (
    <AppShell title="Postulación">
      <CopilotPanel
        contractId={data.contractId!}
        matchId={matchId}
        onApplied={() => void load()}
      />
      <div className={styles.page}>
        <Link className={styles.back} href="/seguimiento">
          ← Volver a seguimiento
        </Link>
        <header className={styles.header}>
          <div>
            <div className={styles.code}>
              {data.code}
              <span>{data.status === "draft" ? "Borrador" : data.status}</span>
            </div>
            <h1>{data.title}</h1>
            <p>{data.entity}</p>
          </div>
          <div className={styles.headerMeta}>
            {data.deadline ? (
              <div>
                <span>Cierre</span>
                <strong>
                  {new Intl.DateTimeFormat("es-PE", {
                    dateStyle: "medium",
                  }).format(new Date(data.deadline))}
                </strong>
              </div>
            ) : null}
            <div>
              <span>Total preparado</span>
              <strong>
                {new Intl.NumberFormat("es-PE", {
                  style: "currency",
                  currency: "PEN",
                }).format(selectedTotal)}
              </strong>
            </div>
          </div>
        </header>
        <nav
          className={styles.tabs}
          aria-label="Ir a un bloque de la postulación"
        >
          <a href="#oferta">Oferta</a>
          <a href="#mercado">Precio y mercado</a>
          <a href="#requisitos">
            RTM <span>{data.requirements.length}</span>
          </a>
          <a href="#archivos">
            Archivos <span>{data.attachments.length}</span>
          </a>
        </nav>
        {error ? (
          <div className={styles.inlineError} role="alert">
            {error}
            <button onClick={() => setError("")} aria-label="Cerrar aviso">
              ×
            </button>
          </div>
        ) : null}
        <div className={styles.layout}>
          <main className={styles.content}>
            <div className={styles.fastTrack}>
              <div>
                <strong>Completa todo aquí</strong>
                <p>
                  Los datos de SEACE ya fueron cargados. Solo ingresa tu oferta
                  y adjunta la propuesta.
                </p>
              </div>
              <span>
                {data.items.filter((item) => item.selected).length} ítems ·{" "}
                {data.attachments.length} archivos
              </span>
            </div>
            <MarketPanel history={history} total={selectedTotal} contractId={data.contractId} />
            <div id="oferta" className={styles.anchorSection}>
              <QuoteEditor
                data={data}
                onItem={(item, patch) => void updateItem(item, patch)}
                onDraft={queueDraft}
              />
            </div>
            <div id="requisitos" className={styles.anchorSection}>
              <Requirements
                data={data}
                onChange={(item, value) => void updateRequirement(item, value)}
              />
            </div>
            <div id="archivos" className={styles.fileStack}>
              <AttachmentUploader
                attachments={data.attachments}
                uploading={uploading}
                onUpload={(file) => void addAttachment(file)}
                onDelete={(id) => void removeAttachment(id)}
              />
              <OfficialDocuments data={data} />
            </div>
          </main>
          <ProgressRail data={data} saving={saving} />
        </div>
      </div>
    </AppShell>
  );
}

function Requirements({
  data,
  onChange,
}: {
  data: ApplicationData;
  onChange: (item: ApplicationRequirement, value: string) => void;
}) {
  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2>Requisitos técnicos mínimos</h2>
        <p>
          Responde exactamente lo ofertado. No uses información que tu empresa
          no pueda acreditar.
        </p>
      </div>
      {data.requirements.length ? (
        <div className={styles.requirements}>
          {data.requirements.map((item, index) => (
            <div key={item.id}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{item.description}</strong>
                {item.requested ? <p>Solicitado: {item.requested}</p> : null}
                <label htmlFor={`rtm-${item.id}`}>RTM ofertado</label>
                <textarea
                  id={`rtm-${item.id}`}
                  rows={3}
                  defaultValue={item.offered}
                  onBlur={(e) => onChange(item, e.target.value)}
                  placeholder="Describe tu oferta para este requisito"
                />
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className={styles.empty}>
          Esta licitación no registra RTM adicionales.
        </p>
      )}
    </section>
  );
}
function OfficialDocuments({ data }: { data: ApplicationData }) {
  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2>Formatos de la entidad</h2>
        <p>
          Archivos originales disponibles desde SEACE. Descárgalos solo si los
          necesitas.
        </p>
      </div>
      {data.documents.length ? (
        <div className={styles.documents}>
          {data.documents.map((doc) => (
            <div key={doc.id}>
              <span className={styles.fileIcon} aria-hidden="true">
                {doc.name.toLowerCase().endsWith(".pdf") ? "PDF" : "DOC"}
              </span>
              <div>
                <strong>{doc.name}</strong>
                <span>{doc.status || doc.mime || "Documento oficial"}</span>
              </div>
              {doc.downloadUrl || (data.contractId && doc.localDocumentId) ? (
                <a
                  href={
                    doc.downloadUrl ??
                    `/api/contracts/${data.contractId}/original/${doc.localDocumentId}`
                  }
                  download
                >
                  Descargar
                </a>
              ) : (
                <span>Descarga no disponible</span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className={styles.empty}>
          SEACE no registró formatos adicionales para este proceso.
        </p>
      )}
    </section>
  );
}
