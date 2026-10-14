import type { CSSProperties, ReactNode } from "react";
import { CheckCircle2, CircleHelp, Clock3, Target, WalletCards } from "lucide-react";
import { scoreTone } from "@/lib/scorePresentation";
import styles from "./DecisionOverview.module.css";

type DecisionOverviewProps = {
  analyzed: boolean;
  score?: number | string | null;
  provisionalScore?: number | string | null;
  scoreUnderReview?: boolean;
  verdict: string;
  verdictTone?: "green" | "amber" | "red" | "neutral";
  fit: string;
  deadline: string;
  deadlineDate: string;
  requirementsTotal: number;
  missingCount: number;
  coveredCount?: number;
  requirementsLabel?: string;
  marketMedian?: string | null;
  marketRange?: string | null;
  actions: ReactNode;
};

export function DecisionOverview({
  analyzed,
  score,
  provisionalScore,
  scoreUnderReview = false,
  verdict,
  fit,
  deadline,
  deadlineDate,
  requirementsTotal,
  missingCount,
  coveredCount,
  requirementsLabel = "Requisitos cubiertos",
  marketMedian,
  marketRange,
  actions,
}: DecisionOverviewProps) {
  const candidate = scoreUnderReview ? provisionalScore : score;
  const candidatePresent = typeof candidate === "number" || (typeof candidate === "string" && candidate.trim() !== "");
  const scoreKnown = analyzed && candidatePresent && Number.isFinite(Number(candidate))
    && Number(candidate) >= 0 && Number(candidate) <= 100;
  const numericScore = scoreKnown ? Math.max(0, Math.min(100, Number(candidate))) : 0;
  const tone = scoreKnown ? scoreTone(numericScore) : "gris";
  const gaugeTone = { verde: "green", ambar: "amber", rojo: "red", gris: "neutral" }[tone];
  const coverageKnown = analyzed && coveredCount != null && requirementsTotal > 0;
  const completed = Math.min(requirementsTotal, Math.max(coveredCount ?? 0, 0));
  const completion = requirementsTotal ? Math.round((completed / requirementsTotal) * 100) : 0;

  return (
    <section className={styles.overview} id="resumen" aria-label="Resumen de decisión">
      <div className={styles.scoreBlock}>
        {scoreUnderReview && !scoreKnown ? <span className={styles.scorePending}><CircleHelp aria-hidden="true" size={32} /></span> : <span
          aria-label={scoreKnown ? `${numericScore} de 100${scoreUnderReview ? ", puntaje provisional" : ""}` : "Puntaje no disponible"}
          data-score-tone={tone}
          title={scoreKnown ? "Menos de 30: rojo · De 30 a 80: amarillo · Más de 80: verde" : undefined}
          className={[styles.scoreGauge, styles[`tone_${gaugeTone}`]].join(" ")}
          style={{ "--score-angle": `${numericScore * 3.6}deg` } as CSSProperties}
        >
          {scoreKnown ? <><strong>{numericScore}</strong><small className={styles.scoreScale}>/100</small></> : <i><span /><span /><span /></i>}
        </span>}
        <div className={scoreUnderReview ? styles.reviewCopy : undefined}>
          <span className={styles.label}>Decisión</span>
          <strong className={styles.verdict}>{scoreUnderReview ? scoreKnown ? "Puntaje provisional" : "Puntaje por validar" : verdict}</strong>
          <small>{scoreUnderReview ? "Estimación IA · pendiente de validación" : analyzed ? `${fit} · evaluación IA` : `${fit} · evaluación pendiente`}</small>
        </div>
      </div>

      <div className={styles.metric}>
        <span className={styles.icon}><Clock3 aria-hidden="true" /></span>
        <div><span className={styles.label}>Cierre</span><strong>{deadline}</strong><small>{deadlineDate}</small></div>
      </div>

      <div className={styles.metric}>
        <span className={styles.icon}><CheckCircle2 aria-hidden="true" /></span>
        <div className={styles.progressCopy}>
          <span className={styles.label}>{requirementsLabel}</span>
          <strong>{coverageKnown ? `${completed} de ${requirementsTotal}` : analyzed && !requirementsTotal ? "Sin requisitos evaluados" : "Por analizar"}</strong>
          <span className={styles.progressTrack} role={coverageKnown ? "progressbar" : undefined} aria-label={coverageKnown ? requirementsLabel : undefined} aria-valuemin={coverageKnown ? 0 : undefined} aria-valuemax={coverageKnown ? requirementsTotal : undefined} aria-valuenow={coverageKnown ? completed : undefined}><i style={{ width: `${coverageKnown ? completion : 0}%` }} /></span>
          <small>{coverageKnown ? (missingCount ? `${missingCount} requieren atención` : "Sin brechas detectadas") : analyzed && !requirementsTotal ? "Revisa los requisitos de la oportunidad" : "Pendiente de evaluación"}</small>
        </div>
      </div>

      <div className={styles.metric}>
        <span className={styles.icon}><WalletCards aria-hidden="true" /></span>
        <div><span className={styles.label}>Referencia de mercado</span><strong>{marketMedian || "Sin precio comparable"}</strong><small>{marketRange || "Revisa el histórico disponible"}</small></div>
      </div>

      <div className={styles.actions}>
        <Target aria-hidden="true" />
        {actions}
      </div>
    </section>
  );
}
