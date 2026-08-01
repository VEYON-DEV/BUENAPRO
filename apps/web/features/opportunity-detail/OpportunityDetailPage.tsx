import Link from "next/link";
import { AppShell } from "@/features/shell/components/AppShell";
import { AppIcon } from "@/components/ui/AppIcon";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Tabs } from "@/components/ui/Tabs";
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
import { MatchEvents } from "@/features/tracking/components/MatchEvents";
import { AnalyzeButton } from "./components/AnalyzeButton";
import { PdfPreview } from "./components/PdfPreview/PdfPreview";
import { SeaceWorkspace } from "./components/SeaceWorkspace";
import { StartApplicationButton } from "./components/StartApplicationButton";
import { HistoricalComparables } from "./components/HistoricalComparables";
import { CopilotPanel } from "@/features/copilot";
import styles from "./OpportunityDetailPage.module.css";

type AnyRecord = Record<string, any>;

function tone(verdict?: string | null) {
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

function InfoTile({
  label,
  value,
  detail,
  tone: tileTone,
}: {
  label: string;
  value: React.ReactNode;
  detail?: string;
  tone?: string;
}) {
  return (
    <div
      className={[styles.infoTile, tileTone ? styles[`tile_${tileTone}`] : ""]
        .filter(Boolean)
        .join(" ")}
    >
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
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
        <div className={styles.headerActions}>
          <span className={styles.deadlineChip}>
            <AppIcon name="clock" />
            {formatDeadline(contract.fec_fin_cotizacion)} ·{" "}
            {formatDateTime(contract.fec_fin_cotizacion)}
          </span>
          <div className={styles.headerButtons}>
            {primaryDoc ? (
              <a
                href={`/api/contracts/${contract.id_contrato}/original/${primaryDoc.id}`}
              >
                <Button variant="secondary">Descargar TDR</Button>
              </a>
            ) : null}
            <StartApplicationButton
              idContrato={contract.id_contrato}
              existing={Boolean(contract.application_id)}
              disabled={
                cotizacionStatus(contract).key !== "abierta" &&
                contract.user_state !== "en_preparacion"
              }
            />
          </div>
        </div>
      </header>

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

      <section className={styles.decisionStrip} id="resumen">
        {contract.verdict ? (
          <>
            <InfoTile
              label="Veredicto"
              tone={tone(contract.verdict)}
              value={verdictShortLabels[contract.verdict] ?? contract.verdict}
              detail={mainGap ? "Con acción pendiente" : "Según tu perfil"}
            />
            <InfoTile
              label="Score"
              value={`${contract.score}/100`}
              detail={`Afinidad de rubro: ${fitLabel(contract.fit_points)}`}
            />
            <InfoTile
              label="Falta"
              value={
                mainGap ? (mainGap.label ?? mainGap.facet) : "Nada crítico"
              }
              detail={mainGap?.accion ?? undefined}
            />
            <InfoTile
              label="Exp. económica exigida"
              value={
                contract.econ_exigido != null
                  ? formatMoney(Number(contract.econ_exigido))
                  : "No exigida"
              }
              detail={
                contract.econ_exigido != null
                  ? "Acreditable; un consorcio puede cubrirla"
                  : "El TDR no pide monto"
              }
            />
          </>
        ) : (
          <>
            <InfoTile
              label="Afinidad de rubro"
              value={fitLabel(contract.fit_points)}
              detail="Por segmento y keywords · el score sale al evaluar con IA"
            />
            <InfoTile
              label="Exp. económica exigida"
              value={
                contract.econ_exigido != null
                  ? formatMoney(Number(contract.econ_exigido))
                  : "No exigida"
              }
              detail={
                contract.econ_exigido != null
                  ? "Se compara con tu facturación"
                  : "El TDR no pide monto"
              }
            />
            <InfoTile
              label="Pago"
              value={facts.tipoPago ?? "Por definir"}
              detail={
                facts.entregables
                  ? `${facts.entregables} ${facts.entregables === 1 ? "entregable" : "entregables"}`
                  : undefined
              }
            />
            <InfoTile
              label="Plazo"
              value={plazoLabel(facts.plazoDias) ?? "Por definir"}
              detail="Ejecución del servicio"
            />
          </>
        )}
      </section>

      <Tabs
        items={[
          { key: "resumen", label: "Decisión", href: "#resumen", active: true },
          { key: "comparables", label: "Histórico", href: "#comparables" },
          {
            key: "requisitos",
            label: `Requisitos (${facets.length})`,
            href: "#requisitos",
          },
          { key: "documentos", label: "Documentos", href: "#documentos" },
          {
            key: "seace",
            label: "Consultas oficiales",
            href: "#seace-workspace",
          },
          { key: "seguimiento", label: "Preparación", href: "#seguimiento" },
          { key: "historial", label: "Actividad", href: "#historial" },
        ]}
      />

      <div className={styles.layout}>
        <div className={styles.main}>
          {analysis?.resumen || facts.lecturaRapida ? (
            <section className={`${styles.panel} ${styles.decisionBrief}`}>
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

          <HistoricalComparables history={history} />

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
                      open={items.length <= 6}
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
            <section className={styles.panel}>
              <div className={styles.sectionTitle}>
                <h2>Entregables</h2>
                <span>{deliverables.length}</span>
              </div>
              <ol className={styles.deliverables}>
                {dedupeDeliverables(deliverables).map((deliverable, index) => (
                  <li key={index}>
                    <strong>
                      {deliverable.text}
                      {deliverable.count > 1 ? ` (×${deliverable.count})` : ""}
                    </strong>
                    {deliverable.deadline ? (
                      <span>{deliverable.deadline}</span>
                    ) : null}
                  </li>
                ))}
              </ol>
            </section>
          ) : null}

          {penalties.length ? (
            <section className={styles.panel}>
              <div className={styles.sectionTitle}>
                <h2>Penalidades y riesgos</h2>
                {facts.penalidadTopePct != null ? (
                  <span>Tope {facts.penalidadTopePct}%</span>
                ) : null}
              </div>
              <ul className={styles.penalties}>
                {penalties.map((penalty) => (
                  <li key={penalty}>{penalty}</li>
                ))}
              </ul>
            </section>
          ) : null}

          {etapaList.length ? (
            <section className={styles.panel}>
              <div className={styles.sectionTitle}>
                <h2>Cronograma SEACE</h2>
                <span>{etapaList.length} etapas</span>
              </div>
              <div className={styles.timeline}>
                {etapaList.map((stage, index) => (
                  <div
                    className={styles.stage}
                    key={`${stage.nomEtapaContrato}-${index}`}
                  >
                    <strong>{stage.nomEtapaContrato}</strong>
                    <span>
                      {stage.fecIni} — {stage.fecFin}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ) : null}

          <section className={styles.panel} id="documentos">
            <div className={styles.sectionTitle}>
              <h2>Documentos</h2>
              <span>{documents.length}</span>
            </div>
            {documents.length ? (
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
            )}
          </section>

          <SeaceWorkspace
            idContrato={contract.id_contrato}
            canQuote={cotizacionStatus(contract).key === "abierta"}
          />

          <section className={styles.panel} id="historial">
            <div className={styles.sectionTitle}>
              <div>
                <span className={styles.sectionKicker}>Colaboración</span>
                <h2>Actividad y comentarios</h2>
              </div>
            </div>
            {matchId ? (
              <MatchEvents matchId={matchId} />
            ) : (
              <p className={styles.mutedNote}>
                Al iniciar la preparación podrás registrar decisiones, avances y
                comentarios del equipo.
              </p>
            )}
          </section>
        </div>

        <aside className={styles.rail}>
          {missingActions.length ? (
            <section className={styles.panel}>
              <div className={styles.sectionTitle}>
                <h2>Qué te falta</h2>
                <span>{missingActions.length}</span>
              </div>
              <ul className={styles.actionList}>
                {missingActions.map((action: AnyRecord, index: number) => (
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
