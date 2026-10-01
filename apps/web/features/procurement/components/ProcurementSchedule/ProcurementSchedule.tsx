"use client";

import { useEffect, useState } from "react";
import { CalendarDays, Clock3, Minus, ArrowRight } from "lucide-react";
import type { ProcurementScheduleStage } from "@/lib/procurementSchedule";
import { scheduleTemporalState } from "@/lib/procurementSchedule";
import { formatDateTime } from "@/lib/format/date";
import styles from "./ProcurementSchedule.module.css";

const labels: Record<ProcurementScheduleStage["kind"], string> = {
  consultations: "Preguntas / consultas",
  observations: "Observaciones",
  answers: "Respuestas de la entidad",
  registration: "Registro de participantes",
  proposals: "Presentación de ofertas",
  publication: "Convocatoria",
  award: "Adjudicación",
  other: "Otra etapa oficial",
};

function ScheduleDate({ value, precision }: {
  value: string | null;
  precision: ProcurementScheduleStage["startPrecision"];
}) {
  if (!value || !precision) return <span className={styles.unknown}>No informado</span>;
  if (precision === "day") {
    const date = new Intl.DateTimeFormat("es-PE", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" })
      .format(new Date(`${value}T12:00:00Z`));
    return <><time dateTime={value}>{date}</time><small>Hora no informada</small></>;
  }
  const instant = new Date(value);
  const date = new Intl.DateTimeFormat("es-PE", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "America/Lima" }).format(instant);
  const hour = new Intl.DateTimeFormat("es-PE", { hour: "2-digit", minute: "2-digit", second: precision === "second" ? "2-digit" : undefined, hourCycle: "h23", timeZone: "America/Lima" }).format(instant);
  return <time dateTime={value}><span>{date}</span><span>{hour}</span></time>;
}

/** Shared official platform stages. No document extraction or inferred deadlines. */
export function ProcurementSchedule({ stages, source, fetchedAt, compact = false }: {
  stages: ProcurementScheduleStage[];
  source: "prod4" | "prod6";
  fetchedAt?: string | null;
  compact?: boolean;
}) {
  // First server/client render agrees; refresh temporal labels each minute.
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const states = stages.map(stage => now ? scheduleTemporalState(stage, now) : "unknown");
  const active = stages.filter((_, index) => states[index] === "active");
  const next = stages.filter((_, index) => states[index] === "upcoming").sort((a, b) => (a.startsAt ?? "").localeCompare(b.startsAt ?? ""))[0];
  const allPast = stages.length > 0 && states.every(state => state === "past");
  const statusLabels = { past: "Plazo finalizado", active: "En plazo", upcoming: "Próxima", unknown: "Sin precisión suficiente" };
  // Never move questions/offers ahead of earlier official stages: the rail is ordered.
  const visible = compact ? stages.slice(0, 3) : stages;
  const remaining = compact ? stages.slice(3) : [];
  function renderStage(stage: ProcurementScheduleStage) {
    const label = stage.kind === "other" ? stage.name : stage.kind === "consultations" && stage.kinds.includes("observations") ? "Preguntas y observaciones" : labels[stage.kind];
    const state = states[stages.indexOf(stage)];
    return (
      <li className={styles.stage} data-state={state} aria-current={state === "active" ? "step" : undefined} key={stage.id}>
        <span className={styles.marker} aria-hidden="true">{state === "active" ? <Clock3 size={14} /> : state === "upcoming" ? <ArrowRight size={14} /> : <Minus size={14} />}</span>
        <div className={styles.stageName}>
          <span className={styles.state}>{now ? statusLabels[state] : "Calculando plazo…"}</span>
          <h3>{label}</h3>
          {label.toLocaleLowerCase("es-PE") !== stage.name.toLocaleLowerCase("es-PE") ? <p>{stage.name}</p> : null}
        </div>
        <dl className={styles.dates}>
          <div><dt>{stage.kind === "answers" ? "Desde" : "Inicio"}</dt><dd><ScheduleDate value={stage.startsAt} precision={stage.startPrecision} /></dd></div>
          <div><dt>{stage.kind === "answers" ? "Hasta" : "Cierre"}</dt><dd><ScheduleDate value={stage.endsAt} precision={stage.endPrecision} /></dd></div>
        </dl>
      </li>
    );
  }
  return (
    <section aria-label="Cronograma oficial" className={`${styles.schedule} ${compact ? styles.compact : ""}`}>
      <header className={styles.header}>
        <CalendarDays aria-hidden="true" size={18} />
        <div><h2>Cronograma oficial</h2><p>Fechas de SEACE · Hora de Perú (UTC−5)</p></div>
      </header>
      {stages.length ? <div className={styles.position} role="status">
        <span className={styles.positionLabel}>Ubicación en el cronograma</span>
        <strong>{!now ? "Consultando fechas…" : active.length ? active.map(stage => stage.name).join(" · ") : allPast ? "Los plazos publicados ya finalizaron" : next ? `Sigue: ${next.name}` : "No se puede determinar la etapa actual"}</strong>
        <p>{active.length ? "En plazo según las fechas oficiales." : next ? "No hay una ventana activa confirmada con los datos publicados." : "Consulta SEACE para confirmar el estado del proceso."}</p>
      </div> : null}
      {stages.length ? (
        <ol className={styles.stages}>
          {visible.map(renderStage)}
        </ol>
      ) : <p className={styles.empty}>SEACE no informó etapas para este proceso. Verifica el cronograma en el portal oficial.</p>}
      {remaining.length ? <details className={styles.otherStages}><summary>Ver las {remaining.length} etapas siguientes</summary><ol className={styles.stages}>{remaining.map(renderStage)}</ol></details> : null}
      <footer className={styles.footer}>
        <span>Fuente: SEACE {source === "prod4" ? "Oportunidades" : "Contratos menores"}</span>
        {fetchedAt ? <span>Consultado: {formatDateTime(fetchedAt)}</span> : null}
        <p>La línea indica plazos, no acredita que una etapa se ejecutó ni que puedes postular. Preguntas, respuestas y ofertas tienen fechas distintas.</p>
      </footer>
    </section>
  );
}
