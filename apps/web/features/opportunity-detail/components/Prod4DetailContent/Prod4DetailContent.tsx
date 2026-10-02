import Link from "next/link";
import { Building2, ExternalLink, FileText, MapPin, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { ProcurementSchedule } from "@/features/procurement";
import { formatDateTime } from "@/lib/format/date";
import { opportunityFacts, verdictShortLabels } from "@/lib/extraction/opportunity";
import type { ProcurementScheduleStage } from "@/lib/procurementSchedule";
import type { getProd4OpportunityForTenant } from "@/server/services/prod4Opportunities";
import { DecisionOverview } from "../DecisionOverview";
import { DetailSectionNavigator } from "../DetailSectionNavigator";
import styles from "./Prod4DetailContent.module.css";

type Detail = NonNullable<Awaited<ReturnType<typeof getProd4OpportunityForTenant>>>;
type RecordValue = Record<string, any>;
function text(value: unknown): string { return typeof value === "string" ? value : ""; }
function records(value: unknown): RecordValue[] { return Array.isArray(value) ? value.filter(v => v && typeof v === "object") : []; }
const labels: Record<string, string> = { cumple: "Cumple", cumple_con_accion: "Accionable", no_cumple: "No cumple", requiere_revision: "Revisar" };
function tone(status: string): "green" | "amber" | "red" | "neutral" { return status === "cumple" || status === "verde" ? "green" : status === "cumple_con_accion" || status === "ambar" ? "amber" : status === "no_cumple" || status === "rojo" ? "red" : "neutral"; }
function officialPdf(code: string) { return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(code) ? `https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=${encodeURIComponent(code)}` : null; }
function evidence(value: unknown): string { return Array.isArray(value) ? value.map(evidence).filter(Boolean).join(" · ") : typeof value === "string" ? value : value && typeof value === "object" ? [text((value as RecordValue).quote), text((value as RecordValue).text), (value as RecordValue).page ? `Página ${(value as RecordValue).page}` : ""].filter(Boolean).join(" · ") : ""; }
function stageDate(stage?: ProcurementScheduleStage) {
  if (!stage?.endsAt) return "No informada";
  if (stage.endPrecision === "day") return `${new Intl.DateTimeFormat("es-PE", { day:"2-digit", month:"2-digit", year:"numeric", timeZone:"UTC" }).format(new Date(`${stage.endsAt}T12:00:00Z`))} · Hora no informada`;
  return formatDateTime(stage.endsAt);
}

export function Prod4DetailContent({ detail }: { detail: Detail }) {
  const { process: p, documents, items, official_schedule: schedule, analysis } = detail;
  const extraction = analysis?.extraction;
  const match = analysis?.match;
  const breakdown = (match?.breakdown_json ?? {}) as RecordValue;
  const evaluated = records(breakdown.requisitos);
  const covered = evaluated.filter(r => r.estado === "cumple").length;
  const missing = evaluated.filter(r => r.estado !== "cumple");
  const facts = opportunityFacts(extraction);
  const primary = documents.find(d => d.codigo_alfresco === extraction?.codigo_alfresco) ?? documents.find(d => d.extension?.toLowerCase() === "pdf");
  const primaryUrl = primary ? officialPdf(primary.codigo_alfresco) : null;
  const proposals = schedule.find(s => s.kind === "proposals");
  const registration = schedule.find(s => s.kind === "registration");
  const fit = p.fit_level === 3 ? "Tu rubro exacto" : p.fit_level === 2 ? "Muy relacionado" : p.fit_level === 1 ? "Rubro general" : "Afinidad sin calcular";
  const pending = analysis?.status === "skipped" ? "Documento pendiente de acceso" : extraction ? "Resumen disponible; evaluación pendiente" : "Análisis documental pendiente";
  const raw = (extraction?.raw_extraction_json ?? {}) as RecordValue;
  const deliverables = records(raw.execution?.deliverables);
  const actions = Array.isArray(breakdown.acciones_recomendadas) ? breakdown.acciones_recomendadas.filter((v: unknown) => typeof v === "string") as string[] : [];
  return <>
    <nav className={styles.breadcrumb} aria-label="Ruta"><Link href="/feed?source=prod4">Oportunidades</Link><span>/</span><span>Concurso SEACE</span></nav>
    <header className={styles.header}>
      <span className={styles.eyebrow}>Procedimiento de selección · {p.object_type === "good" ? "Bienes" : "Servicios"}</span>
      <div className={styles.code}><h1 translate="no">{p.nomenclatura || p.id_procedimiento}</h1><Badge tone="brand">{p.source_window_status === "current" ? "En radar oficial" : "Fuera del radar"}</Badge></div>
      <p className={styles.title}>{p.title || p.description || "Objeto no informado"}</p>
      <div className={styles.meta}><span><Building2 aria-hidden="true" size={16} />{p.buyer_name || "Entidad no informada"}</span>{p.region ? <span><MapPin aria-hidden="true" size={16} />{p.region}</span> : null}</div>
    </header>
    <DecisionOverview analyzed={Boolean(match)} score={match?.score} verdict={match ? verdictShortLabels[match.verdict] ?? match.verdict : "Evaluación pendiente"} verdictTone={tone(match?.verdict ?? "")} fit={fit} deadline="Presentación de ofertas" deadlineDate={stageDate(proposals)} requirementsTotal={evaluated.length} coveredCount={covered} missingCount={missing.length} actions={<>
      <a className={styles.primaryAction} href={p.source_url ?? "https://prod4.seace.gob.pe/openegocio/"} target="_blank" rel="noopener noreferrer">Revisar en SEACE <ExternalLink aria-hidden="true" size={15} /></a>
      {primaryUrl ? <a className={styles.secondaryAction} href={primaryUrl} target="_blank" rel="noopener noreferrer">Abrir bases oficiales</a> : <span className={styles.muted}>Sin PDF publicado</span>}
    </>} />
    {!match || extraction?.requires_human_review ? <p className={styles.notice}><ShieldCheck aria-hidden="true" size={18} /><span>{extraction?.requires_human_review ? "Extracción pendiente de revisión humana." : pending}. {extraction ? "El resumen proviene de las bases; la afinidad preliminar no acredita cumplimiento." : "El worker local debe leer las bases antes de generar resumen, requisitos y puntaje."}</span></p> : null}
    <div className={styles.layout}>
      <DetailSectionNavigator tabs={[{ id: "decision", label: "Decisión y requisitos", count: analysis?.facets.length ?? 0 }, { id: "documents", label: "Documentos", count: documents.length }, { id: "schedule", label: "Cronograma" }, { id: "execution", label: "Ejecución e ítems" }]}>
        <div data-detail-pane="decision" id="detail-pane-decision" role="tabpanel" aria-labelledby="detail-tab-decision">
          <section className={styles.panel}><span className={styles.eyebrow}>Lectura ejecutiva</span><h2>Lo esencial para decidir</h2>
            <div className={styles.briefs}>
              <div><h3>Según tu perfil</h3><p>{text(breakdown.resumen) || "La evaluación de requisitos con tu empresa todavía no está disponible. No confundas afinidad de rubro con cumplimiento."}</p>{actions.length ? <ol>{actions.map((a, i) => <li key={i}>{a}</li>)}</ol> : null}</div>
              <div><h3>Qué solicita la entidad</h3><p>{facts.lecturaRapida || "Todavía no hay un resumen extraído de las bases oficiales. Puedes abrir el PDF original en SEACE."}</p>{facts.observaciones.length ? <ul>{facts.observaciones.map((v, i) => <li key={i}>{v}</li>)}</ul> : null}</div>
            </div>
            {evaluated.length && analysis?.facets.length ? <details className={styles.requirement}><summary><span>Ver los {analysis.facets.length} requisitos originales y su evidencia</span></summary>{analysis.facets.map((f, i) => <details className={styles.requirement} key={f.id ?? i}><summary><span>{f.label || "Requisito extraído"}</span></summary>{evidence(f.evidence_json) ? <blockquote>{evidence(f.evidence_json)}</blockquote> : <p className={styles.muted}>Sin cita de página disponible; revisa las bases oficiales.</p>}</details>)}</details> : null}
          </section>
          <section className={styles.panel}><h2>Requisitos y evidencia</h2><p className={styles.muted}>Requisitos extraídos del documento; la evaluación usa el perfil registrado, no una verificación externa.</p>
            {evaluated.length ? evaluated.map((r, i) => <details className={styles.requirement} key={i}><summary><span>{text(r.requisito) || text(r.label) || `Requisito ${i + 1}`}</span><Badge tone={tone(r.estado)}>{labels[r.estado] ?? "Revisar"}</Badge></summary><p>{text(r.gap) || "Sin brecha indicada"}</p>{r.accion ? <p><strong>Acción:</strong> {text(r.accion)}</p> : null}</details>) : analysis?.facets.length ? analysis.facets.map((f, i) => <details className={styles.requirement} key={f.id ?? i}><summary><span>{f.label || "Requisito"}</span><Badge>Sin evaluar</Badge></summary>{f.details_json ? <p>{text(f.details_json.value) || text(f.details_json.description) || text(f.details_json.texto) || "Consulta la evidencia del requisito."}</p> : null}{evidence(f.evidence_json) ? <blockquote>{evidence(f.evidence_json)}</blockquote> : <p className={styles.muted}>Sin cita de página disponible.</p>}</details>) : <p className={styles.empty}>Los requisitos aparecerán cuando el worker procese las bases.</p>}
          </section>
        </div>
        <div data-detail-pane="documents" id="detail-pane-documents" role="tabpanel" aria-labelledby="detail-tab-documents"><section className={styles.panel}><h2>Documentos oficiales</h2><p className={styles.muted}>Se abren directamente desde tu conexión en SEACE. El acceso depende del portal oficial; el TDR puede estar dentro de las bases.</p>
          {documents.length ? documents.map((d, i) => { const url = officialPdf(d.codigo_alfresco); return <article className={styles.document} key={`${d.codigo_alfresco}-${i}`}><FileText aria-hidden="true" size={24} /><div><h3>{d.name || d.document_type || "Documento oficial"}</h3><p>{d.document_type || "Documento"} · {d.extension?.toUpperCase() || "Formato no informado"}</p>{d.published_at ? <small>Publicado: {formatDateTime(d.published_at)}</small> : null}{d.codigo_alfresco === extraction?.codigo_alfresco ? <small>Documento utilizado en el análisis · {formatDateTime(extraction?.created_at)}</small> : null}</div>{url ? <a href={url} target="_blank" rel="noopener noreferrer" className={styles.secondaryAction}>Abrir en SEACE <ExternalLink aria-hidden="true" size={14} /></a> : <span className={styles.muted}>Enlace no disponible</span>}</article>; }) : <p className={styles.empty}>No hay documentos registrados en la ficha.</p>}
        </section></div>
        <div data-detail-pane="schedule" id="detail-pane-schedule" role="tabpanel" aria-labelledby="detail-tab-schedule"><ProcurementSchedule stages={schedule} source="prod4" fetchedAt={p.schedule_fetched_at ?? p.detail_fetched_at} /></div>
        <div data-detail-pane="execution" id="detail-pane-execution" role="tabpanel" aria-labelledby="detail-tab-execution"><section className={styles.panel}><h2>Ítems del procedimiento</h2>{items.length ? items.map((item, i) => <article className={styles.item} key={`${item.nro_item}-${i}`}><h3>{item.description || `Ítem ${item.nro_item}`}</h3><p>CUBSO {item.cubso_code || "no informado"} · {item.quantity ?? "Cantidad no informada"} {item.unit || ""}</p></article>) : <p className={styles.empty}>Sin ítems publicados.</p>}</section><section className={styles.panel}><h2>Ejecución según las bases</h2>{facts.plazoDias ? <p>Plazo de ejecución: {facts.plazoDias} días.</p> : null}{facts.tipoPago ? <p>Pago: {facts.tipoPago}.</p> : null}{deliverables.length ? <ul>{deliverables.map((v, i) => <li key={i}>{text(v.product) || text(v.description) || text(v.item) || `Entregable ${i + 1}`}{v.presentation_deadline ? ` · ${text(v.presentation_deadline)}` : ""}</li>)}</ul> : <p className={styles.muted}>Consulta las condiciones y entregables en las bases oficiales. No se han identificado entregables estructurados.</p>}</section></div>
      </DetailSectionNavigator>
      <aside className={styles.rail} aria-label="Contexto del concurso"><section className={styles.railPanel}><h2>Qué te falta</h2>{match ? missing.length ? <><p className={styles.muted}>Según tu perfil registrado</p>{missing.slice(0, 3).map((r, i) => <div className={styles.gap} key={i}><Badge tone={tone(r.estado)}>{labels[r.estado] ?? "Revisar"}</Badge><h3>{text(r.requisito) || text(r.label)}</h3><p>{text(r.gap)}</p>{r.accion ? <p><strong>{text(r.accion)}</strong></p> : null}</div>)}{missing.length > 3 ? <details><summary>Ver {missing.length - 3} requisitos adicionales</summary>{missing.slice(3).map((r, i) => <p key={i}>{text(r.requisito)}: {text(r.gap)} {text(r.accion)}</p>)}</details> : null}</> : <p>No se detectaron brechas en la evaluación disponible. Revisa las bases y la evidencia.</p> : <p className={styles.muted}>Pendiente de evaluación documental. Aquí verás las brechas y acciones, no solo un resumen.</p>}</section>
        <section className={styles.railPanel}><h2>Información oficial</h2><dl className={styles.facts}><div><dt>Registro hasta</dt><dd>{stageDate(registration)}</dd></div><div><dt>Ofertas hasta</dt><dd>{stageDate(proposals)}</dd></div><div><dt>Procedimiento</dt><dd>{p.procedure_type || "No informado"}</dd></div><div><dt>Estado documental</dt><dd>{match ? "Evaluado con tu perfil" : pending}</dd></div>{extraction ? <><div><dt>Análisis de bases</dt><dd>{formatDateTime(extraction.created_at)}</dd></div><div><dt>Modelo</dt><dd>{extraction.model}</dd></div></> : null}</dl><p className={styles.muted}>Las fechas provienen de SEACE. Estar en el radar no confirma que aún puedas postular.</p></section>
      </aside>
    </div>
  </>;
}
