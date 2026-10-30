import { Activity, BookOpen, CircleAlert, CircleCheck, ClipboardCheck, Clock, Cog, FileText, Gauge, Hand, Info, Search, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { detailJson } from "@/server/services/ops/redact";
import type { TraceEvent, TraceKind, TraceOutcome } from "@/server/services/ops/traceAssembly";
import { formatFullDay, formatLimaDateTime, formatLimaTime, formatUsd, limaDayOfIso } from "../../model/format";
import styles from "./TraceTimeline.module.css";

const KIND: Record<TraceKind, { label: string; icon: LucideIcon }> = {
  hallazgo: { label: "Hallazgo", icon: Search },
  jev: { label: "JEV", icon: Gauge },
  trabajo: { label: "Trabajo", icon: Cog },
  documento: { label: "Documento", icon: FileText },
  lectura: { label: "Lectura", icon: BookOpen },
  evaluacion: { label: "Evaluación", icon: ClipboardCheck },
  manual: { label: "Manual", icon: Hand },
  seguimiento: { label: "Seguimiento", icon: Activity },
  pipeline: { label: "Pipeline", icon: Activity },
};

const OUTCOME: Record<TraceOutcome, { label: string; icon: LucideIcon }> = {
  ok: { label: "Correcto", icon: CircleCheck },
  warn: { label: "Atención", icon: TriangleAlert },
  error: { label: "Falló", icon: CircleAlert },
  pending: { label: "En curso", icon: Clock },
  info: { label: "Registro", icon: Info },
};

export const TRACE_KIND_LABEL: Record<TraceKind, string> = Object.fromEntries(
  Object.entries(KIND).map(([kind, value]) => [kind, value.label]),
) as Record<TraceKind, string>;

function groupByDay(events: TraceEvent[]) {
  const groups: Array<{ day: string; events: TraceEvent[] }> = [];
  for (const event of events) {
    const day = limaDayOfIso(event.ts);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.events.push(event);
    else groups.push({ day, events: [event] });
  }
  return groups;
}

/** Línea de tiempo cronológica (hora de Lima). El JSON de cada evento queda tras «ver detalle». */
export function TraceTimeline({ events }: { events: TraceEvent[] }) {
  return (
    <div className={styles.timeline}>
      {groupByDay(events).map((group) => (
        <section aria-label={formatFullDay(group.day)} className={styles.day} key={group.day}>
          <h3 className={styles.dayTitle}>{formatFullDay(group.day)}</h3>
          <ol className={styles.events}>
            {group.events.map((event) => {
              const kind = KIND[event.kind];
              const outcome = OUTCOME[event.outcome];
              const OutcomeIcon = outcome.icon;
              const KindIcon = kind.icon;
              return (
                <li className={`${styles.event} ${styles[event.outcome]}`} id={event.id} key={event.id}>
                  <time className={styles.time} dateTime={event.ts}>{formatLimaTime(event.ts)}</time>
                  <span className={styles.marker} title={outcome.label}>
                    <OutcomeIcon aria-hidden="true" size={15} strokeWidth={2} />
                    <span className={styles.srOnly}>{outcome.label}</span>
                  </span>
                  <div className={styles.card}>
                    <div className={styles.head}>
                      <strong className={styles.title}>{event.title}</strong>
                      <span className={styles.kind}>
                        <KindIcon aria-hidden="true" size={12} strokeWidth={2} />
                        {kind.label}
                      </span>
                      {event.company ? <span className={styles.company}>{event.company}</span> : null}
                      {event.costUsd != null && event.costUsd > 0 ? <span className={styles.cost} title="Costo registrado">{formatUsd(event.costUsd)}</span> : null}
                    </div>
                    {event.summary ? <p className={styles.summary}>{event.summary}</p> : null}
                    {event.reason ? (
                      <p className={styles.reason}>
                        <span>{event.outcome === "error" ? "Error" : "Motivo"}</span> {event.reason}
                      </p>
                    ) : null}
                    {event.facts.length ? (
                      <dl className={styles.facts}>
                        {event.facts.map((fact) => (
                          <div key={fact.label}>
                            <dt>{fact.label}</dt>
                            <dd>{fact.time ? formatLimaDateTime(fact.value) : fact.value}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                    <details className={styles.detail}>
                      <summary>ver detalle</summary>
                      <pre>{detailJson(event.detail)}</pre>
                    </details>
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      ))}
    </div>
  );
}
