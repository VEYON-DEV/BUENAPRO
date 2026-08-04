import Link from "next/link";
import { AppShell } from "@/features/shell";
import { AppIcon } from "@/components/ui/AppIcon";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { formatDateTime, formatDeadline } from "@/lib/format/date";
import { formatMoney } from "@/lib/format/money";
import {
  cotizacionStatus,
  fitLabel,
  fitScore,
  opportunityFacts,
  plazoLabel,
  verdictShortLabels,
} from "@/lib/extraction/opportunity";
import { getContractForTenant } from "@/server/services/contracts";
import { getHistoricalComparables } from "@/server/services/historicalComparables";
import { refreshContractDetailIfStale } from "@/server/services/seaceDetail";
import { MatchEvents } from "@/features/tracking";
import { AnalyzeButton } from "../../components/AnalyzeButton";
import { DecisionOverview } from "../../components/DecisionOverview";
import { DetailSectionNavigator } from "../../components/DetailSectionNavigator";
import { PdfPreview } from "../../components/PdfPreview/PdfPreview";
import { SeaceWorkspace } from "../../components/SeaceWorkspace";
import { StartApplicationButton } from "../../components/StartApplicationButton";
import { HistoricalComparables } from "../../components/HistoricalComparables";
import { CopilotPanel } from "@/features/copilot";
import styles from "./OpportunityDetailPage.module.css";

type AnyRecord = Record<string, any>;

function tone(verdict?: string | null): "green" | "amber" | "red" | "neutral" {
  if (verdict === "verde") return "green";
  if (verdict === "ambar") return "amber";
  if (verdict === "rojo") return "red";
  return "neutral";
}

function statusTone(status?: string | null) {
  if (status === "cumple") return "green";
  if (status === "cumple_con_accion") return "amber";
  if (status === "no_cumple") return "red";
  return "neutral";
}

const statusLabels: Record<string, string> = {
  cumple: "Cumple",
  cumple_con_accion: "Accionable",
  no_cumple: "No cumple",
  requiere_revision: "Revisar",
};

const facetGroupLabels: Record<string, string> = {
  legal_capacity: "Capacidad legal",
  ruc_status: "RUC",
  rnp: "RNP",
  economic_experience: "Experiencia económica",
  general_experience: "Experiencia general",
  specific_experience: "Experiencia específica",
  key_personnel: "Personal clave",
  education: "Formación académica",
  professional_registration: "Colegiatura",
  training: "Capacitación",
  license: "Licencias",
  equipment: "Equipamiento",
  insurance: "Seguros",
  company_certification: "Certificaciones",
  proposal_document: "Documentos de propuesta",
  payment_condition: "Condiciones de pago",
  penalty_condition: "Penalidades",
  delivery_condition: "Condiciones de entrega",
  other: "Otros requisitos",
};

const facetGroupOrder = Object.keys(facetGroupLabels);

const docClassLabels: Record<string, string> = {
  tdr: "TDR / Requerimiento",
  eett: "Especificaciones técnicas",
  anexo: "Anexo",
  cotizacion: "Formato de cotización",
  otro: "Documento",
};

function formatFileSize(bytes: number | string): string {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value >= 1_048_576) return `${(value / 1_048_576).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(value / 1024))} KB`;
}

function asArray<T = any>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter(Boolean) as T[]) : [];
}

function textValue(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number") return String(value);
  return "";
}

function facetText(facet: AnyRecord): string {
  const details = facet.details_json ?? facet.details ?? {};
  const direct =
    textValue(details.value) ||
    textValue(details.description) ||
    textValue(details.texto);
  if (direct) return direct;
  const parts: string[] = [];
  for (const [key, value] of Object.entries(details)) {
    if (key === "evidence" || key === "required") continue;
    const text = textValue(value);
    if (text) parts.push(text);
    else if (Array.isArray(value))
      parts.push(...value.map(textValue).filter(Boolean));
  }
  return parts.join(" · ");
}

function groupItems(items: AnyRecord[] = []) {
  const map = new Map<string, AnyRecord & { count: number }>();
  for (const item of items) {
    const key = `${item.facet ?? "otro"}:${item.label ?? "Requisito"}:${item.estado ?? ""}`;
    const current = map.get(key);
    if (current) {
      current.count += 1;
      continue;
    }
    map.set(key, { ...item, count: 1 });
  }
  return Array.from(map.values());
}

function penaltyHighlights(extraction: AnyRecord): string[] {
  const penalties = extraction.penalties ?? {};
  const entries: AnyRecord[] = Array.isArray(penalties)
    ? penalties
    : [penalties];
  const items: string[] = [];
  for (const entry of entries) {
    if (!entry || typeof entry !== "object") continue;
    for (const [, value] of Object.entries(entry)) {
      if (value == null) continue;
      if (typeof value === "string") {
        items.push(value);
        continue;
      }
      if (Array.isArray(value)) {
        for (const sub of value) {
          if (typeof sub === "string") items.push(sub);
          else if (sub && typeof sub === "object") {
            const text = [
              sub.application ?? sub.description,
              sub.calculation_formula ?? sub.formula,
            ]
              .filter(Boolean)
              .join(": ");
            if (text) items.push(text);
          }
        }
        continue;
      }
      if (typeof value === "object") {
        const record = value as AnyRecord;
        const formula =
          record.formula ?? record.calculation_formula ?? record.daily_penalty;
        const cap = record.cap ?? record.tope;
        const text = [
          formula ? `Fórmula: ${formula}` : "",
          cap ? `Tope: ${cap}` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        if (text) items.push(text);
      }
    }
  }
  return [...new Set(items)]
    .filter((item) => !/no\s+(aplica|corresponde)/i.test(item))
    .slice(0, 6);
}

function dedupeDeliverables(deliverables: AnyRecord[]) {
  const map = new Map<
    string,
    { text: string; deadline: string; count: number }
  >();
  deliverables.forEach((deliverable, index) => {
    const text =
      textValue(
        deliverable.product ?? deliverable.item ?? deliverable.description,
      ) || `Entregable ${index + 1}`;
    const deadline = textValue(
      deliverable.presentation_deadline ??
        deliverable.deadline ??
        deliverable.plazo,
    );
    const key = `${text}:${deadline}`;
    const current = map.get(key);
    if (current) current.count += 1;
    else map.set(key, { text, deadline, count: 1 });
  });
  return Array.from(map.values());
}

function stages(rawDetail: AnyRecord, contract: AnyRecord) {
  return asArray<AnyRecord>(
    rawDetail.uitContratoEtapaProjectionList ?? contract.cronograma?.etapas,
  );
}

function DataRow({ label, value }: { label: string; value?: React.ReactNode }) {
  if (value == null || value === "") return null;
  return (
    <div className={styles.dataRow}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export async function OpportunityDetailPage({
  tenantId,
  idContrato,
}: {
  tenantId: string;
  idContrato: number;
}) {
  await refreshContractDetailIfStale(idContrato);
  const data = await getContractForTenant(tenantId, idContrato);
  if (!data) {
    return (
      <AppShell title="Oportunidad">
        <EmptyState
          title="Oportunidad no encontrada"
          action={{ label: "Volver al feed", href: "/feed" }}
        />
      </AppShell>
    );
  }

  const history = await getHistoricalComparables(tenantId, idContrato);

  const { contract, facets, documents } = data as any;
  const facts = opportunityFacts(contract);
  const extraction: AnyRecord = contract.raw_extraction_json ?? {};
  const rawDetail: AnyRecord = contract.raw_detail_json ?? {};
  const projection: AnyRecord = rawDetail.uitContratoCompletoProjection ?? {};
  const item: AnyRecord =
    asArray<AnyRecord>(rawDetail.uitContratoItemProjectionList)[0] ?? {};
  const matchId = contract.match_id;
  const analysis =
    typeof contract.breakdown_json === "object" &&
    !Array.isArray(contract.breakdown_json)
      ? contract.breakdown_json
      : null;
  const analyzedAt: string | null = analysis?.meta?.analyzed_at ?? null;
  const missingActions = groupItems(contract.missing_actions_json ?? []).filter(
    (item: AnyRecord) => item.label || item.accion,
  );
  const mainGap =
    missingActions.find((item: AnyRecord) => item.critico) ?? missingActions[0];
  const deliverables = asArray<AnyRecord>(
    extraction.execution?.deliverables ?? extraction.execution?.entregables,
  );
  const penalties = penaltyHighlights(extraction);
  const etapaList = stages(rawDetail, contract);
  const pdfDocuments = documents.filter(
    (doc: any) =>
      doc.mime === "application/pdf" ||
      String(doc.filename ?? "")
        .toLowerCase()
        .endsWith(".pdf"),
  );
  const primaryDoc =
    pdfDocuments.find((doc: any) => doc.doc_class === "tdr") ?? pdfDocuments[0];
  const ubicacion = [
    contract.departamento,
    contract.provincia,
    contract.distrito,
  ]
    .filter(Boolean)
    .join(", ");

  const groupedFacets = new Map<string, AnyRecord[]>();
  for (const facet of groupItems(facets)) {
    const key = facetGroupLabels[facet.facet] ? facet.facet : "other";
    const bucket = groupedFacets.get(key) ?? [];
    bucket.push(facet);
    groupedFacets.set(key, bucket);
  }
  const orderedFacetGroups = facetGroupOrder.filter((key) =>
    groupedFacets.has(key),
  );

  return (
    <AppShell title="Detalle">
      <CopilotPanel
        contractId={contract.id_contrato}
        matchId={matchId ? String(matchId) : undefined}
      />
      <nav className={styles.breadcrumb} aria-label="Ruta">
        <Link href="/feed">Oportunidades</Link>
        <span>/</span>
        <span>{contract.codigo}</span>
      </nav>

      <header className={styles.header}>
        <div className={styles.headerMain}>
          <span className={styles.eyebrow}>
            Contratación menor · {contract.objeto_nombre ?? "Oportunidad"}
          </span>
          <div className={styles.codeLine}>
            <h1>{contract.codigo}</h1>
            <Badge tone="sage">{contract.estado_nombre ?? "Vigente"}</Badge>
            {(() => {
              const cotizacion = cotizacionStatus(contract);
              const tones: Record<string, "green" | "amber" | "neutral"> = {
                abierta: "green",
                por_abrir: "amber",
                cerrada: "neutral",
                no_disponible: "neutral",
              };
              return (
                <Badge tone={tones[cotizacion.key]}>{cotizacion.label}</Badge>
              );
            })()}
          </div>
          <p className={styles.title}>{contract.descripcion}</p>
          <div className={styles.metaLine}>
            <span>
              <AppIcon name="building" />
              {contract.entidad_nombre}
            </span>
            {contract.objeto_nombre ? (
              <span>
                <AppIcon name="tag" />
                {contract.objeto_nombre}
              </span>
            ) : null}
            {ubicacion ? (
              <span>
                <AppIcon name="pin" />
                {ubicacion}
              </span>
            ) : null}
          </div>
        </div>
      </header>

      <DecisionOverview
        analyzed={Boolean(contract.verdict && contract.score != null)}
        score={contract.score}
        verdict={contract.verdict ? (verdictShortLabels[contract.verdict] ?? contract.verdict) : "Evaluación pendiente"}
        verdictTone={tone(contract.verdict)}
        fit={fitLabel(contract.fit_points)}
        deadline={formatDeadline(contract.fec_fin_cotizacion)}
        deadlineDate={formatDateTime(contract.fec_fin_cotizacion)}
        requirementsTotal={facets.length}
        missingCount={missingActions.length}
        marketMedian={history?.metrics?.precio_median != null ? formatMoney(history.metrics.precio_median) : null}
        marketRange={history?.metrics?.rango_frecuente ? `${formatMoney(history.metrics.rango_frecuente.min)} – ${formatMoney(history.metrics.rango_frecuente.max)}` : null}
        actions={
          <>
            <StartApplicationButton
              idContrato={contract.id_contrato}
              existing={Boolean(contract.application_id)}
              disabled={cotizacionStatus(contract).key !== "abierta" && contract.user_state !== "en_preparacion"}
            />
            {primaryDoc ? (
              <a href={`/api/contracts/${contract.id_contrato}/original/${primaryDoc.id}`}>
                <Button variant="secondary">Descargar TDR</Button>
              </a>
            ) : null}
          </>
        }
      />

      {!contract.verdict ? (
        <div className={styles.analyzeBar}>
          <div>
            <strong>¿Puedes postular a esta oportunidad?</strong>
            <span>
              Evalúa los requisitos del TDR contra tu perfil de empresa:
              veredicto, brechas exactas y acciones.
            </span>
          </div>
          <AnalyzeButton
            idContrato={contract.id_contrato}
            analyzedAt={analyzedAt}
          />
        </div>
      ) : null}

      <div className={styles.layout}>
        <DetailSectionNavigator
          tabs={[
            {
              id: "decision",
              label: "Decisión y requisitos",
              count: facets.length,
            },
            { id: "documents", label: "Documentos", count: documents.length },
            { id: "history", label: "Histórico", count: history?.metrics?.total_count },
            { id: "execution", label: "Ejecución" },
          ]}
        >
          <div data-detail-pane="decision" id="detail-pane-decision" role="tabpanel" aria-labelledby="detail-tab-decision">
          {analysis?.resumen || facts.lecturaRapida ? (
            <section className={`${styles.panel} ${styles.decisionBrief}`} id="decision">
              <div className={styles.sectionTitle}>
                <div>
                  <span className={styles.sectionKicker}>
                    Lectura ejecutiva
                  </span>
                  <h2>Lo esencial para decidir</h2>
                </div>
                {analysis?.resumen ? (
                  <AnalyzeButton
                    idContrato={contract.id_contrato}
                    analyzedAt={analyzedAt}
                  >
                    Re-evaluar
                  </AnalyzeButton>
                ) : null}
              </div>
              <div className={styles.briefGrid}>
                {analysis?.resumen ? (
                  <div className={styles.briefPrimary}>
                    <span className={styles.briefLabel}>Según tu perfil</span>
                    <p className={styles.executive}>{analysis.resumen}</p>
                    {Array.isArray(analysis.acciones_recomendadas) &&
                    analysis.acciones_recomendadas.length ? (
                      <ol className={styles.recommendedActions}>
                        {analysis.acciones_recomendadas.map(
                          (accion: string) => (
                            <li key={accion}>{accion}</li>
                          ),
                        )}
                      </ol>
                    ) : null}
                  </div>
                ) : null}
                {facts.lecturaRapida ? (
                  <div className={styles.briefSecondary}>
                    <span className={styles.briefLabel}>
                      Qué solicita la entidad
                    </span>
                    <p className={styles.executive}>{facts.lecturaRapida}</p>
                    {facts.observaciones.length ? (
                      <ul className={styles.observations}>
                        {facts.observaciones.map((obs) => (
                          <li key={obs}>{obs}</li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </section>
          ) : null}
          <section className={styles.panel} id="requisitos">
            <div className={styles.sectionTitle}>
              <div>
                <span className={styles.sectionKicker}>
                  Validación documental
                </span>
                <h2>Requisitos para postular</h2>
              </div>
              <span>{facets.length} detectados</span>
            </div>
            {orderedFacetGroups.length ? (
              <div className={styles.facetGroups}>
                {orderedFacetGroups.map((groupKey) => {
                  const items = groupedFacets.get(groupKey)!;
                  return (
                    <details
                      className={styles.facetGroup}
                      key={groupKey}
                    >
                      <summary>
                        <strong>{facetGroupLabels[groupKey]}</strong>
                        <span>{items.length}</span>
                      </summary>
                      <ul>
                        {items.map((facet: AnyRecord, index: number) => {
                          const text = facetText(facet);
                          return (
                            <li key={facet.id ?? `${groupKey}-${index}`}>
                              <span className={styles.facetLabel}>
                                {facet.label}
                                {facet.count > 1 ? ` ×${facet.count}` : ""}
                              </span>
                              {text && text !== facet.label ? (
                                <span className={styles.facetDetail}>
                                  {text}
                                </span>
                              ) : null}
                            </li>
                          );
                        })}
                      </ul>
                    </details>
                  );
                })}
              </div>
            ) : (
              <EmptyState title="Sin requisitos extraídos">
                La extracción IA aún no procesa este TDR.
              </EmptyState>
            )}
          </section>
          </div>

          <div data-detail-pane="execution" id="detail-pane-execution" role="tabpanel" aria-labelledby="detail-tab-execution">
          <details className={`${styles.panel} ${styles.technicalPanel}`}>
            <summary>
              <div>
                <span className={styles.sectionKicker}>
                  Información de referencia
                </span>
                <strong>Ficha técnica del contrato</strong>
              </div>
              <span>Ver detalle</span>
            </summary>
            <dl className={styles.dataColumns}>
              <DataRow label="Área usuaria" value={projection.nomAreaUsuaria} />
              <DataRow
                label="Invitación"
                value={projection.nomTipoInvitacion}
              />
              <DataRow
                label="CUBSO"
                value={
                  [item.codCubso, item.nomCubso].filter(Boolean).join(" · ") ||
                  null
                }
              />
              <DataRow
                label="Objeto específico"
                value={item.descripcionItem ?? extraction.contract?.objective}
              />
              <DataRow
                label="Lugar de ejecución"
                value={
                  extraction.contract?.location ??
                  extraction.contract?.execution_location ??
                  item.nomDistritoExt
                }
              />
              <DataRow label="Plazo" value={plazoLabel(facts.plazoDias)} />
              <DataRow label="Pago" value={facts.tipoPago} />
              <DataRow
                label="Roles requeridos"
                value={facts.roles.length ? facts.roles.join(", ") : null}
              />
            </dl>
          </details>

          {deliverables.length ? (
            <details className={`${styles.panel} ${styles.foldPanel}`}>
              <summary><span><strong>Entregables</strong><small>Productos y fechas de presentación</small></span><b>{deliverables.length}</b></summary>
              <div className={styles.foldBody}>
                <ol className={styles.deliverables}>
                  {dedupeDeliverables(deliverables).map((deliverable, index) => (
                    <li key={index}>
                      <strong>{deliverable.text}{deliverable.count > 1 ? ` (×${deliverable.count})` : ""}</strong>
                      {deliverable.deadline ? <span>{deliverable.deadline}</span> : null}
                    </li>
                  ))}
                </ol>
              </div>
            </details>
          ) : null}

          {penalties.length ? (
            <details className={`${styles.panel} ${styles.foldPanel}`}>
              <summary><span><strong>Penalidades y riesgos</strong><small>Condiciones que pueden afectar la ejecución</small></span>{facts.penalidadTopePct != null ? <b>Tope {facts.penalidadTopePct}%</b> : null}</summary>
              <div className={styles.foldBody}><ul className={styles.penalties}>{penalties.map((penalty) => <li key={penalty}>{penalty}</li>)}</ul></div>
            </details>
          ) : null}

          {etapaList.length ? (
            <details className={`${styles.panel} ${styles.foldPanel}`}>
              <summary><span><strong>Cronograma SEACE</strong><small>Etapas oficiales del proceso</small></span><b>{etapaList.length} etapas</b></summary>
              <div className={styles.foldBody}><div className={styles.timeline}>{etapaList.map((stage, index) => <div className={styles.stage} key={`${stage.nomEtapaContrato}-${index}`}><strong>{stage.nomEtapaContrato}</strong><span>{stage.fecIni} — {stage.fecFin}</span></div>)}</div></div>
            </details>
          ) : null}
          </div>

          <div data-detail-pane="documents" id="detail-pane-documents" role="tabpanel" aria-labelledby="detail-tab-documents">
          <details className={`${styles.panel} ${styles.foldPanel}`} id="documentos" open>
            <summary><span><strong>Documentos del proceso</strong><small>TDR, anexos y formatos publicados</small></span><b>{documents.length}</b></summary>
            <div className={styles.foldBody}>{documents.length ? (
              <div className={styles.docs}>
                {documents.map((doc: any) => (
                  <div className={styles.doc} key={doc.id}>
                    <span className={styles.docIcon}>
                      <AppIcon name="doc" />
                    </span>
                    <div>
                      <strong>{doc.filename}</strong>
                      <span>
                        {docClassLabels[doc.doc_class] ?? "Documento"}
                        {doc.size_original_bytes
                          ? ` · ${formatFileSize(doc.size_original_bytes)}`
                          : ""}
                      </span>
                    </div>
                    <a
                      href={`/api/contracts/${contract.id_contrato}/original/${doc.id}`}
                    >
                      <Button variant="secondary">Descargar</Button>
                    </a>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title="Sin documentos">
                SEACE no expone archivos para esta contratación.
              </EmptyState>
            )}</div>
          </details>

          <details className={`${styles.panel} ${styles.foldPanel}`}>
            <summary><span><strong>Consultas oficiales SEACE</strong><small>Preguntas y datos privados del proceso</small></span><b>Conectar</b></summary>
            <div className={styles.foldBody}>
              <SeaceWorkspace
                idContrato={contract.id_contrato}
                canQuote={cotizacionStatus(contract).key === "abierta"}
              />
            </div>
          </details>
          </div>

          <div data-detail-pane="history" id="detail-pane-history" role="tabpanel" aria-labelledby="detail-tab-history">
          <HistoricalComparables history={history} />
          <details className={`${styles.panel} ${styles.foldPanel}`} id="historial">
            <summary><span><strong>Actividad y comentarios</strong><small>Decisiones y avances de la postulación</small></span><b>Ver</b></summary>
            <div className={styles.foldBody}>{matchId ? <MatchEvents matchId={matchId} /> : <p className={styles.mutedNote}>Al iniciar la preparación podrás registrar decisiones, avances y comentarios del equipo.</p>}</div>
          </details>
          </div>
        </DetailSectionNavigator>

        <aside className={styles.rail}>
          {missingActions.length ? (
            <section className={styles.panel}>
              <div className={styles.sectionTitle}>
                <h2>Qué te falta</h2>
                <span>{missingActions.length}</span>
              </div>
              <ul className={styles.actionList}>
                {missingActions.slice(0, 3).map((action: AnyRecord, index: number) => (
                  <li key={`${action.facet}-${index}`}>
                    <Badge tone={statusTone(action.estado)}>
                      {statusLabels[action.estado] ?? action.estado}
                    </Badge>
                    <div>
                      <strong>
                        {action.label ?? action.facet ?? "Requisito"}
                      </strong>
                      {action.gap ? <span>{action.gap}</span> : null}
                      {action.accion ? (
                        <span className={styles.actionHint}>
                          → {action.accion}
                        </span>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
              {missingActions.length > 3 ? (
                <details className={styles.moreGaps}>
                  <summary>Ver {missingActions.length - 3} brecha{missingActions.length - 3 === 1 ? "" : "s"} más</summary>
                  <ul className={styles.actionList}>
                    {missingActions.slice(3).map((action: AnyRecord, index: number) => (
                      <li key={`${action.facet}-more-${index}`}>
                        <Badge tone={statusTone(action.estado)}>{statusLabels[action.estado] ?? action.estado}</Badge>
                        <div><strong>{action.label ?? action.facet ?? "Requisito"}</strong>{action.gap ? <span>{action.gap}</span> : null}{action.accion ? <span className={styles.actionHint}>→ {action.accion}</span> : null}</div>
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </section>
          ) : null}

          <section className={styles.panel}>
            <div className={styles.sectionTitle}>
              <h2>TDR</h2>
              {primaryDoc ? (
                <a
                  className={styles.pdfOpen}
                  href={`/api/contracts/${contract.id_contrato}/original/${primaryDoc.id}`}
                >
                  Abrir original
                </a>
              ) : null}
            </div>
            {primaryDoc ? (
              <PdfPreview
                src={`/api/contracts/${contract.id_contrato}/preview-image/${primaryDoc.id}`}
                title={primaryDoc.filename}
                downloadHref={`/api/contracts/${contract.id_contrato}/original/${primaryDoc.id}`}
                previewHref={`/api/contracts/${contract.id_contrato}/preview/${primaryDoc.id}`}
              />
            ) : (
              <p className={styles.mutedNote}>No hay documento asociado.</p>
            )}
          </section>
        </aside>
      </div>
    </AppShell>
  );
}
