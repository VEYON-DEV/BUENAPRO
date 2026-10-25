"use client";

import Link from "next/link";
import { ChevronDown, FileCheck2 } from "lucide-react";
import type { CSSProperties } from "react";
import { useEffect, useMemo, useState } from "react";
import { AppIcon } from "@/components/ui/AppIcon";
import { EmptyState } from "@/components/ui/EmptyState";
import { ParticipationStatus, ParticipationTerms } from "@/features/procurement";
import { participationReading } from "@/lib/participationTerms";
import { formatDeadline, formatDateTime, formatShortDateTime } from "@/lib/format/date";
import { formatMoney } from "@/lib/format/money";
import { scoreTone } from "@/lib/scorePresentation";
import { FIT_EXACTO, FIT_RELACIONADO, cotizacionStatus, fitLabel, fitScore, fitShortLabel, opportunityFacts, plazoLabel, verdictShortLabels } from "@/lib/extraction/opportunity";
import { jevAffinity } from "@/lib/jevAffinity";
import { JevAffinityMark } from "../JevAffinityMark";
import { SaveButton } from "../SaveButton";
import { analysisPresentation, provisionalAnalysisScore } from "../../model/analysisPresentation";
import styles from "./OpportunityList.module.css";

function deadlineUrgency(value?: string | null) {
  if (!value) return "calm";
  const diffHours = (new Date(value).getTime() - Date.now()) / 36e5;
  if (diffHours < 0) return "closed";
  if (diffHours <= 24) return "urgent";
  if (diffHours <= 72) return "soon";
  return "calm";
}

function FitMark({ hits, compact = false }: { hits?: number | null; compact?: boolean }) {
  const score = fitScore(hits);
  const points = Number(hits ?? 0);
  const level = score >= FIT_EXACTO ? 3 : score >= FIT_RELACIONADO ? 2 : 1;
  const tone = level === 3 ? styles.fitHigh : level === 2 ? styles.fitMid : styles.fitBase;
  const explanation = `Afinidad preliminar: ${points} puntos por rubro, keywords y capacidad económica. El número final aparece después del análisis IA.`;
  return (
    <span
      aria-label={explanation}
      className={[styles.fitMark, tone].join(" ")}
      title={explanation}
    >
      <span className={styles.fitDots} aria-hidden="true">
        {[1, 2, 3].map((dot) => (
          <i key={dot} className={dot <= level ? styles.dotOn : styles.dotOff} />
        ))}
      </span>
      <strong>{compact ? fitShortLabel(hits) : fitLabel(hits)}</strong>
    </span>
  );
}

function ScoreMark({ verdict, score }: { verdict?: string | null; score?: number | string | null }) {
  if (!verdict || score == null || (typeof score === "string" && score.trim() === "")) {
    return <span className={styles.noMatch}>—</span>;
  }
  const numericScore = Number(score);
  if (!Number.isFinite(numericScore) || numericScore < 0 || numericScore > 100) return <span className={styles.noMatch}>—</span>;
  const tone = scoreTone(numericScore);
  return (
    <span
      className={[styles.scoreMark, styles[`score_${tone}`]].join(" ")}
      data-score-tone={tone}
      title="<30 rojo · 30–80 amarillo · >80 verde"
      style={{ "--score": `${numericScore * 3.6}deg` } as CSSProperties}
    >
      <span className={styles.scoreRing}>{numericScore}</span>
      <strong>{verdictShortLabels[verdict] ?? verdict}</strong>
    </span>
  );
}

function PendingAnalysisMark({ provisionalScore }: { provisionalScore: number | null }) {
  if (provisionalScore != null) return <span
    aria-label={`Puntaje provisional: ${provisionalScore} de 100. Por validar.`}
    className={[styles.provisionalScore, styles[`score_${scoreTone(provisionalScore)}`]].join(" ")}
    data-score-tone={scoreTone(provisionalScore)}
    title="<30 rojo · 30–80 amarillo · >80 verde"
    style={{ "--score": `${provisionalScore * 3.6}deg` } as CSSProperties}>
    <span className={styles.scoreRing}>{provisionalScore}</span>
    <span><strong>Provisional</strong><small>Por validar</small></span>
  </span>;
  return <span className={styles.analysisReview}>
    <strong><FileCheck2 aria-hidden="true" size={14} />Analizado</strong>
    <span>Puntaje por validar</span>
  </span>;
}

function FactRow({ icon, label, children }: { icon: Parameters<typeof AppIcon>[0]["name"]; label: string; children: React.ReactNode }) {
  return (
    <div className={styles.factRow}>
      <span className={styles.factIcon}>
        <AppIcon name={icon} />
      </span>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function OpportunityList({ rows, profileId }: { rows: any[]; profileId?: string }) {
  const [selectedId, setSelectedId] = useState<string | number | null>(rows[0]?.id_contrato ?? null);
  // Beside the list (>1500px) the inspector is always shown; below that it is an overlay revealed only after the user picks a row.
  const [userOpened, setUserOpened] = useState(false);
  const [wideViewport, setWideViewport] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1501px)");
    const sync = () => setWideViewport(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  useEffect(() => {
    if (selectedId == null) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !wideViewport) {
        setSelectedId(null);
        setUserOpened(false);
      }
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [selectedId, wideViewport]);
  const [storedDetail, setDetail] = useState<any | null>(null);
  const [detailStatus, setDetailStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const primary = useMemo(
    () => (selectedId == null ? null : rows.find((row) => row.id_contrato === selectedId) ?? null),
    [rows, selectedId],
  );
  const detail = primary && storedDetail?.contract
    && (!profileId || storedDetail.contract.profile_id === profileId)
    && String(storedDetail.contract.id_contrato) === String(primary.id_contrato) ? storedDetail : null;

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailStatus("idle");
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailStatus("loading");
    fetch(`/api/contracts/${selectedId}${profileId ? `?profile_id=${encodeURIComponent(profileId)}` : ""}`)
      .then((response) => {
        if (!response.ok) throw new Error("No se pudo cargar metadata");
        return response.json();
      })
      .then((payload) => {
        if (cancelled) return;
        setDetail(payload);
        setDetailStatus("done");
      })
      .catch(() => {
        if (cancelled) return;
        setDetail(null);
        setDetailStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, profileId]);

  if (!rows.length) {
    return (
      <EmptyState title="No hay oportunidades con esos filtros" action={{ label: "Limpiar filtros", href: "/feed" }}>
        Ajusta la busqueda o revisa si el worker ya termino de procesar nuevos contratos.
      </EmptyState>
    );
  }

  const facts = opportunityFacts(detail?.contract ?? primary);
  const facets = Array.isArray(detail?.facets) ? detail.facets : [];
  const documents = Array.isArray(detail?.documents) ? detail.documents : [];
  const ubicacion = primary
    ? [primary.departamento, primary.provincia].filter(Boolean).join(", ") || "Perú"
    : "";
  const previewAnalysis = primary ? analysisPresentation({ matchId: primary.match_id, score: primary.score, verdict: primary.verdict, reviewRequired: primary.match_review_required }) : "none";
  const previewJev = primary ? jevAffinity(primary) : null;
  const previewProvisionalScore = primary ? provisionalAnalysisScore({ matchId: primary.match_id, score: primary.score, verdict: primary.verdict, reviewRequired: primary.match_review_required }, primary.provisional_score) : null;

  return (
    <section className={[styles.board, !primary ? styles.noPreview : ""].join(" ")}>
      <div className={styles.list}>
        <div className={styles.listHead}>
          <span>Código / objeto</span>
          <span>Entidad</span>
          <span>Subcontratación</span>
          <span>Consorcio</span>
          <span>Cierre</span>
          <span>Análisis / afinidad</span>
          <span className={styles.saveHeading} aria-label="Guardar" />
        </div>
        <div className={styles.rows}>
          {rows.map((row) => {
            const urgency = deadlineUrgency(row.fec_fin_cotizacion);
            const analysis = analysisPresentation({ matchId: row.match_id, score: row.score, verdict: row.verdict, reviewRequired: row.match_review_required });
            const provisionalScore = provisionalAnalysisScore({ matchId: row.match_id, score: row.score, verdict: row.verdict, reviewRequired: row.match_review_required }, row.provisional_score);
            const jev = jevAffinity(row);
            return (
              <article
                className={[styles.row, row.id_contrato === primary?.id_contrato ? styles.active : ""].join(" ")}
                key={row.id_contrato}
              >
                <button
                  className={styles.rowMain}
                  type="button"
                  aria-expanded={row.id_contrato === selectedId && (wideViewport || userOpened)}
                  onClick={() => { const closing = row.id_contrato === selectedId && userOpened; setSelectedId(closing ? null : row.id_contrato); setUserOpened(!closing); }}
                >
                  <div className={styles.codeCell}>
                    <strong className={styles.code}>{row.codigo}</strong>
                    <span className={styles.title}>{row.descripcion}</span>
                    {(() => {
                      const cotizacion = cotizacionStatus(row);
                      return (
                        <span className={[styles.meta, styles[`cot_${cotizacion.key}`]].join(" ")}>
                          {cotizacion.label}
                        </span>
                      );
                    })()}
                  </div>
                  <div className={styles.entity}>
                    <span>{row.entidad_nombre ?? "Entidad no informada"}</span>
                    <span className={styles.meta}>
                      {[row.departamento, row.provincia].filter(Boolean).join(", ") || "Perú"}
                    </span>
                  </div>
                  <div className={styles.participationCell}><ParticipationStatus label="Subcontratación" term={participationReading(row.participation_extraction).subcontratacion} /></div>
                  <div className={styles.participationCell}><ParticipationStatus label="Consorcio" kind="consorcio" term={participationReading(row.participation_extraction).consorcio} /></div>
                  <div className={styles.deadlineCell}>
                    <strong className={styles[urgency]}>{formatDeadline(row.fec_fin_cotizacion)}</strong>
                    <span className={styles.meta}>{formatShortDateTime(row.fec_fin_cotizacion)}</span>
                  </div>
                  <div className={[styles.matchCell, jev ? styles.matchStack : ""].join(" ")}>
                    {jev ? <JevAffinityMark affinity={jev} /> : null}
                    {analysis === "review" ? <PendingAnalysisMark provisionalScore={provisionalScore} /> : analysis === "evaluated" ? (
                      <ScoreMark verdict={row.verdict} score={row.score} />
                    ) : (
                      <FitMark compact hits={row.fit_points} />
                    )}
                  </div>
                </button>
                <SaveButton
                  code={row.codigo}
                  idContrato={row.id_contrato}
                  initialSaved={Boolean(row.is_saved)}
                />
              </article>
            );
          })}
        </div>
      </div>

      {primary ? (
        <aside className={[styles.preview, userOpened ? styles.opened : ""].join(" ")} aria-label="Vista rápida de la oportunidad">
          <div className={styles.previewTop}>
            <strong>Vista rápida</strong>
            <button
              className={styles.closeButton}
              type="button"
              aria-label="Cerrar vista rápida"
              onClick={() => { setSelectedId(null); setUserOpened(false); }}
            >
              <AppIcon name="close" />
            </button>
          </div>

          <div className={styles.previewIdentity}><span className={styles.previewCode}>{primary.codigo}</span><SaveButton code={primary.codigo} idContrato={primary.id_contrato} initialSaved={Boolean(primary.is_saved)} /></div>
          <h2 className={styles.previewTitle}>{facts.descripcionCorta || primary.descripcion}</h2>

          {(() => { const cotizacion = cotizacionStatus(primary); return <span className={[styles.stateChip, styles[`cotdot_${cotizacion.key}`]].join(" ")}><i />{cotizacion.label}</span>; })()}

          <div className={styles.previewScore}>
            {previewAnalysis === "review" ? (
              <><PendingAnalysisMark provisionalScore={previewProvisionalScore} /><span>Revisa el análisis en el detalle</span></>
            ) : previewAnalysis === "evaluated" ? (
              <>
                <ScoreMark verdict={primary.verdict} score={primary.score} />
                <span>Evaluado con tu perfil</span>
              </>
            ) : (
              <>
                <FitMark hits={primary.fit_points} />
                <span>Afinidad preliminar · análisis pendiente</span>
              </>
            )}
          </div>

          {previewJev ? <JevAffinityMark affinity={previewJev} variant="inspector" /> : null}

          <div className={styles.previewActions}>
            <Link className={styles.openLink} href={`/oportunidad/${primary.id_contrato}${profileId ? `?profile_id=${encodeURIComponent(profileId)}` : ""}`}>
              Ver detalle completo
              <AppIcon name="arrow" />
            </Link>
            <SaveButton
              code={primary.codigo}
              idContrato={primary.id_contrato}
              initialSaved={Boolean(primary.is_saved)}
              showLabel
            />
          </div>

          <dl className={styles.facts}>
            <FactRow icon="building" label="Entidad">{primary.entidad_nombre ?? "No informada"}</FactRow>
            <FactRow icon="clock" label="Cierre">{formatDateTime(primary.fec_fin_cotizacion)}</FactRow>
            <FactRow icon="tag" label="Exp. económica">
              {primary.econ_exigido != null
                ? `${formatMoney(Number(primary.econ_exigido))} exigidos`
                : "No exigida en el TDR"}
            </FactRow>
          </dl>
          <ParticipationTerms compact reading={participationReading((detail?.contract ?? primary).participation_extraction)} />

          <details className={styles.moreInfo}>
            <summary>
              <span>Más información</span>
              <ChevronDown aria-hidden="true" size={17} />
            </summary>
            <dl className={styles.secondaryFacts}>
              <FactRow icon="pin" label="Ubicación">{ubicacion}</FactRow>
              <FactRow icon="doc" label="Pago">
                {[facts.tipoPago ?? "Por definir", plazoLabel(facts.plazoDias)].filter(Boolean).join(" · ")}
              </FactRow>
              {facts.roles.length ? (
                <FactRow icon="profile" label="Personal clave">
                  {facts.roles.slice(0, 2).join(", ")}
                  {facts.roles.length > 2 ? ` +${facts.roles.length - 2}` : ""}
                </FactRow>
              ) : null}
              {facts.penalidadTopePct != null ? (
                <FactRow icon="alert" label="Penalidad tope">{facts.penalidadTopePct}% del contrato</FactRow>
              ) : null}
            </dl>
            <p className={styles.previewFoot}>
              {detailStatus === "loading"
                ? "Cargando análisis del TDR…"
                : detailStatus === "error"
                  ? "No se pudo cargar el análisis completo."
                  : `${facets.length} requisitos extraídos · ${documents.length} ${documents.length === 1 ? "documento" : "documentos"}`}
            </p>
          </details>
        </aside>
      ) : null}
    </section>
  );
}
