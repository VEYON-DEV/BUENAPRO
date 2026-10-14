import { Badge } from "@/components/ui/Badge";
import { participationHint, participationLabel, type ParticipationTerm } from "@/lib/participationTerms";
import styles from "./ParticipationStatus.module.css";

export function ParticipationStatus({ term, label, kind = "subcontratacion" }: { term: ParticipationTerm; label?: string; kind?: "subcontratacion" | "consorcio" }) {
  const tone = term.needsReview || term.status === "conditional" ? "amber"
    : term.status === "permitted" ? "green" : term.status === "prohibited" ? "red" : "neutral";
  return <span className={styles.status} title={participationHint(term, kind)}>
    {label ? <span className={styles.label} data-participation-label>{label}</span> : null}
    <Badge tone={tone}>{participationLabel(term, kind)}</Badge>
    {term.status !== "pending" && term.status !== "not_identified"
      ? <small>{term.needsReview ? "Por revisar" : "Según lectura"}</small> : null}
  </span>;
}
