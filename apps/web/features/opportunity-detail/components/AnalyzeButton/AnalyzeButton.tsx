"use client";

import Link from "next/link";
import { LoaderCircle, RefreshCw, RotateCcw, Sparkles, TriangleAlert } from "lucide-react";
import { Button, buttonClassName } from "@/components/ui/Button";
import { useManualGrading } from "../../hooks/useManualGrading";
import type { GradingSource, GradingTone } from "../../model/manualGrading";
import styles from "./AnalyzeButton.module.css";

const messageTone: Partial<Record<GradingTone, string>> = {
  failed: styles.messageFailed,
  limit: styles.messageLimit,
  blocked: styles.messageLimit,
};

/**
 * Califica una licitación (menores) o convocatoria (mayores) con el perfil de la empresa.
 * Con veredicto previo ofrece «Re-calificar» si `canRegrade`; mayores no lo permite y no muestra nada.
 */
export function AnalyzeButton({
  source = "contracts",
  id,
  profileId,
  analyzedAt,
  canRegrade = true,
}: {
  /** Menores (contracts) por defecto; mayores usa prod2. */
  source?: GradingSource;
  id: string | number;
  profileId?: string | null;
  analyzedAt?: string | null;
  canRegrade?: boolean;
}) {
  const graded = Boolean(analyzedAt);
  const { view, submit } = useManualGrading({ source, id, profileId, regrade: graded && canRegrade });
  const busy = view.tone === "active";

  if (graded && !canRegrade) return null;

  if (view.tone === "no_profile") {
    return (
      <div className={styles.root}>
        <Link href="/perfil" className={buttonClassName("secondary")}>
          {view.label}
        </Link>
      </div>
    );
  }

  const Icon = busy ? LoaderCircle : view.tone === "failed" ? RotateCcw : graded ? RefreshCw : Sparkles;
  return (
    <div className={styles.root}>
      <Button
        onClick={submit}
        disabled={view.disabled}
        aria-busy={busy}
        variant={graded || view.tone === "failed" || view.tone === "blocked" ? "secondary" : "primary"}
      >
        <Icon size={16} aria-hidden="true" className={busy ? styles.spinner : undefined} />
        {view.label}
      </Button>
      <div className={styles.status} role="status" aria-live="polite">
        {view.message ? (
          <p className={`${styles.message} ${messageTone[view.tone] ?? ""}`}>
            {view.tone === "failed" || view.tone === "limit" || view.tone === "blocked" ? <TriangleAlert size={14} aria-hidden="true" /> : null}
            {view.message}
          </p>
        ) : null}
        {view.usageText ? <p className={styles.usage}>{view.usageText}</p> : null}
      </div>
    </div>
  );
}
