import { ArrowLeft } from "lucide-react";
import { notFound } from "next/navigation";
import { AppShell } from "@/features/shell";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { listOpsCompanies } from "@/server/services/ops/companies";
import { opsHref, parseItemsQuery } from "@/server/services/ops/filters";
import { listItems } from "@/server/services/ops/items";
import { OPS_SOURCE_LABEL } from "@/server/services/ops/types";
import type { OpsCompany } from "@/server/services/ops/types";
import { Table } from "@/components/ui/Table";
import { formatCount, formatFullDay, formatLimaTime, formatUsd } from "../../model/format";
import styles from "./OpsItemsPage.module.css";

/** La lista detrás de un número de la tabla diaria: mismas filas que contó, con enlace a la traza de cada una. */
export async function OpsItemsPage({ params }: { params: URLSearchParams }) {
  const parsed = parseItemsQuery(params);
  if (!parsed.ok) notFound();
  const query = parsed.value;
  const companies: OpsCompany[] = await listOpsCompanies().catch(() => []);
  const backHref = `${opsHref("/ops", { range: query.range === 14 ? null : query.range, company: query.company })}#${query.source}`;

  let result: Awaited<ReturnType<typeof listItems>> | null = null;
  try {
    result = await listItems(query, companies);
  } catch (error) {
    console.error("ops items failed", error instanceof Error ? error.message : error);
  }

  const header = (
    <PageHeader
      description={result?.step.hint}
      eyebrow={
        <a className={styles.back} href={backHref}>
          <ArrowLeft aria-hidden="true" size={14} strokeWidth={2} />
          Operación
        </a>
      }
      meta={`${OPS_SOURCE_LABEL[query.source]} · ${formatFullDay(query.day)}`}
      title={result?.step.title ?? "Elementos"}
    />
  );

  if (!result) {
    return (
      <AppShell title="Operación">
        <div className={styles.page}>
          {header}
          <EmptyState title="No se pudo cargar la lista">La consulta falló o excedió el tiempo límite. Recarga la página.</EmptyState>
        </div>
      </AppShell>
    );
  }

  const showCompany = result.items.some((item) => item.company);
  const showCost = result.items.some((item) => item.costUsd != null);
  const focus = companies.find((company) => company.id === query.company);

  return (
    <AppShell title="Operación">
      <div className={styles.page}>
        {header}
        <p className={styles.scope}>
          <strong>{formatCount(result.total)}</strong> {result.total === 1 ? "fila" : "filas"}
          {result.total > result.items.length ? ` (se muestran las ${formatCount(result.items.length)} más recientes)` : ""}
          {result.step.shared
            ? " · paso compartido: no depende de la empresa"
            : focus
              ? ` · solo ${focus.label}`
              : " · todas las empresas"}
        </p>
        {result.items.length === 0 ? (
          <EmptyState title="Sin elementos ese día">No hay filas para este paso en el día elegido. Vuelve a la tabla y elige otra celda.</EmptyState>
        ) : (
          <Table className={styles.table}>
            <caption className={styles.srOnly}>{result.step.title}, {formatFullDay(query.day)}</caption>
            <thead>
              <tr>
                <th scope="col">Código</th>
                <th scope="col">Entidad y descripción</th>
                <th scope="col">JEV</th>
                <th scope="col">Último estado</th>
                <th scope="col">Motivo</th>
                {showCompany ? <th scope="col">Empresa</th> : null}
                {showCost ? <th data-numeric scope="col">Costo</th> : null}
                <th data-numeric scope="col">Hora (Lima)</th>
              </tr>
            </thead>
            <tbody>
              {result.items.map((item) => (
                <tr key={item.key}>
                  <td data-label="Código">
                    {item.traceHref ? (
                      <a className={styles.trace} href={item.traceHref} title="Ver traza">{item.code}</a>
                    ) : (
                      <span className={styles.plain}>{item.code}</span>
                    )}
                    {item.ref ? <small className={styles.ref}>{item.ref}</small> : null}
                  </td>
                  <td data-label="Entidad">
                    {item.entity ? <span className={styles.entity} title={item.entity}>{item.entity}</span> : null}
                    {item.description ? <small className={styles.description} title={item.description}>{item.description}</small> : null}
                  </td>
                  <td data-label="JEV">
                    {item.jev.length ? (
                      <span className={styles.chips}>
                        {item.jev.map((entry) => (
                          <span aria-label={`JEV ${entry.score} de 100${entry.company ? `, ${entry.company}` : ""}`} className={styles.chip} key={`${entry.company}-${entry.score}`}>
                            {entry.company ? `${entry.company} ` : ""}
                            {Number.isInteger(entry.score) ? entry.score : entry.score.toFixed(1)}
                          </span>
                        ))}
                      </span>
                    ) : (
                      <span className={styles.none}>—</span>
                    )}
                  </td>
                  <td data-label="Estado">{item.state ?? <span className={styles.none}>—</span>}</td>
                  <td data-label="Motivo">{item.reason ?? <span className={styles.none}>—</span>}</td>
                  {showCompany ? <td data-label="Empresa">{item.company ?? <span className={styles.none}>—</span>}</td> : null}
                  {showCost ? (
                    <td data-label="Costo" data-numeric>{item.costUsd != null ? formatUsd(item.costUsd) : "—"}</td>
                  ) : null}
                  <td data-label="Hora" data-numeric>
                    <time dateTime={item.ts}>{formatLimaTime(item.ts)}</time>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </div>
    </AppShell>
  );
}
