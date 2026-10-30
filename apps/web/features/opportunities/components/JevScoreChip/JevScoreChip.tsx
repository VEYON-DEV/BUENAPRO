import { jevScoreChip } from "../../model/analysisPresentation";
import styles from "./JevScoreChip.module.css";

/** Compact JEV title-affinity chip. It sits beside the match score and never replaces it; no score, no chip. */
export function JevScoreChip({ score }: { score?: number | string | null }) {
  const chip = jevScoreChip(score);
  if (!chip) return null;
  return (
    <span aria-label={`${chip.label} de 100. ${chip.title}`} className={styles.chip} data-jev-score={chip.value} title={chip.title}>
      {chip.label}
    </span>
  );
}
