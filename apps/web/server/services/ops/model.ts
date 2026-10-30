import { COST_BUCKETS, columnSteps, findStep } from "./steps.ts";
import type { StepDef } from "./steps.ts";
import { scrubText } from "./redact.ts";
import type { OpsCompany, OpsSource } from "./types.ts";

/** Formas de datos del panel y su armado a partir de filas de SQL. Todo puro (sin base de datos). */

// --- empresas -----------------------------------------------------------------------------------

const LEGAL_SUFFIX = /[\s,]+(S\.?\s?A\.?\s?C\.?|S\.?\s?A\.?|S\.?\s?R\.?\s?L\.?|E\.?\s?I\.?\s?R\.?\s?L\.?|S\.?\s?A\.?\s?A\.?)\s*$/i;

/** «DIN AUTOMATIZACION S.A.C.» → «DIN AUTOMATIZACION»; «VEYON SAC» → «VEYON». */
export function companyLabel(razonSocial: string): string {
  let label = razonSocial.replace(/\s+/g, " ").trim();
  for (let pass = 0; pass < 2; pass += 1) {
    const stripped = label.replace(LEGAL_SUFFIX, "").trim();
    if (stripped && stripped !== label) label = stripped;
  }
  return label.length > 28 ? `${label.slice(0, 27)}…` : label || razonSocial;
}

export type CompanyRow = { id: string; razon_social: string; tenant_id: string; ruc?: string | null };

/** Etiquetas cortas y únicas: si dos empresas dan el mismo nombre se les agrega el final del RUC. */
export function labelCompanies(rows: CompanyRow[]): OpsCompany[] {
  const base = rows.map((row) => ({ row, label: companyLabel(row.razon_social) }));
  const seen = new Map<string, number>();
  for (const item of base) seen.set(item.label, (seen.get(item.label) ?? 0) + 1);
  return base.map(({ row, label }) => ({
    id: row.id.toLowerCase(),
    label: (seen.get(label) ?? 0) > 1 && row.ruc ? `${label} · …${row.ruc.slice(-4)}` : label,
    razonSocial: row.razon_social,
    tenantId: row.tenant_id.toLowerCase(),
  }));
}

export function companyLabels(companies: readonly OpsCompany[]): Map<string, string> {
  return new Map(companies.map((company) => [company.id, company.label]));
}

// --- tabla diaria -------------------------------------------------------------------------------

export type CountRow = { step: string; day: string; n: number };

export type DailyColumn = {
  key: string;
  label: string;
  title: string;
  hint: string;
  group: string;
  shared: boolean;
  tone: StepDef["tone"];
};

export type DailyGroup = { label: string; span: number; shared: boolean };

export type DailyTable = {
  source: OpsSource;
  columns: DailyColumn[];
  groups: DailyGroup[];
  /** Más nuevo primero. */
  rows: Array<{ day: string; values: Record<string, number> }>;
  totals: Record<string, number>;
  /** Máximo por columna dentro del rango, para escalar las barras. */
  max: Record<string, number>;
};

export function shapeDaily(source: OpsSource, days: readonly string[], counts: readonly CountRow[]): DailyTable {
  const steps = columnSteps(source);
  const columns: DailyColumn[] = steps.map((step) => ({
    key: step.key,
    label: step.label,
    title: step.title,
    hint: step.hint,
    group: step.group,
    shared: step.shared,
    tone: step.tone,
  }));
  const index = new Map<string, number>();
  for (const row of counts) index.set(`${row.step}|${row.day}`, (index.get(`${row.step}|${row.day}`) ?? 0) + Number(row.n));
  const rows = days.map((day) => ({
    day,
    values: Object.fromEntries(columns.map((column) => [column.key, index.get(`${column.key}|${day}`) ?? 0])),
  }));
  const totals: Record<string, number> = {};
  const max: Record<string, number> = {};
  for (const column of columns) {
    totals[column.key] = rows.reduce((sum, row) => sum + row.values[column.key], 0);
    max[column.key] = rows.reduce((peak, row) => Math.max(peak, row.values[column.key]), 0);
  }
  const groups: DailyGroup[] = [];
  for (const column of columns) {
    const last = groups[groups.length - 1];
    if (last && last.label === column.group) {
      last.span += 1;
      last.shared = last.shared && column.shared;
    } else {
      groups.push({ label: column.group, span: 1, shared: column.shared });
    }
  }
  return { source, columns, groups, rows, totals, max };
}

// --- costos -------------------------------------------------------------------------------------

export type CostRow = { bucket: string; day: string; usd: string | number; n: number; priced: number };

export type CostCell = { usd: number; n: number; priced: number };

export type CostsTable = {
  buckets: string[];
  rows: Array<{ day: string; cells: Record<string, CostCell>; totalUsd: number }>;
  totals: Record<string, number>;
  totalUsd: number;
};

export function shapeCosts(days: readonly string[], rows: readonly CostRow[]): CostsTable {
  const buckets = Object.keys(COST_BUCKETS);
  const index = new Map<string, CostCell>();
  for (const row of rows) {
    index.set(`${row.bucket}|${row.day}`, { usd: Number(row.usd) || 0, n: Number(row.n) || 0, priced: Number(row.priced) || 0 });
  }
  const empty: CostCell = { usd: 0, n: 0, priced: 0 };
  const shaped = days.map((day) => {
    const cells = Object.fromEntries(buckets.map((bucket) => [bucket, index.get(`${bucket}|${day}`) ?? empty]));
    return { day, cells, totalUsd: buckets.reduce((sum, bucket) => sum + cells[bucket].usd, 0) };
  });
  const totals = Object.fromEntries(buckets.map((bucket) => [bucket, shaped.reduce((sum, row) => sum + row.cells[bucket].usd, 0)]));
  return { buckets, rows: shaped, totals, totalUsd: shaped.reduce((sum, row) => sum + row.totalUsd, 0) };
}

export type QuotaRow = { tenant_id: string; day: string; used: number };

export type QuotaTable = {
  limit: number;
  /** Una columna por tenant: el tope es por empresa (tenant) y día, y suma menores y mayores. */
  columns: Array<{ tenantId: string; label: string }>;
  rows: Array<{ day: string; used: Record<string, number> }>;
};

export function shapeQuota(days: readonly string[], companies: readonly OpsCompany[], rows: readonly QuotaRow[], limit: number): QuotaTable {
  const columns: QuotaTable["columns"] = [];
  for (const company of companies) {
    if (!columns.some((column) => column.tenantId === company.tenantId)) columns.push({ tenantId: company.tenantId, label: company.label });
  }
  const index = new Map(rows.map((row) => [`${row.tenant_id.toLowerCase()}|${row.day}`, Number(row.used)]));
  return {
    limit,
    columns,
    rows: days.map((day) => ({
      day,
      used: Object.fromEntries(columns.map((column) => [column.tenantId, index.get(`${column.tenantId}|${day}`) ?? 0])),
    })),
  };
}

// --- elementos y fallas -------------------------------------------------------------------------

/** Fila cruda de un paso (ver itemsSql). */
export type EventRow = {
  item_id: string | null;
  ts: Date | string;
  day: string;
  profile_id: string | null;
  score: string | null;
  state: string | null;
  reason: string | null;
  cost: string | null;
  ref: string | null;
  title: string | null;
};

export type ItemLabel = {
  code: string | null;
  entity: string | null;
  description: string | null;
  /** Puntajes JEV vigentes por empresa; en mayores uno solo, sin empresa. */
  jev: Array<{ profileId: string | null; score: number }>;
};

export type OpsItem = {
  key: string;
  itemId: string | null;
  traceHref: string | null;
  code: string;
  entity: string | null;
  description: string | null;
  /** Dato secundario de la fila: documento leído, número de trabajo… */
  ref: string | null;
  jev: Array<{ company: string | null; score: number }>;
  state: string | null;
  reason: string | null;
  company: string | null;
  ts: string;
  costUsd: number | null;
};

export function traceHref(source: OpsSource, itemId: string | null): string | null {
  return itemId && /^[1-9]\d{0,18}$/.test(itemId) ? `/ops/traza/${source}/${itemId}` : null;
}

function clean(value: string | null | undefined, max = 240): string | null {
  return value ? scrubText(value, max) : null;
}

function iso(value: Date | string): string {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toISOString();
}

/** Une una fila de evento con la ficha de su licitación (si la hay) y las etiquetas de empresa. */
export function buildItem(
  source: OpsSource,
  step: StepDef,
  row: EventRow,
  index: number,
  labels: ReadonlyMap<string, ItemLabel>,
  companies: ReadonlyMap<string, string>,
  focusCompany: string | null,
): OpsItem {
  const label = row.item_id ? labels.get(row.item_id) : undefined;
  const eventScore = row.score == null ? null : Number(row.score);
  const profileLabel = (id: string | null) => (id ? (companies.get(id.toLowerCase()) ?? `empresa ${id.slice(0, 8)}`) : null);
  let jev = (label?.jev ?? [])
    .filter((entry) => !focusCompany || !entry.profileId || entry.profileId.toLowerCase() === focusCompany)
    .map((entry) => ({ company: profileLabel(entry.profileId), score: entry.score }));
  // En los pasos de JEV, el puntaje del propio evento es el de esa decisión.
  if (eventScore != null && Number.isFinite(eventScore) && step.key.startsWith("jev")) {
    jev = [{ company: profileLabel(row.profile_id), score: eventScore }];
  }
  const standalone = step.rowKind === "busqueda" || step.rowKind === "embedding";
  const code = standalone
    ? (row.ref ?? row.title ?? "—")
    : (label?.code ?? (row.item_id ? `#${row.item_id}` : (row.ref ?? "—")));
  const ref = !standalone && row.ref && row.ref !== code ? row.ref : null;
  return {
    key: `${step.key}-${row.item_id ?? "x"}-${index}`,
    itemId: row.item_id,
    traceHref: standalone ? null : traceHref(source, row.item_id),
    code: clean(code, 120) ?? "—",
    entity: clean(standalone ? row.title : label?.entity, 160),
    description: standalone ? null : clean(label?.description, 240),
    ref: clean(ref, 160),
    jev,
    state: clean(row.state, 120),
    reason: clean(row.reason, 240),
    company: profileLabel(row.profile_id),
    ts: iso(row.ts),
    costUsd: row.cost == null ? null : Number(row.cost),
  };
}

export type FailureRow = {
  step: string;
  reason: string;
  total: number;
  item_id: string | null;
  ts: Date | string;
  profile_id: string | null;
  state: string | null;
  ref: string | null;
  title: string | null;
};

export type FailureGroup = {
  source: OpsSource;
  step: string;
  stepLabel: string;
  reason: string;
  total: number;
  items: OpsItem[];
};

/** Agrupa las filas (ya ordenadas por total) en grupos paso + razón con sus elementos. */
export function groupFailures(
  source: OpsSource,
  rows: readonly FailureRow[],
  labels: ReadonlyMap<string, ItemLabel>,
  companies: ReadonlyMap<string, string>,
  focusCompany: string | null,
): FailureGroup[] {
  const groups = new Map<string, FailureGroup>();
  rows.forEach((row, index) => {
    const step = findStep(source, row.step);
    if (!step) return;
    const key = `${row.step}|${row.reason}`;
    let group = groups.get(key);
    if (!group) {
      group = { source, step: row.step, stepLabel: step.title, reason: clean(row.reason, 240) ?? "sin motivo registrado", total: Number(row.total), items: [] };
      groups.set(key, group);
    }
    group.items.push(
      buildItem(
        source,
        step,
        { ...row, day: "", score: null, reason: null, cost: null },
        index,
        labels,
        companies,
        focusCompany,
      ),
    );
  });
  return [...groups.values()];
}
