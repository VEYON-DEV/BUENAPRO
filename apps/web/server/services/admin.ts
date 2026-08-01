import { query } from "@/server/db/client";

type JsonRecord = Record<string, unknown>;

const MVP_BATCH_BUCKETS = [
  { bucket: "tecnologia", segments: [43, 81] },
  { bucket: "transporte", segments: [78] },
  { bucket: "legal", segments: [80] },
];

type Pagination = {
  page: number;
  pageSize: number;
  limit: number;
  offset: number;
};

const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;

export function isInternalAdminRequest(request: Request) {
  const expected = process.env.INTERNAL_JOBS_TOKEN;
  const token = request.headers.get("x-internal-token");
  return Boolean(expected && token === expected);
}

export async function enqueueHistoricalBackfill(input: { segment: number; limit?: number; year?: number }) {
  const dedupKey = `historical_backfill:${input.segment}`;
  const result = await query<JsonRecord>(
    `
    INSERT INTO worker_jobs (job_type, queue_name, payload, dedup_key, priority)
    VALUES ('historical_backfill', 'io', $1::jsonb, $2, 5)
    ON CONFLICT DO NOTHING
    RETURNING id, status, payload, created_at
    `,
    [JSON.stringify(input), dedupKey],
  );
  return result.rows[0] ?? null;
}

export async function getHistoricalStatus(segment?: number) {
  const values: unknown[] = [];
  const where = segment ? `WHERE cubso_segmento = $1` : "";
  if (segment) values.push(String(segment));
  const [outcomes, progress, jobs] = await Promise.all([
    query<JsonRecord>(
      `
      SELECT cubso_segmento, count(*)::int AS total,
        count(*) FILTER (WHERE estado_resultado='ADJUDICADO')::int AS adjudicados,
        count(*) FILTER (WHERE estado_resultado='DESIERTO')::int AS desiertos,
        max(last_checked_at) AS last_checked_at
      FROM historical_contract_outcomes ${where}
      GROUP BY cubso_segmento ORDER BY cubso_segmento
      `,
      values,
    ),
    query<JsonRecord>(
      `SELECT cubso_segmento, status, next_page, page_size, total_elements,
         processed, saved, failed, heartbeat_at, completed_at, last_error
       FROM historical_backfill_progress ${where}
       ORDER BY cubso_segmento`,
      values,
    ),
    query<JsonRecord>(
      `SELECT id, status, payload, attempts, last_error, created_at, finished_at
       FROM worker_jobs WHERE job_type='historical_backfill'
       ORDER BY created_at DESC LIMIT 10`,
    ),
  ]);
  return { segments: outcomes.rows, progress: progress.rows, recent_jobs: jobs.rows };
}

export function parsePositiveInteger(value: string | null, fallback: number) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) return fallback;
  return parsed;
}

function parseOptionalPositiveInteger(value: string | null) {
  if (!value) return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) return null;
  return parsed;
}

function getPagination(params: URLSearchParams): Pagination {
  const page = parsePositiveInteger(params.get("page"), 1);
  const requestedPageSize = parsePositiveInteger(params.get("page_size"), DEFAULT_PAGE_SIZE);
  const pageSize = Math.min(requestedPageSize, MAX_PAGE_SIZE);
  return {
    page,
    pageSize,
    limit: pageSize,
    offset: (page - 1) * pageSize,
  };
}

function withPagination<T extends JsonRecord>(rows: T[], pagination: Pagination, total?: number) {
  return {
    data: rows,
    meta: {
      page: pagination.page,
      page_size: pagination.pageSize,
      count: rows.length,
      total,
    },
  };
}

function pushFilter(where: string[], values: unknown[], sql: string, ...filterValues: unknown[]) {
  let filterSql = sql;
  for (const value of filterValues) {
    values.push(value);
    filterSql = filterSql.replace("?", `$${values.length}`);
  }
  where.push(filterSql);
}

function sortDirection(value: string | null) {
  return value?.toLowerCase() === "asc" ? "ASC" : "DESC";
}

function pickSort(value: string | null, allowed: Record<string, string>, fallback: string) {
  return allowed[value ?? ""] ?? allowed[fallback];
}

async function countRows(fromAndWhereSql: string, values: unknown[]) {
  const result = await query<{ total: string }>(`SELECT count(*)::text AS total ${fromAndWhereSql}`, values);
  return Number(result.rows[0]?.total ?? 0);
}

export async function listWorkerJobs(params: URLSearchParams) {
  const pagination = getPagination(params);
  const values: unknown[] = [];
  const where: string[] = [];

  const status = params.get("status");
  if (status) pushFilter(where, values, "status = ?", status);

  const queueName = params.get("queue_name");
  if (queueName) pushFilter(where, values, "queue_name = ?", queueName);

  const jobType = params.get("job_type");
  if (jobType) pushFilter(where, values, "job_type = ?", jobType);

  const q = params.get("q");
  if (q) {
    const pattern = `%${q}%`;
    pushFilter(where, values, "(job_type ILIKE ? OR dedup_key ILIKE ? OR last_error ILIKE ?)", pattern, pattern, pattern);
  }

  const whereSql = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const sortColumn = pickSort(
    params.get("sort"),
    {
      id: "id",
      created_at: "created_at",
      run_after: "run_after",
      priority: "priority",
      attempts: "attempts",
      status: "status",
    },
    "created_at",
  );
  const direction = sortDirection(params.get("direction"));
  const fromAndWhere = `FROM worker_jobs ${whereSql}`;
  const total = await countRows(fromAndWhere, values);

  values.push(pagination.limit, pagination.offset);
  const result = await query<JsonRecord>(
    `
    SELECT
      id,
      job_type,
      queue_name,
      payload,
      status,
      priority,
      run_after,
      attempts,
      max_attempts,
      claimed_by,
      claimed_at,
      last_error,
      dedup_key,
      created_at,
      finished_at
    FROM worker_jobs
    ${whereSql}
    ORDER BY ${sortColumn} ${direction}, id DESC
    LIMIT $${values.length - 1}
    OFFSET $${values.length}
    `,
    values,
  );
  return withPagination(result.rows, pagination, total);
}

export async function getWorkerJob(id: number) {
  const job = await query<JsonRecord>("SELECT * FROM worker_jobs WHERE id = $1", [id]);
  const row = job.rows[0] ?? null;
  if (!row) return null;

  const events = await query<JsonRecord>(
    `
    SELECT *
    FROM pipeline_events
    WHERE job_id = $1
    ORDER BY created_at DESC, id DESC
    LIMIT 100
    `,
    [id],
  );
  return { data: row, events: events.rows };
}

export async function retryWorkerJob(id: number) {
  const result = await query<JsonRecord>(
    `
    UPDATE worker_jobs
    SET status = 'pending',
        attempts = 0,
        run_after = now(),
        claimed_by = NULL,
        claimed_at = NULL,
        finished_at = NULL,
        last_error = NULL
    WHERE id = $1
      AND status IN ('failed', 'dead', 'claimed')
    RETURNING *
    `,
    [id],
  );
  return result.rows[0] ?? null;
}

export async function markWorkerJobDead(id: number, reason?: string) {
  const result = await query<JsonRecord>(
    `
    UPDATE worker_jobs
    SET status = 'dead',
        finished_at = now(),
        claimed_by = NULL,
        claimed_at = NULL,
        last_error = COALESCE($2, last_error)
    WHERE id = $1
      AND status <> 'done'
    RETURNING *
    `,
    [id, reason ? reason.slice(0, 4000) : null],
  );
  return result.rows[0] ?? null;
}

export async function listPipelineEvents(params: URLSearchParams) {
  const pagination = getPagination(params);
  const values: unknown[] = [];
  const where: string[] = [];

  const idContrato = parseOptionalPositiveInteger(params.get("id_contrato"));
  if (idContrato) pushFilter(where, values, "id_contrato = ?", idContrato);

  const jobId = parseOptionalPositiveInteger(params.get("job_id"));
  if (jobId) pushFilter(where, values, "job_id = ?", jobId);

  const stage = params.get("stage");
  if (stage) pushFilter(where, values, "stage = ?", stage);

  const status = params.get("status");
  if (status) pushFilter(where, values, "status = ?", status);

  const whereSql = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const fromAndWhere = `FROM pipeline_events ${whereSql}`;
  const total = await countRows(fromAndWhere, values);

  values.push(pagination.limit, pagination.offset);
  const result = await query<JsonRecord>(
    `
    SELECT *
    FROM pipeline_events
    ${whereSql}
    ORDER BY created_at DESC, id DESC
    LIMIT $${values.length - 1}
    OFFSET $${values.length}
    `,
    values,
  );
  return withPagination(result.rows, pagination, total);
}

export async function listApiContractChecks(params: URLSearchParams) {
  const pagination = getPagination(params);
  const values: unknown[] = [];
  const where: string[] = [];

  const endpoint = params.get("endpoint");
  if (endpoint) pushFilter(where, values, "endpoint = ?", endpoint);

  const ok = params.get("ok");
  if (ok === "true" || ok === "false") pushFilter(where, values, "ok = ?", ok === "true");

  const whereSql = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const fromAndWhere = `FROM api_contract_checks ${whereSql}`;
  const total = await countRows(fromAndWhere, values);

  values.push(pagination.limit, pagination.offset);
  const result = await query<JsonRecord>(
    `
    SELECT *
    FROM api_contract_checks
    ${whereSql}
    ORDER BY checked_at DESC, id DESC
    LIMIT $${values.length - 1}
    OFFSET $${values.length}
    `,
    values,
  );
  return withPagination(result.rows, pagination, total);
}

export async function listTdrExtractionsRequiringReview(params: URLSearchParams) {
  const pagination = getPagination(params);
  const values: unknown[] = [];
  const where = ["e.requires_human_review = true"];

  const quality = params.get("quality");
  if (quality) pushFilter(where, values, "e.quality = ?", quality);

  const idContrato = parseOptionalPositiveInteger(params.get("id_contrato"));
  if (idContrato) pushFilter(where, values, "d.id_contrato = ?", idContrato);

  const whereSql = `WHERE ${where.join(" AND ")}`;
  const fromAndWhere = `
    FROM tdr_extractions e
    JOIN contract_documents d ON d.id = e.contract_document_id
    LEFT JOIN seace_contracts c ON c.id_contrato = d.id_contrato
    ${whereSql}
  `;
  const total = await countRows(fromAndWhere, values);

  values.push(pagination.limit, pagination.offset);
  const result = await query<JsonRecord>(
    `
    SELECT
      e.id,
      e.contract_document_id,
      e.model,
      e.prompt_version,
      e.schema_version,
      e.input_tokens,
      e.output_tokens,
      e.cost_usd,
      e.quality,
      e.requires_human_review,
      e.is_current,
      e.summary_json,
      e.created_at,
      d.id_contrato,
      d.id_contrato_archivo,
      d.filename,
      d.doc_class,
      d.mime,
      c.codigo AS contrato_codigo,
      c.entidad_nombre,
      c.descripcion
    FROM tdr_extractions e
    JOIN contract_documents d ON d.id = e.contract_document_id
    LEFT JOIN seace_contracts c ON c.id_contrato = d.id_contrato
    ${whereSql}
    ORDER BY e.created_at DESC, e.id DESC
    LIMIT $${values.length - 1}
    OFFSET $${values.length}
    `,
    values,
  );
  return withPagination(result.rows, pagination, total);
}

export async function startMvpBatch({ year, perBucket }: { year: number; perBucket: number }) {
  const batchId = `mvp-${year}-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}`;
  const jobs = [];

  for (const item of MVP_BATCH_BUCKETS) {
    const payload = {
      anio: year,
      batch_id: batchId,
      bucket: item.bucket,
      segments: item.segments,
      max_contracts: perBucket,
    };
    const result = await query<JsonRecord>(
      `
      INSERT INTO worker_jobs (job_type, queue_name, payload, dedup_key, priority)
      VALUES ('poll_search', 'io', $1::jsonb, $2, 1)
      ON CONFLICT DO NOTHING
      RETURNING id, job_type, queue_name, payload, status, created_at
      `,
      [JSON.stringify(payload), `poll_search:${batchId}:${item.bucket}`],
    );
    jobs.push(result.rows[0] ?? { skipped: true, payload });
  }

  return {
    data: {
      batch_id: batchId,
      year,
      per_bucket: perBucket,
      target_contracts: perBucket * MVP_BATCH_BUCKETS.length,
      buckets: MVP_BATCH_BUCKETS,
      jobs,
    },
  };
}

export async function getBatchStatus(batchId?: string) {
  const currentBatchId = batchId ?? (await latestBatchId());
  if (!currentBatchId) {
    return {
      data: {
        batch_id: null,
        status: "sin_batch",
        message: "No hay batches con batch_id en worker_jobs.",
      },
    };
  }

  const jobs = await query<JsonRecord>(
    `
    SELECT status, queue_name, job_type, count(*)::int AS count
    FROM worker_jobs
    WHERE payload->>'batch_id' = $1
    GROUP BY status, queue_name, job_type
    ORDER BY queue_name, job_type, status
    `,
    [currentBatchId],
  );

  const buckets = await query<JsonRecord>(
    `
    SELECT
      COALESCE(payload->>'bucket', 'sin_bucket') AS bucket,
      count(*) FILTER (WHERE job_type = 'process_contract')::int AS contracts_enqueued,
      count(*) FILTER (WHERE job_type = 'process_contract' AND status = 'done')::int AS contracts_processed,
      count(*) FILTER (WHERE job_type = 'download_file' AND status = 'done')::int AS files_downloaded,
      count(*) FILTER (WHERE job_type = 'extract_tdr')::int AS tdr_jobs,
      count(*) FILTER (WHERE job_type = 'extract_tdr' AND status = 'done')::int AS tdr_done,
      count(*) FILTER (WHERE status IN ('failed', 'dead'))::int AS failed_or_dead
    FROM worker_jobs
    WHERE payload->>'batch_id' = $1
    GROUP BY COALESCE(payload->>'bucket', 'sin_bucket')
    ORDER BY bucket
    `,
    [currentBatchId],
  );

  const totals = await query<JsonRecord>(
    `
    WITH batch_jobs AS (
      SELECT *
      FROM worker_jobs
      WHERE payload->>'batch_id' = $1
    ),
    batch_contracts AS (
      SELECT DISTINCT (payload->>'id_contrato')::bigint AS id_contrato
      FROM batch_jobs
      WHERE payload ? 'id_contrato'
    ),
    target AS (
      SELECT COALESCE(sum((payload->>'max_contracts')::int), 0)::int AS target_contracts
      FROM batch_jobs
      WHERE job_type = 'poll_search'
    ),
    extraction_stats AS (
      SELECT
        count(e.*)::int AS extractions,
        count(*) FILTER (WHERE e.reused_from_extraction_id IS NOT NULL)::int AS reused_extractions,
        COALESCE(sum(e.cost_usd), 0)::numeric AS cost_usd
      FROM tdr_extractions e
      JOIN contract_documents d ON d.id = e.contract_document_id
      JOIN batch_contracts bc ON bc.id_contrato = d.id_contrato
    )
    SELECT
      (SELECT target_contracts FROM target) AS target_contracts,
      (SELECT count(*)::int FROM batch_contracts) AS contracts_seen,
      (SELECT count(*)::int FROM contract_documents d JOIN batch_contracts bc ON bc.id_contrato = d.id_contrato) AS documents,
      (SELECT count(*)::int FROM requirement_facets f JOIN batch_contracts bc ON bc.id_contrato = f.id_contrato WHERE f.is_current) AS facets,
      (SELECT count(*)::int FROM contract_filter_index fi JOIN batch_contracts bc ON bc.id_contrato = fi.id_contrato) AS filter_rows,
      (SELECT extractions FROM extraction_stats) AS extractions,
      (SELECT reused_extractions FROM extraction_stats) AS reused_extractions,
      (SELECT cost_usd FROM extraction_stats) AS cost_usd,
      count(*) FILTER (WHERE job_type = 'download_file')::int AS download_jobs_total,
      count(*) FILTER (WHERE job_type = 'download_file' AND status = 'done')::int AS download_jobs_done,
      count(*) FILTER (WHERE job_type = 'extract_tdr')::int AS extract_jobs_total,
      count(*) FILTER (WHERE job_type = 'extract_tdr' AND status = 'done')::int AS extract_jobs_done,
      count(*)::int AS total_jobs,
      count(*) FILTER (WHERE status = 'done')::int AS done_jobs,
      count(*) FILTER (WHERE status IN ('failed', 'dead'))::int AS failed_jobs,
      min(created_at) AS started_at,
      max(COALESCE(finished_at, claimed_at, created_at)) AS last_activity_at
    FROM batch_jobs
    `,
    [currentBatchId],
  );

  const total = totals.rows[0] ?? {};
  const eta = estimateEta(total);
  const progress = buildBatchProgress(total);
  return {
    data: {
      batch_id: currentBatchId,
      status: batchState(total),
      eta,
      progress,
      totals: total,
      jobs: jobs.rows,
      buckets: buckets.rows,
    },
  };
}

async function latestBatchId() {
  const result = await query<{ batch_id: string }>(
    `
    SELECT payload->>'batch_id' AS batch_id
    FROM worker_jobs
    WHERE payload ? 'batch_id'
    ORDER BY created_at DESC
    LIMIT 1
    `,
  );
  return result.rows[0]?.batch_id ?? null;
}

function batchState(total: JsonRecord) {
  if (Number(total.failed_jobs ?? 0) > 0) return "requiere_revision";
  if (Number(total.total_jobs ?? 0) > 0 && Number(total.done_jobs ?? 0) === Number(total.total_jobs ?? 0)) {
    return "completo";
  }
  return "corriendo";
}

function estimateEta(total: JsonRecord) {
  const startedAt = total.started_at ? new Date(String(total.started_at)).getTime() : null;
  const doneJobs = Number(total.done_jobs ?? 0);
  const totalJobs = Number(total.total_jobs ?? 0);
  if (!startedAt || doneJobs <= 0 || totalJobs <= doneJobs) {
    return { seconds_remaining: null, text: "calculando" };
  }
  const elapsedSeconds = Math.max((Date.now() - startedAt) / 1000, 1);
  const jobsPerSecond = doneJobs / elapsedSeconds;
  const secondsRemaining = Math.ceil((totalJobs - doneJobs) / jobsPerSecond);
  return {
    seconds_remaining: secondsRemaining,
    text: humanDuration(secondsRemaining),
    jobs_per_minute: Math.round(jobsPerSecond * 60 * 10) / 10,
  };
}

function buildBatchProgress(total: JsonRecord) {
  const targetContracts = Number(total.target_contracts ?? 0);
  const contractsSeen = Number(total.contracts_seen ?? 0);
  const documents = Number(total.documents ?? 0);
  const extractions = Number(total.extractions ?? 0);
  const totalJobs = Number(total.total_jobs ?? 0);
  const doneJobs = Number(total.done_jobs ?? 0);

  return {
    overall_pct: percent(doneJobs, totalJobs),
    contracts_pct: percent(contractsSeen, targetContracts),
    downloads_pct: percent(Number(total.download_jobs_done ?? 0), Number(total.download_jobs_total ?? 0)),
    extractions_pct: percent(extractions, Number(total.extract_jobs_total ?? 0)),
    contracts: { done: contractsSeen, total: targetContracts },
    downloads: {
      done: Number(total.download_jobs_done ?? 0),
      total: Number(total.download_jobs_total ?? 0),
      documents,
    },
    extractions: {
      done: extractions,
      total: Number(total.extract_jobs_total ?? 0),
    },
    jobs: { done: doneJobs, total: totalJobs },
  };
}

function percent(done: number, total: number) {
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, Math.round((done / total) * 1000) / 10);
}

function humanDuration(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}
