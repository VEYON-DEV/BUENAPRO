import type { PoolClient } from "pg";

export class Prod4AnalysisError extends Error {
  code: string;
  status: number;
  constructor(code: string, status: number, message: string) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export function assertProd4AnalysisAvailable(): never {
  throw new Prod4AnalysisError(
    "source_retired", 410,
    "PROD4 fue retirado como fuente de ingesta. No se permiten nuevos análisis; el histórico permanece disponible.",
  );
}

export async function enqueueProd4Analysis(_client: Pick<PoolClient, "query">, _tenantId: string, _id: string, _force: boolean): Promise<{ job_id: string; status: string }> {
  // Keep a guard at the service boundary for callers outside the HTTP route.
  assertProd4AnalysisAvailable();
}

export async function getProd4AnalysisJob(client: Pick<PoolClient, "query">, tenantId: string, id: string, jobId: string) {
  const result = await client.query(
    `SELECT j.id::text AS job_id,j.status,
            cp.is_active AS profile_active,
            EXISTS (
              SELECT 1 FROM prod4_processes p
              JOIN prod4_document_extractions e ON e.id_procedimiento=p.id_procedimiento
                AND e.is_current AND e.id::text=j.payload->>'extraction_id'
                AND e.quality <> 'failed' AND NOT e.requires_human_review AND e.facet_count > 0
              JOIN opportunity_matches m ON m.opportunity_id=p.opportunity_id AND m.profile_id=cp.id
              WHERE p.id_procedimiento::text=j.payload->>'id_procedimiento'
                AND p.document_analysis_status='extracted'
                AND m.breakdown_json->'meta'->>'extraction_id'=j.payload->>'extraction_id'
                AND m.breakdown_json->'meta'->>'profile_hash'=cp.profile_hash
                AND (COALESCE(j.payload->>'force','false') <> 'true' OR m.updated_at >= j.created_at)
            ) AS match_current
     FROM worker_jobs j
     JOIN company_profiles cp ON cp.id::text=j.payload->>'profile_id'
     WHERE cp.tenant_id=$1 AND j.id=$2::bigint AND j.job_type='analyze_prod4_match'
       AND j.payload->>'id_procedimiento'=$3`, [tenantId, jobId, id],
  );
  const job = result.rows[0];
  if (!job) throw new Prod4AnalysisError("not_found", 404, "No se encontró este análisis.");
  if (job.status === "done" && (!job.profile_active || !job.match_current)) {
    return { job_id: job.job_id, status: "skipped", error: job.profile_active
      ? "El concurso o sus bases cambiaron durante el análisis. No se generó una evaluación vigente; actualiza el detalle antes de volver a intentarlo."
      : "El perfil de empresa ya no está activo. Activa un perfil antes de volver a analizar." };
  }
  // Raw worker errors can include provider information; never expose them to clients.
  return { job_id: job.job_id, status: job.status, ...(job.status === "dead" || job.status === "failed" ? { error: "No se pudo completar el análisis. Puedes volver a intentarlo." } : {}) };
}
