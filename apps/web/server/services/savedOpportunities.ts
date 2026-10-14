import { query } from "@/server/db/client";

/** Postgres "undefined_table": migration 0044 not applied yet. Reads degrade to "nothing saved". */
export function isMissingSavedOpportunitiesTable(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const detail = error as { code?: string; table?: string; message?: string };
  if (detail.code !== "42P01") return false;
  return detail.table === "saved_opportunities"
    || (typeof detail.message === "string"
      && /relation "(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?saved_opportunities" does not exist/.test(detail.message));
}

/** Saves a Buscador SEACE (PROD2) convocatoria by its canonical opportunity. */
export async function saveProd2OpportunityForTenant(tenantId: string, idConvocatoria: string, actorId?: string | null) {
  const result = await query<{ opportunity_id: string; created_at: Date }>(
    `
    INSERT INTO saved_opportunities (tenant_id, opportunity_id, saved_by)
    SELECT $1, p.opportunity_id, $3
    FROM prod2_discoveries p
    WHERE p.id_convocatoria = $2::bigint
    ON CONFLICT (tenant_id, opportunity_id)
    DO UPDATE SET saved_by = COALESCE(saved_opportunities.saved_by, EXCLUDED.saved_by)
    RETURNING opportunity_id::text, created_at
    `,
    [tenantId, idConvocatoria, actorId ?? null],
  );
  const saved = result.rows[0];
  if (!saved) return { error: "Opportunity not found", status: 404 as const };
  return { data: { id_convocatoria: idConvocatoria, opportunity_id: saved.opportunity_id, is_saved: true, saved_at: saved.created_at }, status: 200 as const };
}

export async function unsaveProd2OpportunityForTenant(tenantId: string, idConvocatoria: string) {
  await query(
    `
    DELETE FROM saved_opportunities s
    USING prod2_discoveries p
    WHERE s.tenant_id = $1 AND p.id_convocatoria = $2::bigint AND s.opportunity_id = p.opportunity_id
    `,
    [tenantId, idConvocatoria],
  );
  return { data: { id_convocatoria: idConvocatoria, is_saved: false }, status: 200 as const };
}

/** Subset of the given opportunity IDs saved by the tenant. */
export async function savedOpportunityIds(tenantId: string, opportunityIds: string[]): Promise<Set<string>> {
  if (!opportunityIds.length) return new Set();
  try {
    const result = await query<{ opportunity_id: string }>(
      "SELECT opportunity_id::text FROM saved_opportunities WHERE tenant_id = $1 AND opportunity_id = ANY($2::uuid[])",
      [tenantId, opportunityIds],
    );
    return new Set(result.rows.map(row => row.opportunity_id));
  } catch (error) {
    if (isMissingSavedOpportunitiesTable(error)) return new Set();
    throw error;
  }
}
