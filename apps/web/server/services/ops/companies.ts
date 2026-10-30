import { opsQuery } from "./db.ts";
import { labelCompanies } from "./model.ts";
import type { CompanyRow } from "./model.ts";
import type { OpsCompany } from "./types.ts";

/**
 * Perfiles activos que alguna vez pasaron por el pipeline: decisiones de menores, evaluaciones de mayores o
 * calificaciones manuales. En la práctica son DIN y VEYON; los perfiles de prueba sin actividad no ensucian el filtro.
 */
const COMPANIES_SQL = `
SELECT cp.id::text AS id, cp.razon_social, cp.tenant_id::text AS tenant_id, cp.ruc
FROM company_profiles cp
WHERE cp.is_active
  AND (EXISTS (SELECT 1 FROM prod6_profile_decisions d WHERE d.profile_id = cp.id)
    OR EXISTS (SELECT 1 FROM manual_grading_requests r WHERE r.profile_id = cp.id)
    OR EXISTS (SELECT 1 FROM prod2_evaluations e WHERE e.profile_id = cp.id AND e.kind = 'gemini_match'))
ORDER BY cp.razon_social, cp.created_at`;

export async function listOpsCompanies(): Promise<OpsCompany[]> {
  return labelCompanies(await opsQuery<CompanyRow>(COMPANIES_SQL));
}
