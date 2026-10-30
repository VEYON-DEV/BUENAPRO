import { ChevronRight, CircleCheck } from "lucide-react";
import { opsHref } from "@/server/services/ops/filters";
import type { FailureGroup } from "@/server/services/ops/model";
import { OPS_SOURCE_LABEL } from "@/server/services/ops/types";
import type { OpsSource } from "@/server/services/ops/types";
import { formatCount, formatDayLabel, formatLimaTime } from "../../model/format";
import styles from "./FailureGroups.module.css";

/**
 * Fallas de un día: una fila por paso y motivo con su conteo; al abrirla se ven hasta 50 elementos, cada uno
 * con su enlace a la traza. `<details>` nativo: sin JavaScript y navegable con teclado.
 */
export function FailureGroups({
  source,
  groups,
  day,
  company,
}: {
  source: OpsSource;
  groups: FailureGroup[];
  day: string;
  company: string | null;
}) {
  const total = groups.reduce((sum, group) => sum + group.total, 0);
  return (
    <div className={styles.block}>
      <h3 className={styles.title}>
        {OPS_SOURCE_LABEL[source]}
        <span className={styles.count}>
          {groups.length ? `${formatCount(total)} en ${formatCount(groups.length)} grupo${groups.length === 1 ? "" : "s"}` : "sin fallas"}
        </span>
      </h3>
      {groups.length === 0 ? (
        <p className={styles.empty}>
          <CircleCheck aria-hidden="true" size={16} strokeWidth={1.9} />
          Sin fallas registradas el {formatDayLabel(day)}.
        </p>
      ) : (
        <ul className={styles.list}>
          {groups.map((group) => (
            <li key={`${group.step}|${group.reason}`}>
              <details className={styles.group}>
                <summary className={styles.summary}>
                  <ChevronRight aria-hidden="true" className={styles.chevron} size={16} strokeWidth={2} />
                  <span className={styles.step}>{group.stepLabel}</span>
                  <span className={styles.reason}>{group.reason}</span>
                  <span className={styles.total}>{formatCount(group.total)}</span>
                </summary>
                <div className={styles.body}>
                  <ul className={styles.items}>
                    {group.items.map((item) => (
                      <li className={styles.item} key={item.key}>
                        {item.traceHref ? (
                          <a className={styles.code} href={item.traceHref}>{item.code}</a>
                        ) : (
                          <span className={styles.code}>{item.code}</span>
                        )}
                        <span className={styles.entity}>{item.entity ?? item.ref ?? ""}</span>
                        <span className={styles.state}>{item.company ? `${item.company} · ` : ""}{item.state ?? ""}</span>
                        <time className={styles.time} dateTime={item.ts}>{formatLimaTime(item.ts)}</time>
                      </li>
                    ))}
                  </ul>
                  <p className={styles.more}>
                    {group.total > group.items.length ? `Mostrando ${group.items.length} de ${formatCount(group.total)}. ` : ""}
                    <a href={opsHref("/ops/items", { source, step: group.step, day, company })}>Ver todo el paso del día</a>
                  </p>
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
