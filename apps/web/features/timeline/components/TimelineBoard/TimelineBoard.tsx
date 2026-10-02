"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Building2, Radio, ChevronLeft, ChevronRight, RefreshCw, Clock3, ArrowUpRight, X, CalendarDays } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { EmptyState } from "@/components/ui/EmptyState";
import { scheduleTemporalState, type ProcurementScheduleStage } from "@/lib/procurementSchedule";
import type { TimelineResponse, TimelineOpportunity } from "@/lib/procurementTimeline";
import { refreshTimeline } from "../../api/timeline";
import { addDays, dayOffset, formatBoundary, layoutStages, limaDay, stageLabel, validDay } from "../../utils/gantt";
import styles from "./TimelineBoard.module.css";

function Signal({ row }: { row: TimelineOpportunity }) {
  if (row.status === "evaluated") return <span className={`${styles.signal} ${row.verdict === "verde" ? styles.green : styles.amber}`}><b>{row.score}</b>{row.verdict === "verde" ? "Cumples" : "Te falta poco"}</span>;
  return <span className={`${styles.signal} ${styles.violet}`}><span aria-hidden="true">{"●".repeat(row.fitLevel)}</span>{row.fitLevel === 3 ? "Tu rubro exacto" : "Muy relacionado"}</span>;
}

export function TimelineBoard({ initial, initialParams }: { initial: TimelineResponse; initialParams: Record<string, string> }) {
  const [result, setResult] = useState(initial);
  const [now, setNow] = useState(() => new Date(initial.meta.generatedAt));
  const today = limaDay(now.toISOString());
  const [start, setStart] = useState(() => validDay(initialParams.start) ? initialParams.start : addDays(today, -2));
  const [days, setDays] = useState([14, 30, 60].includes(Number(initialParams.days)) ? Number(initialParams.days) : 14);
  const [q, setQ] = useState(initialParams.q ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<{ row: TimelineOpportunity; stage: ProcurementScheduleStage } | null>(null);
  const requestEpoch = useRef(0);
  const activeSource = useRef(initial.meta.source);
  activeSource.current = initial.meta.source;
  const source = result.meta.source;
  useEffect(() => {
    setResult(initial); setQ(initialParams.q ?? ""); setSelected(null);
    requestEpoch.current += 1; setBusy(false); setError("");
    setStart(validDay(initialParams.start) ? initialParams.start : addDays(limaDay(initial.meta.generatedAt), -2));
    setDays([14, 30, 60].includes(Number(initialParams.days)) ? Number(initialParams.days) : 14);
  }, [initial, initialParams.q, initialParams.start, initialParams.days]);
  useEffect(() => {
    const restore = () => {
      const params = new URL(window.location.href).searchParams;
      const date = params.get("start") ?? undefined;
      setStart(validDay(date) ? date : addDays(limaDay(new Date().toISOString()), -2));
      setDays([14, 30, 60].includes(Number(params.get("days"))) ? Number(params.get("days")) : 14);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60000);
    return () => window.clearInterval(timer);
  }, []);
  const updateWindow = (nextStart: string, nextDays = days) => {
    setStart(nextStart); setDays(nextDays);
    const url = new URL(window.location.href);
    url.searchParams.set("start", nextStart); url.searchParams.set("days", String(nextDays));
    window.history.replaceState(null, "", url);
  };
  async function refresh(search = q) {
    const epoch = ++requestEpoch.current;
    setBusy(true); setError("");
    try {
      const next = await refreshTimeline(source, search);
      if (epoch !== requestEpoch.current || source !== activeSource.current) return;
      setResult(next); setNow(new Date()); setSelected(null);
      const url = new URL(window.location.href);
      search ? url.searchParams.set("q", search) : url.searchParams.delete("q");
      window.history.replaceState(null, "", url);
    } catch (err) { if (epoch === requestEpoch.current && source === activeSource.current) setError(err instanceof Error ? err.message : "No se pudo actualizar. Intenta nuevamente."); }
    finally { if (epoch === requestEpoch.current && source === activeSource.current) setBusy(false); }
  }
  const rows = result.data;
  const urgent = rows.filter(row => row.deadline?.at && row.deadline.precision !== "day" && dayOffset(row.deadline.at, today) >= 0 && new Date(row.deadline.at).getTime() - now.getTime() <= 3 * 86400000 && new Date(row.deadline.at).getTime() > now.getTime()).length;
  const urgentDays = rows.filter(row => row.deadline?.precision === "day" && dayOffset(row.deadline.at, today) >= 0 && dayOffset(row.deadline.at, today) <= 3).length;
  const end = addDays(start, days - 1);
  const axisDays = Array.from({ length: days }, (_, index) => addDays(start, index));
  const dayWidth = days === 14 ? 54 : days === 30 ? 38 : 28;
  const dateShort = (value: string) => new Intl.DateTimeFormat("es-PE", { timeZone: "UTC", day: "numeric", month: "short" }).format(new Date(`${value}T12:00:00Z`));
  const todayOffset = dayOffset(today, start);
  const chartVars = { "--day-width": `${dayWidth}px`, "--timeline-width": `${days * dayWidth}px` } as CSSProperties;
  const sourceHref = (value: string) => `/cronograma?${new URLSearchParams({ source: value, start, days: String(days) })}`;
  return <>
    <div className={styles.topline}>
      <nav className={styles.sources} aria-label="Fuente del cronograma">
        <Link href={sourceHref("prod6")} className={source === "prod6" ? styles.sourceActive : ""} aria-current={source === "prod6" ? "page" : undefined}><Radio size={18} aria-hidden="true" />Contratos menores</Link>
        <Link href={sourceHref("prod4")} className={source === "prod4" ? styles.sourceActive : ""} aria-current={source === "prod4" ? "page" : undefined}><Building2 size={18} aria-hidden="true" />Concursos SEACE</Link>
      </nav>
      <p className={styles.urgency}><Clock3 size={17} aria-hidden="true" /><strong>{urgent}</strong> cierran en las próximas 72 h{urgentDays ? <span> · {urgentDays} próximos con hora no informada</span> : null}</p>
    </div>
    <div className={styles.toolbar}>
      <div className={styles.windowControls}>
        <Button variant="secondary" aria-label="Periodo anterior" onClick={() => updateWindow(addDays(start, -days))}><ChevronLeft size={18} aria-hidden="true" /></Button>
        <Button variant="secondary" onClick={() => updateWindow(addDays(today, -2))}>Hoy</Button>
        <Button variant="secondary" aria-label="Periodo siguiente" onClick={() => updateWindow(addDays(start, days))}><ChevronRight size={18} aria-hidden="true" /></Button>
        <strong className={styles.range}>{dateShort(start)} — {dateShort(end)} <small>{end.slice(0, 4)}</small></strong>
        <Select aria-label="Días visibles" value={days} onChange={event => updateWindow(start, Number(event.target.value))}><option value={14}>14 días</option><option value={30}>30 días</option><option value={60}>60 días</option></Select>
      </div>
      <form className={styles.search} onSubmit={event => { event.preventDefault(); void refresh(); }}>
        <Input aria-label="Buscar oportunidades en el cronograma" name="q" autoComplete="off" maxLength={120} placeholder="Código, entidad u objeto…" value={q} onChange={event => setQ(event.target.value)} />
        <Button disabled={busy} type="submit">Buscar</Button>
        <Button disabled={busy} variant="secondary" type="button" aria-label="Actualizar cronograma desde la base de datos" onClick={() => void refresh()}><RefreshCw size={17} aria-hidden="true" /></Button>
      </form>
    </div>
    <div className={styles.legend}>
      <span>{rows.length} oportunidades con potencial</span>
      <span className={styles.violet}>●● / ●●● Afinidad sin evaluar</span>
      <span className={styles.amber}>● Te falta poco</span>
      <span className={styles.green}>● Cumples</span>
    </div>
    <p className={styles.mobileHint}>Agenda de cierres · abre una oportunidad para consultar todas sus etapas.</p>
    <div aria-live="polite" className={styles.feedback}>{busy ? "Actualizando cronograma…" : error}</div>
    {selected ? <section className={styles.selection} aria-label="Etapa seleccionada">
      <div><small>{selected.row.code}</small><h2>{selected.stage.name}</h2><p>{formatBoundary(selected.stage.startsAt, selected.stage.startPrecision)} → {formatBoundary(selected.stage.endsAt, selected.stage.endPrecision)}</p></div>
      <Link href={`${selected.row.detailHref}#schedule`}>Ver cronograma completo <ArrowUpRight size={16} aria-hidden="true" /></Link>
      <Button variant="ghost" aria-label="Cerrar etapa seleccionada" onClick={() => setSelected(null)}><X size={18} aria-hidden="true" /></Button>
    </section> : null}
    {!rows.length ? <EmptyState title="No hay oportunidades con estos criterios">Solo aparecen afinidades de 2–3 puntos sin evaluar y evaluaciones amarillas o verdes. Revisa tu búsqueda o tus líneas de negocio.</EmptyState> : <>
      <div className={styles.chart} style={chartVars} role="region" aria-label="Gantt de oportunidades y etapas oficiales" tabIndex={0}>
        <div className={styles.axis}>
          <div className={styles.axisLabel}>Oportunidad <small>Ordenadas por cierre de ofertas</small></div>
          <div className={styles.axisDates}>{axisDays.map(date => <div key={date} className={date === today ? styles.todayDate : ""}><small>{new Intl.DateTimeFormat("es-PE", { timeZone: "UTC", weekday: "short" }).format(new Date(`${date}T12:00:00Z`))}</small><strong>{Number(date.slice(8))}</strong>{date === today ? <em>Hoy</em> : <span>{dateShort(date).split(" ").slice(1).join(" ")}</span>}</div>)}</div>
        </div>
        {rows.map(row => {
          const layout = layoutStages(row.schedule, start, days);
          const rowHeight = Math.max(128, layout.lanes * 36 + 32);
          const hasPartial = row.schedule.some(stage => !stage.startsAt || !stage.endsAt);
          return <div className={styles.row} key={row.id} style={{ minHeight: rowHeight }}>
            <div className={styles.identity}>
              <Signal row={row} />
              <Link href={row.detailHref} title={row.title} className={styles.code} translate="no">{row.code}<ArrowUpRight size={14} aria-hidden="true" /></Link>
              <p title={row.title}>{row.title}</p>
              <small title={row.entity}>{row.entity}</small>
              <span className={styles.deadline}>{row.deadline ? `Ofertas: ${formatBoundary(row.deadline.at, row.deadline.precision)}` : "Cierre de ofertas no informado"}</span>
            </div>
            <div className={styles.track}>
              {todayOffset >= 0 && todayOffset < days ? <div className={styles.todayLine} aria-hidden="true" style={{ left: (todayOffset + .5) * dayWidth }} /> : null}
              {layout.bars.map(bar => {
                const state = scheduleTemporalState(bar.stage, now);
                const shortLabel = ({ proposals: "Oferta", consultations: "Preg.", observations: "Obs.", registration: "Reg.", publication: "Conv.", answers: "Resp.", award: "Pro", other: /integra/i.test(bar.stage.name) ? "Integr." : /califica|evalua/i.test(bar.stage.name) ? "Eval." : "Etapa" })[bar.stage.kind];
                return <button key={bar.stage.id} className={`${styles.bar} ${bar.stage.kind === "proposals" ? styles.offerBar : ""} ${state === "past" ? styles.pastBar : ""} ${state === "active" ? styles.activeBar : ""}`} style={{ left: bar.start * dayWidth + 4, width: Math.max(24, (bar.end - bar.start) * dayWidth - 8), top: 16 + bar.lane * 36 }} onClick={() => setSelected({ row, stage: bar.stage })} aria-label={`${row.code}: ${bar.stage.name}. Inicio ${formatBoundary(bar.stage.startsAt, bar.stage.startPrecision)}. Fin ${formatBoundary(bar.stage.endsAt, bar.stage.endPrecision)}.`} title={`${bar.stage.name}\n${formatBoundary(bar.stage.startsAt, bar.stage.startPrecision)} → ${formatBoundary(bar.stage.endsAt, bar.stage.endPrecision)}`}>
                  <span>{bar.clippedStart ? "‹ " : ""}{bar.end - bar.start <= 1 ? shortLabel : stageLabel(bar.stage)}{bar.clippedEnd ? " ›" : ""}</span>
                </button>;
              })}
              {!layout.bars.length ? <div className={styles.outside}><CalendarDays size={17} aria-hidden="true" /><span>{row.schedule.length ? "Sin etapas fechadas en este periodo" : "Cronograma oficial pendiente"}</span>{row.deadline?.at ? <Button variant="ghost" size="compact" onClick={() => updateWindow(addDays(limaDay(row.deadline!.at), -3))}>Ir al cierre</Button> : null}</div> : null}
              {hasPartial ? <Link href={`${row.detailHref}#schedule`} className={styles.partial}>Hay etapas sin fecha completa · ver detalle</Link> : null}
            </div>
          </div>;
        })}
      </div>
      <div className={styles.mobileList}>
        {rows.map(row => <section key={row.id} className={styles.mobileRow}><Signal row={row} /><Link className={styles.code} href={row.detailHref}>{row.code}<ArrowUpRight size={16} aria-hidden="true" /></Link><p>{row.title}</p><span className={styles.deadline}>{row.deadline ? `Ofertas: ${formatBoundary(row.deadline.at, row.deadline.precision)}` : "Cierre de ofertas no informado"}</span><details><summary>Ver {row.schedule.length} etapas oficiales</summary>{row.schedule.map(stage => <div className={styles.mobileStage} key={stage.id}><strong>{stage.name}</strong><span>{formatBoundary(stage.startsAt, stage.startPrecision)}</span><span>Hasta {formatBoundary(stage.endsAt, stage.endPrecision)}</span></div>)}</details></section>)}
      </div>
    </>}
    <footer className={styles.note}>Barras oscuras: ofertas. Barras claras: otras etapas. Una etapa vencida no acredita su ejecución. Fechas en hora de Perú; confirma cambios y requisitos en SEACE.<br />Consulta de BuenaPro: {formatBoundary(result.meta.generatedAt, "minute")}. El worker actualiza las fechas oficiales; este botón vuelve a consultar la base de datos.</footer>
  </>;
}
