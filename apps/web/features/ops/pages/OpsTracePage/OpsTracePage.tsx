import { ArrowLeft, Coins, ExternalLink } from "lucide-react";
import { notFound } from "next/navigation";
import { AppShell } from "@/features/shell";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { getTrace } from "@/server/services/ops/trace";
import type { Trace } from "@/server/services/ops/traceAssembly";
import { OPS_SOURCE_LABEL } from "@/server/services/ops/types";
import type { OpsSource } from "@/server/services/ops/types";
import { TRACE_KIND_LABEL, TraceTimeline } from "../../components/TraceTimeline";
import { formatCount, formatLimaDateTime, formatUsd } from "../../model/format";
import styles from "./OpsTracePage.module.css";

function fichaHref(source: OpsSource, id: string) {
  return source === "menores" ? `/oportunidad/${id}` : `/oportunidad/prod2/${id}`;
}

/** «Traza»: todo lo que sabemos de una licitación, en orden cronológico y en hora de Lima. */
export async function OpsTracePage({ source, id }: { source: OpsSource; id: string }) {
  let trace: Trace | null = null;
  let failed = false;
  try {
    trace = await getTrace(source, id);
  } catch (error) {
    failed = true;
    console.error("ops trace failed", error instanceof Error ? error.message : error);
  }
  if (!trace && !failed) notFound();

  const back = (
    <a className={styles.back} href={`/ops#${source}`}>
      <ArrowLeft aria-hidden="true" size={14} strokeWidth={2} />
      Operación
    </a>
  );

  if (!trace) {
    return (
      <AppShell title="Operación">
        <div className={styles.page}>
          <PageHeader eyebrow={back} title="Traza" />
          <EmptyState title="No se pudo cargar la traza">La consulta falló o excedió el tiempo límite. Recarga la página.</EmptyState>
        </div>
      </AppShell>
    );
  }

  const kinds = Object.entries(trace.totals.byKind) as Array<[keyof typeof TRACE_KIND_LABEL, number]>;
  return (
    <AppShell title="Operación">
      <div className={styles.page}>
        <PageHeader
          actions={
            <a className={styles.ficha} href={fichaHref(source, id)}>
              Ver ficha
              <ExternalLink aria-hidden="true" size={14} strokeWidth={2} />
            </a>
          }
          description={trace.header.entity ?? undefined}
          eyebrow={back}
          meta={`${OPS_SOURCE_LABEL[source]} · ${source === "menores" ? "contrato" : "convocatoria"} ${id}`}
          title={`Traza · ${trace.header.code}`}
        />
        {trace.header.description ? <p className={styles.description}>{trace.header.description}</p> : null}

        <section aria-label="Resumen de la traza" className={styles.facts}>
          <dl>
            {trace.header.facts.map((fact) => (
              <div key={fact.label}>
                <dt>{fact.label}</dt>
                <dd>{fact.time ? formatLimaDateTime(fact.value) : fact.value}</dd>
              </div>
            ))}
            <div>
              <dt>Eventos</dt>
              <dd>{formatCount(trace.totals.events)}</dd>
            </div>
            <div className={styles.cost}>
              <dt>
                <Coins aria-hidden="true" size={13} strokeWidth={2} /> Costo registrado
              </dt>
              <dd>{formatUsd(trace.totals.costUsd)}</dd>
            </div>
          </dl>
          {kinds.length ? (
            <ul aria-label="Eventos por tipo" className={styles.kinds}>
              {kinds.map(([kind, count]) => (
                <li key={kind}>
                  {TRACE_KIND_LABEL[kind]} <strong>{formatCount(count)}</strong>
                </li>
              ))}
            </ul>
          ) : null}
        </section>

        {trace.header.notes.map((note) => (
          <p className={styles.note} key={note}>{note}</p>
        ))}

        {trace.events.length ? (
          <p className={styles.timeNote}>Las horas están en hora de Lima. El JSON de «ver detalle» conserva las marcas originales en UTC (Lima = UTC−5).</p>
        ) : null}
        {trace.events.length ? (
          <TraceTimeline events={trace.events} />
        ) : (
          <EmptyState title="Sin eventos registrados">Todavía no hay nada guardado de esta licitación.</EmptyState>
        )}
      </div>
    </AppShell>
  );
}
