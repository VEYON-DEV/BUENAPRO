"use client";

import Link from "next/link";
import { LoaderCircle, RefreshCw, RotateCcw, Sparkles, TriangleAlert } from "lucide-react";
import { Button, buttonClassName } from "@/components/ui/Button";
import { useManualGrading } from "../../hooks/useManualGrading";
import type { GradingTone } from "../../model/manualGrading";
import styles from "./AnalyzeButton.module.css";

const messageTone: Partial<Record<GradingTone, string>> = {
  failed: styles.messageFailed,
  limit: styles.messageLimit,
};

/** Califica una licitación menor con el perfil de la empresa; «Re-calificar» cuando ya hay veredicto. */
export function AnalyzeButton({
  idContrato,
  profileId,
  analyzedAt,
}: {
  idContrato: string | number;
  profileId?: string | null;
  analyzedAt?: string | null;
}) {
  const { view, submit } = useManualGrading({ idContrato, profileId, regrade: Boolean(analyzedAt) });
  const busy = view.tone === "active";

  if (view.tone === "no_profile") {
    return (
      <div className={styles.root}>
        <Link href="/perfil" className={buttonClassName("secondary")}>
          {view.label}
        </Link>
      </div>
    );
  }

  const Icon = busy ? LoaderCircle : view.tone === "failed" ? RotateCcw : analyzedAt ? RefreshCw : Sparkles;
  return (
    <div className={styles.root}>
      <Button
        onClick={submit}
        disabled={view.disabled}
        aria-busy={busy}
        variant={analyzedAt || view.tone === "failed" ? "secondary" : "primary"}
      >
        <Icon size={16} aria-hidden="true" className={busy ? styles.spinner : undefined} />
        {view.label}
      </Button>
      <div className={styles.status} role="status" aria-live="polite">
        {view.message ? (
          <p className={`${styles.message} ${messageTone[view.tone] ?? ""}`}>
            {view.tone === "failed" || view.tone === "limit" ? <TriangleAlert size={14} aria-hidden="true" /> : null}
            {view.message}
          </p>
        ) : null}
        {view.usageText ? <p className={styles.usage}>{view.usageText}</p> : null}
      </div>
    </div>
  );
}
