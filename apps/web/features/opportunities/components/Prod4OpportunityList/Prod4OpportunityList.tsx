"use client";

import { ExternalLink, FileText, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/ui/EmptyState";
import { formatDateTime, formatShortDateTime } from "@/lib/format/date";
import styles from "./Prod4OpportunityList.module.css";

export type Prod4OpportunitySummary = {
  id_procedimiento: string;
  opportunity_id: string;
  nomenclatura: string | null;
  title: string | null;
  description: string | null;
  object_type: "good" | "service" | string;
  procedure_type: string | null;
  buyer_name: string | null;
  region: string | null;
  published_at: string | null;
  registration_closes_at: string | null;
  proposals_start_at: string | null;
  proposals_closes_at: string | null;
  reference_amount: number | string | null;
  currency: string | null;
  source_url: string | null;
  technology_match_reason: unknown;
  technology_segments: string[];
  items_count: number;
  documents_count: number;
  source_window_status: "current" | "exited";
  actionability: string;
};

type Prod4Detail = {
  process: Prod4OpportunitySummary;
  items: Array<{ nro_item: number; cubso_code: string | null; description: string | null; quantity: number | null; unit: string | null }>;
  documents: Array<{ codigo_alfresco: string; name: string | null; document_type: string | null; extension: string | null; published_at: string | null; source_url: string | null }>;
  schedule: Array<{ position: number; stage_key: string | null; stage_name: string | null; starts_at: string | null; ends_at: string | null }>;
};

function objectLabel(objectType: string) {
  return objectType === "good" ? "Bienes" : objectType === "service" ? "Servicios" : "Otro";
}

function deadlineFor(row: Prod4OpportunitySummary) {
  return row.proposals_closes_at ?? row.registration_closes_at;
}

function deadlineLabel(row: Prod4OpportunitySummary) {
  const deadline = deadlineFor(row);
  if (!deadline) return "Cronograma por confirmar";
  const prefix = row.proposals_closes_at ? "Propuestas" : "Registro";
  return `${prefix} · ${formatShortDateTime(deadline)}`;
}

export function Prod4OpportunityList({ rows }: { rows: Prod4OpportunitySummary[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Prod4Detail | null>(null);
  const [detailStatus, setDetailStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const primary = useMemo(
    () => rows.find((row) => row.id_procedimiento === selectedId) ?? null,
    [rows, selectedId],
  );

  const firstId = rows[0]?.id_procedimiento ?? null;
  useEffect(() => {
    if (window.matchMedia("(min-width: 1401px)").matches) {
      setSelectedId((current) => current ?? firstId);
    }
  }, [firstId]);

  useEffect(() => {
    if (selectedId && !rows.some((row) => row.id_procedimiento === selectedId)) {
      setSelectedId(window.matchMedia("(min-width: 1401px)").matches ? rows[0]?.id_procedimiento ?? null : null);
    }
  }, [rows, selectedId]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailStatus("idle");
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailStatus("loading");
    fetch(`/api/prod4/opportunities/${encodeURIComponent(selectedId)}`)
      .then((response) => {
        if (!response.ok) throw new Error("No se pudo cargar la ficha");
        return response.json();
      })
      .then((payload: Prod4Detail) => {
        if (cancelled) return;
        setDetail(payload);
        setDetailStatus("done");
      })
      .catch(() => {
        if (cancelled) return;
        setDetail(null);
        setDetailStatus("error");
      });
    return () => { cancelled = true; };
  }, [selectedId]);

  if (!rows.length) {
    return (
      <EmptyState title="No hay concursos de tecnología con esos filtros" action={{ label: "Limpiar filtros", href: "/feed?source=prod4" }}>
        Prueba con bienes y servicios juntos o cambia el término de búsqueda.
      </EmptyState>
    );
  }

  return (
    <section className={[styles.board, primary ? "" : styles.noPreview].join(" ")}>
      <div className={styles.list}>
        <div className={styles.listHead}>
          <span>Procedimiento</span><span>Objeto</span><span>Entidad</span><span>Tipo</span><span>Próxima fecha</span><span>Ficha</span>
        </div>
        <div className={styles.rows}>
          {rows.map((row) => (
            <article className={[styles.row, row.id_procedimiento === selectedId ? styles.active : ""].join(" ")} key={row.id_procedimiento}>
              <button className={styles.rowMain} onClick={() => setSelectedId(row.id_procedimiento === selectedId ? null : row.id_procedimiento)} type="button">
                <div className={styles.codeCell}>
                  <strong>{row.nomenclatura ?? row.id_procedimiento}</strong>
                  <span className={styles.sourceTag}>Concurso SEACE</span>
                </div>
                <div className={styles.objectCell}>
                  <strong>{row.title || row.description || "Objeto sin descripción"}</strong>
                  <span className={styles.meta}>{row.technology_segments?.length ? `CUBSO ${row.technology_segments.join(", ")}` : "Tecnología"}</span>
                </div>
                <div className={styles.buyerCell}>
                  <span>{row.buyer_name || "Entidad no informada"}</span>
                  <span className={styles.meta}>{row.region || "Perú"}</span>
                </div>
                <span className={styles.objectTag}>{objectLabel(row.object_type)}</span>
                <div className={styles.dateCell}>
                  <strong>{deadlineLabel(row)}</strong>
                  <span className={styles.meta}>{row.source_window_status === "current" ? "En radar oficial" : "Fuera del radar"}</span>
                </div>
                <span className={styles.docCount}><FileText aria-hidden="true" size={15} /> {row.documents_count ?? 0}</span>
              </button>
            </article>
          ))}
        </div>
      </div>

      {primary ? (
        <aside aria-label="Vista rápida del concurso" className={styles.preview}>
          <div className={styles.previewTop}>
            <strong>Vista rápida</strong>
            <button aria-label="Cerrar vista rápida" className={styles.closeButton} onClick={() => setSelectedId(null)} type="button"><X aria-hidden="true" size={17} /></button>
          </div>
          <span className={styles.previewCode}>{primary.nomenclatura ?? primary.id_procedimiento}</span>
          <h2>{primary.title || primary.description || "Objeto sin descripción"}</h2>
          <div className={styles.pills}><span>{objectLabel(primary.object_type)}</span><span>Procedimiento de selección</span></div>
          <p className={styles.eligibility}>La presencia en PROD4 no confirma que aún puedas presentar una oferta. Revisa el cronograma y los requisitos oficiales.</p>
          <dl className={styles.facts}>
            <div><dt>Entidad</dt><dd>{primary.buyer_name || "No informada"}</dd></div>
            <div><dt>Registro hasta</dt><dd>{formatDateTime(primary.registration_closes_at)}</dd></div>
            <div><dt>Propuestas hasta</dt><dd>{formatDateTime(primary.proposals_closes_at)}</dd></div>
            <div><dt>Tipo de proceso</dt><dd>{primary.procedure_type || "Por confirmar"}</dd></div>
          </dl>
          {detailStatus === "loading" ? <p className={styles.loading} role="status">Cargando cronograma y documentos…</p> : null}
          {detailStatus === "error" ? <p className={styles.error} role="alert">No se pudo cargar la ficha. Selecciona el concurso otra vez para reintentar.</p> : null}
          {detail ? (
            <div className={styles.detailSections}>
              {detail.schedule.length ? <details><summary>Cronograma ({detail.schedule.length})</summary><ol>{detail.schedule.slice(0, 6).map((stage, index) => <li key={`${stage.position}-${index}`}><strong>{stage.stage_name || "Etapa"}</strong><span>{formatDateTime(stage.starts_at)} — {formatDateTime(stage.ends_at)}</span></li>)}</ol></details> : null}
              {detail.items.length ? <details><summary>Ítems ({detail.items.length})</summary><ul>{detail.items.slice(0, 6).map((item, index) => <li key={`${item.nro_item}-${index}`}><strong>{item.description || `Ítem ${item.nro_item}`}</strong><span>{item.cubso_code ? `CUBSO ${item.cubso_code}` : "Sin código CUBSO"}</span></li>)}</ul></details> : null}
              {detail.documents.length ? <details open><summary>Documentos ({detail.documents.length})</summary><ul>{detail.documents.slice(0, 6).map((document, index) => <li key={`${document.codigo_alfresco}-${index}`}><FileText aria-hidden="true" size={15} /><span>{document.name || document.document_type || "Documento"}</span>{document.extension?.toLowerCase().replace(/^\./, "") === "pdf" ? <a aria-label={`Abrir ${document.name || "documento"}`} href={`/api/prod4/opportunities/${primary.id_procedimiento}/documents/${encodeURIComponent(document.codigo_alfresco)}`} rel="noopener noreferrer" target="_blank">Abrir PDF <ExternalLink aria-hidden="true" size={13} /></a> : <small>Disponible en SEACE</small>}</li>)}</ul></details> : null}
            </div>
          ) : null}
          {primary.source_url ? <a className={styles.openOfficial} href={primary.source_url} rel="noopener noreferrer" target="_blank">Abrir módulo SEACE <ExternalLink aria-hidden="true" size={16} /></a> : null}
        </aside>
      ) : null}
    </section>
  );
}
