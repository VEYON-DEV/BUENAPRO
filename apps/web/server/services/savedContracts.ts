import { query } from "@/server/db/client";

export async function saveContractForTenant(
  tenantId: string,
  idContrato: number,
  actorId?: string | null,
) {
  const result = await query<{
    id_contrato: string;
    created_at: Date;
  }>(
    `
    INSERT INTO saved_contracts (tenant_id, id_contrato, saved_by)
    SELECT $1, c.id_contrato, $3
    FROM seace_contracts c
    WHERE c.id_contrato = $2
    ON CONFLICT (tenant_id, id_contrato)
    DO UPDATE SET saved_by = COALESCE(saved_contracts.saved_by, EXCLUDED.saved_by)
    RETURNING id_contrato, created_at
    `,
    [tenantId, idContrato, actorId ?? null],
  );

  const saved = result.rows[0];
  if (!saved) return { error: "Contract not found", status: 404 as const };
  return {
    data: {
      id_contrato: Number(saved.id_contrato),
      is_saved: true,
      saved_at: saved.created_at,
    },
    status: 200 as const,
  };
}

export async function unsaveContractForTenant(tenantId: string, idContrato: number) {
  await query(
    "DELETE FROM saved_contracts WHERE tenant_id = $1 AND id_contrato = $2",
    [tenantId, idContrato],
  );
  return { data: { id_contrato: idContrato, is_saved: false }, status: 200 as const };
}
