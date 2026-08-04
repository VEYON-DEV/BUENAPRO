import Link from "next/link";
import { AlertTriangle, ArrowRight, Bell, Building2, Clock3, MapPin, Sparkles } from "lucide-react";
import { formatDateTime, formatDeadline, formatShortDateTime } from "@/lib/format/date";
import type { AlertItem } from "../../model/types";
import styles from "./AlertInbox.module.css";

function reasonLabel(reason: string) {
  if (reason === "verdict_change") return "Evaluación actualizada";
  return "Nueva oportunidad evaluada";
}

function verdictMeta(verdict?: string | null) {
  if (verdict === "verde") return { label: "Sí conviene", tone: "success" } as const;
  if (verdict === "ambar") return { label: "Vale la pena revisarla", tone: "warning" } as const;
  if (verdict === "rojo") return { label: "No conviene", tone: "danger" } as const;
  return { label: "Requiere revisión", tone: "neutral" } as const;
}

function scoreOf(alert: AlertItem) {
  const score = alert.score ?? alert.payload?.score;
  return score == null ? null : Math.round(Number(score));
}

function verdictOf(alert: AlertItem) {
  return alert.verdict ?? alert.payload?.verdict ?? alert.payload?.veredicto ?? "gris";
}

function deadlineOf(alert: AlertItem) {
  return alert.payload?.deadline ?? alert.payload?.cierre ?? alert.fec_fin_cotizacion;
}

function opportunityHref(alert: AlertItem) {
  const contractId = alert.id_contrato ?? alert.payload?.id_contrato;
  if (contractId) return `/oportunidad/${contractId}`;
  const ctaHref = typeof alert.payload?.cta === "string" ? alert.payload.cta : alert.payload?.cta?.url;
  const payloadHref = alert.payload?.cta_url ?? ctaHref ?? alert.payload?.url;
  if (!payloadHref) return null;
  if (payloadHref.startsWith("/") && !payloadHref.startsWith("//")) return payloadHref;
  try {
    const parsed = new URL(payloadHref);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

function ctaLabel(alert: AlertItem) {
  if (typeof alert.payload?.cta === "object" && alert.payload.cta?.label) return alert.payload.cta.label;
  return "Ver oportunidad";
}

export function AlertInbox({ alerts }: { alerts: AlertItem[] }) {
  if (!alerts.length) {
    return (
      <section className={styles.empty} aria-labelledby="empty-alerts-title">
        <span aria-hidden="true"><Bell size={23} /></span>
        <div>
          <h2 id="empty-alerts-title">Tu bandeja está al día</h2>
          <p>Las oportunidades con evaluación superior al umbral configurado aparecerán aquí.</p>
          <Link href="/configuracion">Revisar configuración <ArrowRight size={15} /></Link>
        </div>
      </section>
    );
  }

  const [featured, ...recent] = alerts;
  const featuredScore = scoreOf(featured);
  const featuredVerdict = verdictMeta(verdictOf(featured));
  const featuredHref = opportunityHref(featured);
  const strengths = Array.isArray(featured.payload?.strengths) ? featured.payload.strengths.slice(0, 2) : [];
  const featuredTitle = featured.descripcion ?? featured.payload?.summary ?? "Oportunidad evaluada";
  const featuredSummary = featured.payload?.summary === featuredTitle ? null : featured.payload?.summary;

  return (
    <div className={styles.layout}>
      <section className={styles.featured} aria-labelledby="featured-alert-title">
        <div className={styles.featuredTop}>
          <div className={styles.featuredLabel}>
            <span aria-hidden="true"><Sparkles size={18} /></span>
            <div>
              <small>{reasonLabel(featured.reason)} · {formatShortDateTime(featured.created_at)}</small>
              <strong className={styles[featuredVerdict.tone]}>{featuredVerdict.label}</strong>
            </div>
          </div>
          {featuredScore != null ? (
            <div className={styles.score} aria-label={`Puntaje ${featuredScore} de 100`}>
              <strong>{featuredScore}</strong><span>/100</span>
            </div>
          ) : null}
        </div>

        <div className={styles.featuredCopy}>
          <span className={styles.code}>{featured.codigo ?? "Oportunidad SEACE"}</span>
          <h2 id="featured-alert-title">{featuredTitle}</h2>
          <p>{featuredSummary || "Abre la oportunidad para revisar el resultado completo de la evaluación."}</p>
        </div>

        <dl className={styles.facts}>
          <div><dt><Building2 size={15} /> Entidad</dt><dd>{featured.entidad_nombre ?? "No indicada"}</dd></div>
          <div><dt><MapPin size={15} /> Modalidad</dt><dd>{featured.payload?.modality || "No indicada"}</dd></div>
          <div><dt><Clock3 size={15} /> Cierre</dt><dd>{formatDateTime(deadlineOf(featured))}</dd></div>
        </dl>

        {(strengths.length || featured.payload?.main_risk) ? (
          <div className={styles.evidence}>
            {strengths.length ? (
              <div>
                <strong>A favor</strong>
                <ul>{strengths.map((strength) => <li key={strength}>{strength}</li>)}</ul>
              </div>
            ) : null}
            {featured.payload?.main_risk ? (
              <div className={styles.risk}>
                <strong><AlertTriangle size={15} /> Revisar</strong>
                <p>{featured.payload.main_risk}</p>
              </div>
            ) : null}
          </div>
        ) : null}

        <footer className={styles.featuredFooter}>
          <span>{formatDeadline(deadlineOf(featured))}</span>
          {featuredHref ? <Link href={featuredHref}>{ctaLabel(featured)} <ArrowRight size={16} /></Link> : null}
        </footer>
      </section>

      <section className={styles.history} aria-labelledby="alert-history-title">
        <header className={styles.historyHeader}>
          <div>
            <h2 id="alert-history-title">Notificaciones recientes</h2>
            <p>Evaluaciones que superaron tu puntaje mínimo.</p>
          </div>
          <span>{alerts.length} en la bandeja</span>
        </header>

        {recent.length ? (
          <div className={styles.list}>
            {recent.map((alert) => {
              const score = scoreOf(alert);
              const verdict = verdictMeta(verdictOf(alert));
              const href = opportunityHref(alert);
              const content = (
                <>
                  <span className={[styles.unread, alert.read_at ? styles.read : ""].filter(Boolean).join(" ")} aria-hidden="true" />
                  <span className={styles.listIcon} aria-hidden="true"><Bell size={17} /></span>
                  <span className={styles.listCopy}>
                    <small>{reasonLabel(alert.reason)} · {formatShortDateTime(alert.created_at)}</small>
                    <strong>{alert.descripcion ?? "Oportunidad evaluada"}</strong>
                    <span>{alert.codigo ?? "Sin código"}{alert.entidad_nombre ? ` · ${alert.entidad_nombre}` : ""}</span>
                    {alert.payload?.summary && alert.payload.summary !== alert.descripcion ? <p>{alert.payload.summary}</p> : null}
                  </span>
                  <span className={styles.listResult}>
                    {score != null ? <strong>{score}/100</strong> : <strong>Evaluada</strong>}
                    <span className={styles[verdict.tone]}>{verdict.label}</span>
                    <small>{formatDeadline(deadlineOf(alert))}</small>
                  </span>
                  {href ? <ArrowRight size={17} aria-hidden="true" /> : null}
                </>
              );
              return href ? <Link href={href} key={alert.id}>{content}</Link> : <div key={alert.id}>{content}</div>;
            })}
          </div>
        ) : (
          <div className={styles.onlyFeatured}>Esta es tu primera alerta. Las siguientes aparecerán en esta lista.</div>
        )}
      </section>
    </div>
  );
}
