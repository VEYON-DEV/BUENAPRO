"use client";

import { ExternalLink, FileCheck2, FileText, X } from "lucide-react";
import Link from "next/link";
import type { CSSProperties } from "react";
import { useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/ui/EmptyState";
import { ParticipationStatus, ParticipationTerms, ProcurementSchedule } from "@/features/procurement";
import { participationReading } from "@/lib/participationTerms";
import { keywordAffinitySummary } from "@/lib/keywordAffinity";
import { scoreTone } from "@/lib/scorePresentation";
import type { ProcurementScheduleStage } from "@/lib/procurementSchedule";
import { verdictShortLabels } from "@/lib/extraction/opportunity";
import { formatShortDateTime } from "@/lib/format/date";
import { seaceDetailRequest } from "../../api/seaceDetail";
import { JevScoreChip } from "../JevScoreChip";
import { SaveButton } from "../SaveButton";
import { Prod2RowTags } from "../Prod2RowTags";
import { analysisPresentation, provisionalAnalysisScore } from "../../model/analysisPresentation";
import { prod2DrilldownHref, rowKeywordChips, rowLineTag } from "../../model/prod2Listing";
import styles from "./Prod4OpportunityList.module.css";

export type Prod4OpportunitySummary = {
  id_procedimiento: string;
  id_convocatoria?: string;
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
  detail_fetched_at?: string | null;
  schedule_fetched_at?: string | null;
  reference_amount: number | string | null;
  currency: string | null;
  source_url: string | null;
  technology_match_reason: unknown;
  technology_segments: string[];
  items_count: number;
  documents_count: number;
  source_window_status: "current" | "exited" | "observed";
  official_schedule?: ProcurementScheduleStage[];
  actionability: string;
  fit_points: number | null;
  keyword_points?: number | null;
  fit_basis?: "profile_keywords";
  fit_score: number | null;
  fit_level: number | null;
  business_line_name: string | null;
  /** Solo mayores (PROD2): línea que calza (cuenta si keyword_points > 0), si es prioritaria y las etiquetas Nueva/Activa. */
  business_line_id?: string | null;
  business_line_favorite?: boolean;
  is_new?: boolean;
  is_active?: boolean;
  keyword_hits: Array<{ keyword: string; match?: string; points?: number }> | null;
  match_score: number | null;
  match_verdict: string | null;
  match_id?: string | null;
  match_review_required?: boolean;
  provisional_score?: number | null;
  jev_score?: number | null;
  participation_extraction?: unknown;
  is_saved?: boolean;
  has_official_detail?: boolean;
  publication_status?: string | null;
  publication_status_label?: string | null;
};

type Prod4Detail = {
  process: Prod4OpportunitySummary;
  items: Array<{ nro_item: number; cubso_code: string | null; description: string | null; quantity: number | null; unit: string | null }>;
  documents: Array<{ codigo_alfresco: string; name: string | null; document_type: string | null; extension: string | null; published_at: string | null; source_url: string | null; download_url?: string | null }>;
  schedule: Array<{ position: number; stage_key: string | null; stage_name: string | null; starts_at: string | null; ends_at: string | null }>;
  official_schedule: ProcurementScheduleStage[];
};

function objectLabel(objectType: string) {
  return objectType === "good" ? "Bienes" : objectType === "service" ? "Servicios" : "Otro";
}

function deadlineFor(row: Prod4OpportunitySummary) {
  return row.proposals_closes_at ?? row.registration_closes_at;
}

function deadlineLabel(row: Prod4OpportunitySummary) {
  const official = row.official_schedule?.find(stage => stage.kind === "proposals")
    ?? row.official_schedule?.find(stage => stage.kind === "registration");
  if (official?.endsAt && official.endPrecision === "day") {
    const date = new Intl.DateTimeFormat("es-PE", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" })
      .format(new Date(`${official.endsAt}T12:00:00Z`));
    return `${official.kind === "proposals" ? "Propuestas" : "Registro"} · ${date} · Sin hora`;
  }
  const deadline = deadlineFor(row);
  if (!deadline) return "Cronograma por confirmar";
  const prefix = row.proposals_closes_at ? "Propuestas" : "Registro";
  return `${prefix} · ${formatShortDateTime(deadline)}`;
}

function affinityLabel(level: number) {
  if (level >= 3) return "Tu rubro exacto";
  if (level >= 2) return "Muy relacionado";
  return "Rubro general";
}

function AffinityMark({ row, source }: { row: Prod4OpportunitySummary; source: "prod2" | "prod4" }) {
  const analysisInput = { matchId: row.match_id, score: row.match_score, verdict: row.match_verdict, reviewRequired: row.match_review_required };
  const analysis = analysisPresentation(analysisInput);
  const provisionalScore = provisionalAnalysisScore(analysisInput, row.provisional_score);
  if (analysis === "review" && provisionalScore != null) return <span
    aria-label={`Puntaje provisional: ${provisionalScore} de 100. Por validar.`}
    className={[styles.provisionalAffinity, styles[`score_${scoreTone(provisionalScore)}`]].join(" ")}
    data-score-tone={scoreTone(provisionalScore)} title="<30 rojo · 30–80 amarillo · >80 verde">
    <span className={styles.scoreRing} style={{ "--score": `${provisionalScore * 3.6}deg` } as CSSProperties}>{provisionalScore}</span>
    <span><strong>Provisional</strong><small>Por validar</small></span>
  </span>;
  if (analysis === "review") return <span className={styles.analysisReview}>
    <strong><FileCheck2 aria-hidden="true" size={14} />Analizado</strong>
    <span>Puntaje por validar</span>
  </span>;
  if (analysis === "evaluated" && row.match_verdict) {
    const score = Number(row.match_score);
    return (
      <span aria-label={`Evaluación de requisitos: ${score} de 100, ${verdictShortLabels[row.match_verdict] ?? row.match_verdict}`} className={[styles.finalAffinity, styles[`score_${scoreTone(score)}`]].join(" ")} data-score-tone={scoreTone(score)} title="<30 rojo · 30–80 amarillo · >80 verde">
        <span className={styles.scoreRing} style={{ "--score": `${score * 3.6}deg` } as CSSProperties}>{score}</span>
        <strong>{verdictShortLabels[row.match_verdict] ?? row.match_verdict}</strong>
      </span>
    );
  }
  if (source === "prod2") {
    const affinity = keywordAffinitySummary(row);
    if (!affinity) return <span className={styles.noAffinity}>Sin afinidad calculada</span>;
    return <span aria-label={affinity.description} className={[styles.preliminaryAffinity, styles.keywordAffinity].join(" ")} title={affinity.description}>
      <span aria-hidden="true" className={styles.fitDots}>{[1, 2, 3].map(dot => <i className={dot <= affinity.level ? styles.fitDotOn : styles.fitDotOff} key={dot} />)}</span>
      <strong>{affinity.label}</strong>
    </span>;
  }
  const level = Number(row.fit_level);
  if (!Number.isInteger(level) || level < 1 || level > 3) {
    return <span className={styles.noAffinity}>Sin afinidad calculada</span>;
  }
  const hits = row.keyword_hits?.map((hit) => hit.keyword).filter(Boolean).slice(0, 3).join(", ");
  const explanation = `Afinidad preliminar${row.fit_score != null ? ` ${row.fit_score} de 100` : ""}: ${affinityLabel(level)}${row.business_line_name ? ` en ${row.business_line_name}` : ""}${hits ? `. Coincidencias: ${hits}` : ""}. No confirma que cumplas los requisitos.`;
  return (
    <span aria-label={explanation} className={styles.preliminaryAffinity} title={explanation}>
      <span aria-hidden="true" className={styles.fitDots}>{[1, 2, 3].map((dot) => <i className={dot <= level ? styles.fitDotOn : styles.fitDotOff} key={dot} />)}</span>
      <strong>{affinityLabel(level)}</strong>
    </span>
  );
}

/** Etiquetas de la fila de mayores: Nueva, Activa, la línea que calza y hasta tres palabras (el resto, «+N»). */
function RowTags({ row }: { row: Prod4OpportunitySummary }) {
  const words = rowKeywordChips(row);
  return <Prod2RowTags extra={words.extra} isActive={row.is_active === true} isNew={row.is_new === true} keywords={words.shown} line={rowLineTag(row)} />;
}

export function Prod4OpportunityList({ rows, source = "prod4", profileId, filters }: {
  rows: Prod4OpportunitySummary[]; source?: "prod2" | "prod4"; profileId?: string;
  /** Filtros vigentes de la lista de mayores; los enlaces de línea y palabra de la vista rápida conservan algunos de ellos. */
  filters?: Record<string, string>;
}) {
  const rowId = (row: Prod4OpportunitySummary) => source === "prod2" ? row.id_convocatoria ?? row.id_procedimiento : row.id_procedimiento;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [storedDetail, setDetail] = useState<Prod4Detail | null>(null);
  const [detailStatus, setDetailStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const primary = useMemo(
    () => rows.find((row) => rowId(row) === selectedId) ?? null,
    [rows, selectedId, source],
  );
  const detail = primary && storedDetail && rowId(storedDetail.process) === rowId(primary) ? storedDetail : null;
  const previewRow = detail?.process ?? primary;
  const previewAffinity = source === "prod2" && previewRow ? keywordAffinitySummary(previewRow) : null;
  // En la vista rápida la línea y las palabras que coinciden filtran la lista (solo mayores, y solo si la línea suma puntos).
  const previewLine = source === "prod2" && previewRow ? rowLineTag(previewRow) : null;
  const previewWords = source === "prod2" && previewRow ? rowKeywordChips(previewRow, Infinity).all : [];
  const drilldownFilters = filters ?? (profileId ? { profile_id: profileId } : {});
  const previewAnalysis = previewRow ? analysisPresentation({ matchId: previewRow.match_id, score: previewRow.match_score, verdict: previewRow.match_verdict, reviewRequired: previewRow.match_review_required }) : "none";

  const firstId = rows[0] ? rowId(rows[0]) : null;
  useEffect(() => {
    if (window.matchMedia("(min-width: 1401px)").matches) {
      setSelectedId((current) => current ?? firstId);
    }
  }, [firstId]);

  useEffect(() => {
    if (selectedId == null) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape" && !window.matchMedia("(min-width: 1401px)").matches) setSelectedId(null); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [selectedId]);

  useEffect(() => {
    if (selectedId && !rows.some((row) => rowId(row) === selectedId)) {
      setSelectedId(window.matchMedia("(min-width: 1401px)").matches ? firstId : null);
    }
  }, [rows, selectedId, source, firstId]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailStatus("idle");
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailStatus("loading");
    seaceDetailRequest<Prod4Detail>(source, selectedId, profileId)
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
  }, [selectedId, source, profileId]);

  if (!rows.length) {
    return (
      <EmptyState title="No hay concursos con esos filtros" action={{ label: "Limpiar filtros", href: `/feed?source=${source}${profileId ? `&profile_id=${encodeURIComponent(profileId)}` : ""}` }}>
        Prueba con bienes y servicios juntos o cambia el término de búsqueda.
      </EmptyState>
    );
  }

  return (
    <section className={[styles.board, primary ? "" : styles.noPreview, source === "prod2" ? styles.withSave : ""].join(" ")}>
      <div className={styles.list}>
        <div className={styles.listHead}>
          <span>Procedimiento / objeto</span><span>Entidad</span><span>Subcontratación</span><span>Consorcio</span><span>Próxima fecha</span><span>Análisis / afinidad</span>
          {source === "prod2" ? <span className={styles.saveHeading}><span className={styles.srOnly}>Guardar</span></span> : null}
        </div>
        <div className={styles.rows}>
          {rows.map((row) => (
            <article className={[styles.row, rowId(row) === selectedId ? styles.active : ""].join(" ")} key={rowId(row)}>
              <button className={styles.rowMain} onClick={() => setSelectedId(rowId(row) === selectedId ? null : rowId(row))} type="button" aria-expanded={rowId(row) === selectedId}>
                <div className={styles.objectCell}>
                  <span className={styles.code} translate="no">{row.nomenclatura ?? rowId(row)}</span>
                  <strong>{row.title || row.description || "Objeto sin descripción"}</strong>
                  <span className={styles.meta}>{objectLabel(row.object_type)} · {source === "prod2" && row.has_official_detail === false ? "Solo datos del listado" : `${row.documents_count ?? 0} documentos`}</span>
                  {source === "prod2" && row.publication_status_label ? <span className={styles.meta}>{row.publication_status_label}</span> : null}
                  {source === "prod2" ? <RowTags row={row} /> : null}
                </div>
                <div className={styles.buyerCell}>
                  <span>{row.buyer_name || "Entidad no informada"}</span>
                  <span className={styles.meta}>{row.region || "Perú"}</span>
                </div>
                <div className={styles.participationCell}><ParticipationStatus label="Subcontratación" term={participationReading(row.participation_extraction).subcontratacion} /></div>
                <div className={styles.participationCell}><ParticipationStatus label="Consorcio" kind="consorcio" term={participationReading(row.participation_extraction).consorcio} /></div>
                <div className={styles.dateCell}>
                  <strong>{deadlineLabel(row)}</strong>
                  <span className={styles.meta}>{source === "prod2" ? row.has_official_detail === false ? "Ficha por consultar" : "Cronograma consultado" : row.source_window_status === "current" ? "En radar oficial" : "Fuera del radar"}</span>
                </div>
                <div className={styles.affinityCell}>
                  <AffinityMark row={row} source={source} />
                  <JevScoreChip score={row.jev_score} />
                </div>
              </button>
              {source === "prod2" ? <SaveButton code={row.nomenclatura} idContrato={rowId(row)} endpoint={`/api/prod2/opportunities/${rowId(row)}/saved`} initialSaved={Boolean(row.is_saved)} /> : null}
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
          {source === "prod2" ? <Prod2RowTags flush isActive={previewRow?.is_active === true} isNew={previewRow?.is_new === true} /> : null}
          {source === "prod2" && previewRow?.has_official_detail === false ? <p className={styles.eligibility}>Solo datos del listado. La ficha oficial, las bases y el cronograma todavía no están disponibles en BuenaPro.</p> : null}
          {source === "prod2" && previewRow?.publication_status_label ? <p className={styles.eligibility}>{previewRow.publication_status_label}. Estado conservado de la última revisión.</p> : null}
          <div className={styles.previewAffinityGroup}>
            <div className={styles.previewAffinity}><span>{previewAnalysis !== "none" ? "Análisis de requisitos" : "Afinidad con tu empresa"}</span><AffinityMark row={detail?.process ?? primary} source={source} /></div>
            {previewAnalysis === "review" ? <p className={styles.analysisContext}>El análisis está disponible en el detalle. El puntaje sigue por validar.</p> : null}
            {previewAffinity ? <div className={styles.keywordContext} title={previewAffinity.description}>
              {previewLine ? <Prod2RowTags flush keywordHref={keyword => prod2DrilldownHref(drilldownFilters, { kw: keyword })} keywords={previewWords}
                line={previewLine} lineHref={previewLine.id ? prod2DrilldownHref(drilldownFilters, { line: previewLine.id }) : undefined} />
                : <strong>{previewAffinity.lineName}</strong>}
              <span>{previewAffinity.pointLabel} de 45 puntos por palabras clave</span>
              {previewLine && previewWords.length ? null
                : <span>{previewAffinity.hits.length ? `Coincidencias: ${previewAffinity.hits.join(", ")}` : "Sin coincidencias de palabras clave"}</span>}
            </div> : null}
          </div>
          <Link className={styles.openOfficial} href={`${source === "prod2" ? "/oportunidad/prod2" : "/oportunidad/seace"}/${rowId(primary)}${source === "prod2" && profileId ? `?profile_id=${encodeURIComponent(profileId)}` : ""}`}>Ver detalle completo →</Link>
          {source === "prod2" ? <SaveButton code={primary.nomenclatura} idContrato={rowId(primary)} endpoint={`/api/prod2/opportunities/${rowId(primary)}/saved`} initialSaved={Boolean(primary.is_saved)} showLabel /> : null}
          <p className={styles.eligibility}>Revisa el cronograma y la modalidad antes de participar. La afinidad con tus líneas de negocio no confirma que puedas presentar una oferta.</p>
          <dl className={styles.facts}>
            <div><dt>Entidad</dt><dd>{primary.buyer_name || "No informada"}</dd></div>
            <div><dt>Tipo de proceso</dt><dd>{primary.procedure_type || "Por confirmar"}</dd></div>
          </dl>
          <ParticipationTerms compact reading={participationReading((detail?.process ?? primary).participation_extraction)} />
          {detailStatus === "loading" ? <p className={styles.loading} role="status">Cargando cronograma y documentos…</p> : null}
          {detailStatus === "error" ? <p className={styles.error} role="alert">No se pudo cargar la ficha. Selecciona el concurso otra vez para reintentar.</p> : null}
          {detail ? (
            <>
            <ProcurementSchedule stages={detail.official_schedule ?? []} source={source} fetchedAt={detail.process.schedule_fetched_at ?? detail.process.detail_fetched_at} compact />
            <div className={styles.detailSections}>
              {detail.items.length ? <details><summary>Ítems ({detail.items.length})</summary><ul>{detail.items.slice(0, 6).map((item, index) => <li key={`${item.nro_item}-${index}`}><strong>{item.description || `Ítem ${item.nro_item}`}</strong><span>{item.cubso_code ? `CUBSO ${item.cubso_code}` : "Sin código CUBSO"}</span></li>)}</ul></details> : null}
              {detail.documents.length ? <details open><summary>Documentos ({detail.documents.length})</summary><ul>{detail.documents.slice(0, 6).map((document, index) => <li key={`${document.codigo_alfresco}-${index}`}><FileText aria-hidden="true" size={15} /><span>{document.name || document.document_type || "Documento"}</span>{source === "prod2" ? document.download_url ? <a aria-label={`Abrir ${document.name || "documento"}`} href={document.download_url} rel="noopener noreferrer" target="_blank">Abrir PDF <ExternalLink aria-hidden="true" size={13} /></a> : <small>Consulta el portal oficial</small> : document.extension?.toLowerCase().replace(/^\./, "") === "pdf" ? <a aria-label={`Abrir ${document.name || "documento"}`} href={`/api/prod4/opportunities/${primary.id_procedimiento}/documents/${encodeURIComponent(document.codigo_alfresco)}`} rel="noopener noreferrer" target="_blank">Abrir PDF <ExternalLink aria-hidden="true" size={13} /></a> : <small>Disponible en SEACE</small>}</li>)}</ul></details> : null}
            </div>
            </>
          ) : null}
          {primary.source_url ? <a className={styles.openOfficial} href={primary.source_url} rel="noopener noreferrer" target="_blank">Abrir módulo SEACE <ExternalLink aria-hidden="true" size={16} /></a> : null}
        </aside>
      ) : null}
    </section>
  );
}
