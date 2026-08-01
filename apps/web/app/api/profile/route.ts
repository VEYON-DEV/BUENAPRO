import { NextRequest, NextResponse } from "next/server";
import { query } from "@/server/db/client";
import { requireTenantId } from "@/server/auth/tenant";
import { cleanCompanyKeywords } from "@/server/services/companyKeywords";

export async function GET() {
  const tenantId = await requireTenantId();
  const result = await query("SELECT * FROM company_profiles WHERE tenant_id = $1 ORDER BY created_at LIMIT 1", [tenantId]);
  return NextResponse.json({ data: result.rows[0] ?? null });
}

export async function PUT(request: NextRequest) {
  const tenantId = await requireTenantId();
  const body = await request.json();
  const companyKeywords = body.company_keywords == null ? null : cleanCompanyKeywords(body.company_keywords);
  const result = await query(
    `
    INSERT INTO company_profiles (
      tenant_id, ruc, razon_social, identity_json, finance_json, experience_json,
      econ_experience_json, team_json, hireable_roles_json, equipment_json,
      certifications_json, company_keywords, profile_hash, updated_at
    )
    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb, COALESCE($12::text[], '{}'), md5($13), now())
    ON CONFLICT (tenant_id, ruc)
    DO UPDATE SET
      razon_social = EXCLUDED.razon_social,
      identity_json = EXCLUDED.identity_json,
      finance_json = EXCLUDED.finance_json,
      experience_json = EXCLUDED.experience_json,
      econ_experience_json = EXCLUDED.econ_experience_json,
      team_json = EXCLUDED.team_json,
      hireable_roles_json = EXCLUDED.hireable_roles_json,
      equipment_json = EXCLUDED.equipment_json,
      certifications_json = EXCLUDED.certifications_json,
      company_keywords = COALESCE($12, company_profiles.company_keywords),
      profile_hash = EXCLUDED.profile_hash,
      updated_at = now()
    RETURNING *
    `,
    [
      tenantId,
      body.ruc,
      body.razon_social,
      JSON.stringify(body.identity_json ?? {}),
      JSON.stringify(body.finance_json ?? {}),
      JSON.stringify(body.experience_json ?? []),
      JSON.stringify(body.econ_experience_json ?? {}),
      JSON.stringify(body.team_json ?? []),
      JSON.stringify(body.hireable_roles_json ?? []),
      JSON.stringify(body.equipment_json ?? []),
      JSON.stringify(body.certifications_json ?? []),
      companyKeywords,
      JSON.stringify(body),
    ],
  );
  await query(
    "INSERT INTO worker_jobs (job_type, queue_name, payload, dedup_key, priority) VALUES ('match_profile', 'match', $1::jsonb, $2, 3) ON CONFLICT DO NOTHING",
    [JSON.stringify({ profile_id: result.rows[0].id }), `match_profile:${result.rows[0].id}`],
  );
  return NextResponse.json({ data: result.rows[0] });
}
