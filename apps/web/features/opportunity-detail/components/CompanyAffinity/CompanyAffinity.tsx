import type { CSSProperties } from "react";
import { CheckCircle2, CircleHelp, Clock3, FileCheck2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { formatDateTime } from "@/lib/format/date";
import { JEV_DISCLAIMER, jevCompanyAffinities, type JevCompanyAffinity, type JevCompanyRow } from "@/lib/jevAffinity";
import styles from "./CompanyAffinity.module.css";

const DECISION_ICONS = { accepted: CheckCircle2, rejected: XCircle, pending: Clock3 } as const;

function Meter({ company }: { company: JevCompanyAffinity }) {
  if (company.score == null) return <p className={styles.noScore}>{company.noScoreNote}</p>;
  const threshold = company.threshold;
  return (
    <div className={styles.meterBlock}>
      <div
        aria-label={`Afinidad JEV de ${company.name}`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={company.score}
        aria-valuetext={company.valueText}
        className={styles.meter}
        role="meter"
      >
        <span className={styles.track}><i className={styles.fill} style={{ width: `${company.score}%` }} /></span>
        {threshold != null ? <span className={styles.threshold} style={{ "--threshold": `${threshold}%` } as CSSProperties} /> : null}
      </div>
      <p className={styles.reading}>
        <strong>{company.score}</strong><span>/ 100</span>
        {threshold != null ? <small>Umbral {Math.round(threshold)}</small> : null}
      </p>
    </div>
  );
}

/** Calificación JEV por empresa del tenant. Sin decisiones no muestra nada. */
export function CompanyAffinity({ rows }: { rows: JevCompanyRow[] | null | undefined }) {
  const companies = jevCompanyAffinities(rows);
  if (!companies.length) return null;
  return (
    <section aria-labelledby="company-affinity-title" className={styles.panel} id="company-affinity">
      <header className={styles.head}>
        <h2 id="company-affinity-title">Afinidad por empresa</h2>
        <p>{JEV_DISCLAIMER}</p>
      </header>
      <ul className={styles.list}>
        {companies.map((company) => {
          const Icon = company.decision ? DECISION_ICONS[company.decision] : CircleHelp;
          const context = [company.subcategoryLabel, company.domainLabel].filter(Boolean).join(" · ");
          return (
            <li className={[styles.item, company.isCurrent && companies.length > 1 ? styles.current : ""].join(" ")} key={company.profileId}>
              <div className={styles.identity}>
                <div className={styles.nameLine}>
                  <strong className={styles.name}>{company.name}</strong>
                  {company.isCurrent ? <span className={styles.activeTag}>Perfil activo</span> : null}
                </div>
                {context ? <span className={styles.context}>{context}</span> : null}
              </div>
              <Meter company={company} />
              <div className={styles.status}>
                <Badge tone={company.tone}><Icon aria-hidden="true" className={styles.pillIcon} size={13} />{company.decisionLabel}</Badge>
                {company.informationLabel ? (
                  <span className={styles.information}>
                    {company.informationClear === false ? <CircleHelp aria-hidden="true" size={13} /> : <FileCheck2 aria-hidden="true" size={13} />}
                    {company.informationLabel}
                  </span>
                ) : null}
                {company.decidedAt ? <span className={styles.date}>{formatDateTime(company.decidedAt)}</span> : null}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
