import { opsQuery } from "./db.ts";
import { listOpsCompanies } from "./companies.ts";
import { isTraceId } from "./filters.ts";
import { companyLabels } from "./model.ts";
import { MAJOR_QUERIES, MINOR_QUERIES } from "./traceQueries.ts";
import { assembleMajorTrace, assembleMinorTrace } from "./traceAssembly.ts";
import type { MajorTraceRaw, MinorTraceRaw, Raw, Trace } from "./traceAssembly.ts";
import type { OpsSource } from "./types.ts";

async function fetchAll<K extends string>(queries: Record<K, string>, id: string): Promise<Record<K, Raw[]>> {
  const entries = await Promise.all(
    (Object.entries(queries) as Array<[K, string]>).map(async ([name, sql]) => [name, await opsQuery<Raw>(sql, [id])] as const),
  );
  return Object.fromEntries(entries) as Record<K, Raw[]>;
}

/** Línea de tiempo completa de una licitación, o null si no hay ningún registro suyo. */
export async function getTrace(source: OpsSource, id: string): Promise<Trace | null> {
  if (!isTraceId(id)) return null;
  const labels = companyLabels(await listOpsCompanies());
  if (source === "menores") {
    const rows = await fetchAll(MINOR_QUERIES, id);
    const raw: MinorTraceRaw = { ...rows, contract: rows.contract[0] ?? null };
    return assembleMinorTrace(id, raw, labels);
  }
  const rows = await fetchAll(MAJOR_QUERIES, id);
  const raw: MajorTraceRaw = { ...rows, discovery: rows.discovery[0] ?? null };
  return assembleMajorTrace(id, raw, labels);
}
