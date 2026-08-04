import type { CSSProperties, ReactNode } from "react";
import { CheckCircle2, Clock3, Target, WalletCards } from "lucide-react";
import styles from "./DecisionOverview.module.css";

type DecisionOverviewProps = {
  analyzed: boolean;
  score?: number | string | null;
  verdict: string;
  verdictTone?: "green" | "amber" | "red" | "neutral";
  fit: string;
  deadline: string;
  deadlineDate: string;
  requirementsTotal: number;
  missingCount: number;
  marketMedian?: string | null;
  marketRange?: string | null;
  actions: ReactNode;
};

export function DecisionOverview({
  analyzed,
  score,
  verdict,
  verdictTone = "neutral",
  fit,
  deadline,
  deadlineDate,
  requirementsTotal,
  missingCount,
  marketMedian,
  marketRange,
  actions,
}: DecisionOverviewProps) {
  const numericScore = analyzed ? Math.max(0, Math.min(100, Number(score) || 0)) : 0;
  const completed = Math.max(requirementsTotal - missingCount, 0);
  const completion = requirementsTotal ? Math.round((completed / requirementsTotal) * 100) : 0;

  return (
    <section className={styles.overview} id="resumen" aria-label="Resumen de decisión">
      <div className={styles.scoreBlock}>
        <span
          className={[styles.scoreGauge, styles[`tone_${verdictTone}`]].join(" ")}
          style={{ "--score-angle": `${numericScore * 3.6}deg` } as CSSProperties}
        >
          {analyzed ? <strong>{numericScore}</strong> : <i><span /><span /><span /></i>}
        </span>
        <div>
          <span className={styles.label}>Decisión</span>
          <strong className={styles.verdict}>{verdict}</strong>
          <small>{analyzed ? `${fit} · evaluación IA` : `${fit} · evaluación pendiente`}</small>
        </div>
      </div>

      <div className={styles.metric}>
        <span className={styles.icon}><Clock3 aria-hidden="true" /></span>
        <div><span className={styles.label}>Cierre</span><strong>{deadline}</strong><small>{deadlineDate}</small></div>
      </div>

      <div className={styles.metric}>
        <span className={styles.icon}><CheckCircle2 aria-hidden="true" /></span>
        <div className={styles.progressCopy}>
          <span className={styles.label}>Requisitos cubiertos</span>
          <strong>{requirementsTotal ? `${completed} de ${requirementsTotal}` : "Por analizar"}</strong>
          <span className={styles.progressTrack}><i style={{ width: `${completion}%` }} /></span>
          <small>{missingCount ? `${missingCount} requieren acción` : "Sin brechas detectadas"}</small>
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
