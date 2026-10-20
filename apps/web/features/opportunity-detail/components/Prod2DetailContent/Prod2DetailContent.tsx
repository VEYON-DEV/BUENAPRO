import Link from "next/link";
import { Building2, ChevronDown, ExternalLink, FileText, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { SaveButton } from "@/features/opportunities";
import { ParticipationTerms, ProcurementSchedule } from "@/features/procurement";
import { participationReading } from "@/lib/participationTerms";
import { keywordAffinitySummary } from "@/lib/keywordAffinity";
import { formatDateTime } from "@/lib/format/date";
import { opportunityFacts, verdictShortLabels } from "@/lib/extraction/opportunity";
import type { ProcurementScheduleStage } from "@/lib/procurementSchedule";
import type { getProd2OpportunityForTenant } from "@/server/services/prod2Opportunities";
import { evaluationPresentation, isAdministrativeRequirement } from "../../model/evaluationPresentation";
import { requirementFullText, requirementSourceFacet } from "../../model/requirementText";
import { AdministrativeChecklist } from "../AdministrativeChecklist";
import { DecisionOverview } from "../DecisionOverview";
import { DetailSectionNavigator } from "../DetailSectionNavigator";
import styles from "./Prod2DetailContent.module.css";

type Detail = NonNullable<Awaited<ReturnType<typeof getProd2OpportunityForTenant>>>;
type RecordValue = Record<string, any>;
const text = (value: unknown) => typeof value === "string" ? value : "";
const record = (value: unknown): RecordValue => value && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
const records = (value: unknown): RecordValue[] => Array.isArray(value) ? value.filter(row => row && typeof row === "object") : [];
const labels: Record<string, string> = { cumple: "Cumple", cumple_con_accion: "Accionable", no_cumple: "No cumple", requiere_revision: "Revisar", no_aplica: "No aplica" };
function tone(status: string): "green" | "amber" | "red" | "neutral" {
  return status === "cumple" || status === "verde" ? "green" : status === "cumple_con_accion" || status === "ambar" ? "amber" : status === "no_cumple" || status === "rojo" ? "red" : "neutral";
}
function stageDate(stage?: ProcurementScheduleStage) {
  if (!stage?.endsAt) return "Fecha no informada";
  return stage.endPrecision === "day" ? `${new Intl.DateTimeFormat("es-PE", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }).format(new Date(`${stage.endsAt}T12:00:00Z`))} · Hora no informada` : formatDateTime(stage.endsAt);
}
function evidence(value: unknown): string {
  if (Array.isArray(value)) return value.map(evidence).filter(Boolean).join(" · ");
  if (typeof value === "string") return value;
  const source = record(value);
  return [text(source.quote) || text(source.text), source.page ? `Página ${source.page}` : ""].filter(Boolean).join(" · ");
}

export function Prod2DetailContent({ detail }: { detail: Detail }) {
  const { process: process, documents, official_schedule: schedule, analysis, profile } = detail;
  const extraction = analysis.extraction;
  const match = analysis.match;
  const breakdown = record(match?.breakdown_json);
  const evaluation = evaluationPresentation(breakdown, match?.missing_actions_json, analysis.facets);
  const explicitChecklist = Array.isArray(breakdown.administrative_checklist) || Array.isArray(breakdown.preparacion_documental);
  const evaluated = records(breakdown.requisitos).filter(requirement => requirement.score_impact !== false
    && requirement.source_facet !== "penalty_condition"
    && (explicitChecklist || !isAdministrativeRequirement(requirement, analysis.facets)));
  const sourceFacet = (requirement: RecordValue) => requirementSourceFacet(requirement, analysis.facets);
  const missing = evaluation.gaps;
  const facts = opportunityFacts(extraction);
  const primary = documents.find(document => document.codigo_alfresco === extraction?.codigo_alfresco)
    ?? documents.find(document => document.download_url);
  const proposals = schedule.find(stage => stage.kind === "proposals");
  const registration = schedule.find(stage => stage.kind === "registration");
  const memberNames = record(record(breakdown.meta).member_names);
  const contextNames = [...new Set(Object.values(memberNames).filter(value => typeof value === "string"))].join(" + ");
  const context = contextNames || profile?.name || "Tu perfil";
  const raw = record(extraction?.raw_extraction_json);
  const deliverables = records(record(raw.execution).deliverables);
  const actions = Array.isArray(breakdown.acciones_recomendadas) ? breakdown.acciones_recomendadas.filter((value: unknown) => typeof value === "string") as string[] : [];
  const feedUrl = `/feed?source=prod2${profile ? `&profile_id=${encodeURIComponent(profile.id)}` : ""}`;
  const affinity = keywordAffinitySummary(process);
  const commercialAffinity = process.commercial_affinity;
  const renderGap = (requirement: RecordValue, index: number) => {
    const fullText = requirementFullText(requirement, analysis.facets);
    const title = text(requirement.requisito) || text(requirement.label);
    return <div className={styles.gap} key={index}><Badge tone={tone(requirement.estado)}>{labels[requirement.estado] ?? "Revisar"}</Badge>
      <h3>{title}</h3><p>{text(requirement.gap)}</p>
      {fullText && fullText !== title ? <details className={styles.disclosure}><summary>Ver requisito completo<ChevronDown aria-hidden="true" size={16} /></summary><p>{fullText}</p></details> : null}
    </div>;
  };
  return <>
    <nav className={styles.breadcrumb} aria-label="Ruta"><Link href={feedUrl}>Oportunidades</Link><span>/</span><span>Buscador SEACE</span></nav>
    <header className={styles.header}>
      <span className={styles.eyebrow}>{process.procedure_type || "Procedimiento SEACE"} · {process.object_type === "good" ? "Bienes" : "Servicios"}</span>
      <div className={styles.code}><h1 translate="no">{process.nomenclatura || process.id_convocatoria}</h1><Badge tone="brand">Fuente PROD2</Badge></div>
      <p className={styles.title}>{process.description || "Objeto no informado"}</p>
      <div className={styles.meta}><span><Building2 aria-hidden="true" size={16} />{process.buyer_name || "Entidad no informada"}</span>
        {process.source_url ? <a href={process.source_url} target="_blank" rel="noopener noreferrer">Revisar en SEACE <ExternalLink aria-hidden="true" size={15} /></a> : null}</div>
      {process.has_official_detail === false ? <p className={styles.muted}>Solo datos del listado. La ficha oficial, las bases y el cronograma todavía no están disponibles en BuenaPro.</p> : null}
      {process.publication_status_label ? <p className={styles.muted}>{process.publication_status_label}. Estado conservado de la última revisión.</p> : null}
    </header>
    <DecisionOverview analyzed={evaluation.hasEvaluation} score={match?.score} provisionalScore={match?.provisional_score} scoreUnderReview={evaluation.scoreUnderReview || Boolean(process.match_review_required)} verdict={match ? verdictShortLabels[match.verdict] ?? match.verdict : "Evaluación pendiente"}
      verdictTone={tone(match?.verdict ?? "")} fit={commercialAffinity ? `${commercialAffinity.index} / 100 · Afinidad comercial por título` : affinity ? `${affinity.label} · ${affinity.pointLabel} puntos` : "Sin afinidad calculada"}
      deadline="Presentación de ofertas" deadlineDate={stageDate(proposals)} requirementsTotal={evaluation.substantive.length} coveredCount={evaluation.coveredCount} missingCount={missing.length} requirementsLabel="Requisitos clave cubiertos"
      actions={<>{primary?.download_url ? <a className={styles.primaryAction} href={primary.download_url} target="_blank" rel="noopener noreferrer"><FileText aria-hidden="true" size={16} /> Abrir bases en PDF</a>
        : process.source_url ? <a className={styles.primaryAction} href={process.source_url} target="_blank" rel="noopener noreferrer">Consultar bases en SEACE <ExternalLink aria-hidden="true" size={16} /></a> : <span>Documento pendiente</span>}
        <SaveButton code={process.nomenclatura} idContrato={process.id_convocatoria} endpoint={`/api/prod2/opportunities/${process.id_convocatoria}/saved`} initialSaved={Boolean(process.is_saved)} showLabel /></>} />
    <p className={styles.notice}><ShieldCheck aria-hidden="true" size={18} /><span>{commercialAffinity ? <><strong>Afinidad comercial: {commercialAffinity.index} / 100.</strong> Comparación del título con el perfil comercial actual de tu empresa. </> : affinity ? <><strong>{affinity.lineName}</strong> · {affinity.pointLabel} de 45 puntos por palabras clave. {affinity.hits.length ? `Coincidencias: ${affinity.hits.join(", ")}. ` : "Sin coincidencias de palabras clave. "}</> : null}{contextNames ? "Convenio declarado; participación conjunta pendiente de verificar. " : "La evaluación usa el perfil registrado. "}
      {extraction?.requires_human_review ? "Las bases requieren revisión humana. " : ""}La afinidad comercial y el puntaje no acreditan elegibilidad ni son probabilidades de adjudicación.</span></p>
    <div className={styles.layout}>
      <DetailSectionNavigator tabs={[{ id: "decision", label: "Decisión y requisitos", count: evaluated.length || evaluation.substantiveFacets.length },
        { id: "documents", label: "Documentos", count: documents.length }, { id: "schedule", label: "Cronograma" }, { id: "execution", label: "Ejecución" }]}>
        <div data-detail-pane="decision" id="detail-pane-decision" role="tabpanel" aria-labelledby="detail-tab-decision">
          <section className={styles.panel}><h2>Lo esencial para decidir</h2><div className={styles.briefs}>
            <div><h3>{context}</h3><p>{text(breakdown.resumen) || "La evaluación documental todavía no está disponible. Revisa las bases y la modalidad para determinar cómo participar."}</p>
              {actions.length ? <details className={styles.disclosure}><summary>Acciones recomendadas</summary><ol>{actions.map((action, index) => <li key={index}>{action}</li>)}</ol></details> : null}</div>
            <div><h3>Qué solicita la entidad</h3><p>{facts.lecturaRapida || "Consulta las bases oficiales para conocer el alcance y los requisitos."}</p>
              {facts.observaciones.length ? <details className={styles.disclosure}><summary>Observaciones de las bases</summary><ul>{facts.observaciones.map((value, index) => <li key={index}>{value}</li>)}</ul></details> : null}</div>
          </div><ParticipationTerms reading={participationReading(process.participation_extraction)} /></section>
          <section className={styles.panel}><h2>Experiencia y capacidades requeridas</h2><p className={styles.muted}>Cada evaluación se vincula a las bases descargadas. Los requisitos deseables conservan su carácter.</p>
            {evaluated.length ? evaluated.map((requirement, index) => { const facet = sourceFacet(requirement); const fullText = requirementFullText(requirement, analysis.facets); return <details className={styles.requirement} key={text(requirement.requirement_id) || index}>
              <summary><span>{text(requirement.requisito) || `Requisito ${index + 1}`}{requirement.conditional_branch === "mype" ? <small className={styles.requirementKind}>Condicional · MYPE</small> : facet ? <small className={styles.requirementKind}>{facet.required ? "Obligatorio" : "Deseable / no obligatorio"}</small> : null}</span><Badge tone={tone(requirement.estado)}>{labels[requirement.estado] ?? "Revisar"}</Badge></summary>
              <p>{text(requirement.gap) || "Sin brecha indicada."}</p>{requirement.accion ? <p><strong>Acción:</strong> {text(requirement.accion)}</p> : null}
              {fullText && fullText !== text(requirement.requisito) ? <p><strong>Requisito de las bases:</strong> {fullText}</p> : null}
              {records(requirement.member_gaps).map((member, memberIndex) => <div className={styles.member} key={text(member.profile_id) || memberIndex}>
                <h3>{text(memberNames[member.profile_id]) || "Miembro del convenio"}<Badge tone={tone(member.estado)}>{labels[member.estado] ?? "Revisar"}</Badge></h3>
                <p>{text(member.gap) || "Sin brecha indicada."}</p>{member.accion ? <p><strong>Acción:</strong> {text(member.accion)}</p> : null}
              </div>)}
              {facet && evidence(facet.evidence_json) ? <blockquote>{evidence(facet.evidence_json)}</blockquote> : <p className={styles.muted}>Consulta el requisito en las bases originales.</p>}
            </details>; }) : evaluation.substantiveFacets.length ? evaluation.substantiveFacets.map((facet, index) => <details className={styles.requirement} key={facet.id ?? index}>
              <summary><span>{facet.label}</span><Badge>{facet.required ? "Obligatorio" : "Deseable"}</Badge></summary>
              {requirementFullText(facet, analysis.facets) && requirementFullText(facet, analysis.facets) !== facet.label ? <p>{requirementFullText(facet, analysis.facets)}</p> : null}
              {evidence(facet.evidence_json) ? <blockquote>{evidence(facet.evidence_json)}</blockquote> : <p className={styles.muted}>Consulta las bases originales.</p>}
            </details>) : <p className={styles.empty}>{extraction ? "Sin requisitos de capacidad detectados; consulta las bases originales." : "Los requisitos estarán disponibles cuando termine la lectura de las bases."}</p>}
            <AdministrativeChecklist items={evaluation.administrativeChecklist} />
          </section>
        </div>
        <div data-detail-pane="documents" id="detail-pane-documents" role="tabpanel" aria-labelledby="detail-tab-documents"><section className={styles.panel}>
          <h2>Documentos oficiales</h2><p className={styles.muted}>Vistas PDF del documento descargado para consulta. La ficha de SEACE conserva el archivo original y su procedencia.</p>
          {!process.document_inventory_complete ? <p className={styles.muted}>El inventario de la ficha todavía está incompleto.</p> : null}
          {documents.length ? documents.map(document => <article className={styles.document} key={document.codigo_alfresco}>
            <FileText aria-hidden="true" size={24} /><div><h3>{document.name}</h3><p>{document.document_type} · {document.stage_name}</p>
              {document.published_at_text ? <small>Publicado: {document.published_at_text}</small> : null}
              {document.download_url && document.extension !== "pdf" ? <small>Vista PDF convertida · Original: {document.extension?.toUpperCase() || "archivo de SEACE"}</small> : null}
              {document.codigo_alfresco === extraction?.codigo_alfresco ? <small>Documento utilizado en la evaluación</small> : null}</div>
            {document.download_url ? <a className={styles.secondaryAction} href={document.download_url} target="_blank" rel="noopener noreferrer">Abrir PDF <ExternalLink aria-hidden="true" size={14} /></a>
              : process.source_url ? <a className={styles.secondaryAction} href={process.source_url} target="_blank" rel="noopener noreferrer">Consultar en SEACE <ExternalLink aria-hidden="true" size={14} /></a> : <span>Descarga pendiente</span>}
          </article>) : <p className={styles.empty}>No hay documentos registrados en esta ficha.</p>}
        </section></div>
        <div data-detail-pane="schedule" id="detail-pane-schedule" role="tabpanel" aria-labelledby="detail-tab-schedule">
          <ProcurementSchedule stages={schedule} source="prod2" fetchedAt={process.detail_fetched_at} />
        </div>
        <div data-detail-pane="execution" id="detail-pane-execution" role="tabpanel" aria-labelledby="detail-tab-execution"><section className={styles.panel}>
          <h2>Condiciones de ejecución</h2>{facts.plazoDias ? <p>Plazo de ejecución: {facts.plazoDias} días.</p> : null}{facts.tipoPago ? <p>Pago: {facts.tipoPago}.</p> : null}
          {deliverables.length ? <ul>{deliverables.map((deliverable, index) => <li key={index}>{text(deliverable.product) || text(deliverable.description) || text(deliverable.item) || `Entregable ${index + 1}`}
            {deliverable.presentation_deadline ? ` · ${text(deliverable.presentation_deadline)}` : ""}</li>)}</ul> : <p className={styles.muted}>Consulta los plazos, entregables y condiciones de pago en las bases oficiales.</p>}
          {process.item_states?.length ? <p className={styles.muted}>Estados publicados de los ítems: {process.item_states.join(" · ")}. No equivalen por sí solos a una convocatoria abierta.</p> : null}
        </section></div>
      </DetailSectionNavigator>
      <aside className={styles.rail} aria-label="Contexto del procedimiento">
        <section className={styles.railPanel}><h2>Brechas de capacidad</h2>{match ? missing.length ? <>
          {missing.slice(0, 3).map(renderGap)}
          {missing.length > 3 ? <details className={styles.disclosure}><summary>Ver {missing.length - 3} brecha{missing.length - 3 === 1 ? "" : "s"} más<ChevronDown aria-hidden="true" size={16} /></summary>{missing.slice(3).map(renderGap)}</details> : null}
        </> : <p>Sin brechas indicadas en esta evaluación. Verifica la modalidad y la documentación antes de participar.</p> : <p>Evaluación documental pendiente.</p>}</section>
        <section className={styles.railPanel}><h2>Información oficial</h2><dl className={styles.facts}>
          <div><dt>Registro de participantes</dt><dd>{stageDate(registration)}</dd></div>
          <div><dt>Presentación de ofertas</dt><dd>{stageDate(proposals)}</dd></div>
          <div><dt>Cuantía publicada</dt><dd>{process.reference_amount != null ? new Intl.NumberFormat("es-PE", { style: "currency", currency: process.currency === "USD" ? "USD" : "PEN" }).format(Number(process.reference_amount)) : "No informada"}</dd></div>
          <div><dt>Ficha consultada</dt><dd>{process.detail_fetched_at ? formatDateTime(process.detail_fetched_at) : "No informado"}</dd></div>
        </dl><p className={styles.muted}>Fuente: buscador público del SEACE, PROD2. El cronograma no acredita adjudicación ni elegibilidad.</p></section>
      </aside>
    </div>
  </>;
}
