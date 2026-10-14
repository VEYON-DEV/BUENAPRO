import { participationHint, type ParticipationReading } from "@/lib/participationTerms";
import { ParticipationStatus } from "../ParticipationStatus";
import styles from "./ParticipationTerms.module.css";

export function ParticipationTerms({ reading, compact = false }: { reading: ParticipationReading; compact?: boolean }) {
  return <section className={styles.section} aria-label="Condiciones de participación">
    <h2>Cómo puedes participar</h2>
    <p className={styles.intro}>Subcontratación y consorcio según la lectura de las bases.</p>
    <div className={[styles.terms, compact ? styles.compact : ""].join(" ")}>
      {([ ["subcontratacion", "Subcontratación"], ["consorcio", "Consorcio"] ] as const).map(([key, label]) => {
        const term = reading[key];
        return <div className={styles.term} key={key}>
          <h3>{label}</h3><ParticipationStatus term={term} kind={key} />
          <details className={styles.evidence}>
            <summary>{term.clause ? "Ver cita y condiciones" : "Qué falta confirmar"}</summary>
            <p>{participationHint(term, key)}</p>
            {term.clause ? <blockquote>{term.clause}</blockquote> : null}
            {term.page ? <p className={styles.page}>Página {term.page} {reading.pageScope === "combined" ? "de la lectura combinada; puede diferir del original." : "del documento leído."}</p> : null}
            {term.conditions.length ? <><h4>Condiciones identificadas</h4><ul>{term.conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul></> : null}
            {!term.clause ? <p>Consulta las bases oficiales: la ausencia de una cláusula identificada no indica autorización ni prohibición.</p> : null}
          </details>
        </div>;
      })}
    </div>
  </section>;
}
