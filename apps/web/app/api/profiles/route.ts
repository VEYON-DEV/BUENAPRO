import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { query } from "@/server/db/client";
import { cleanCompanyKeywords } from "@/server/services/companyKeywords";

function jsonBody(value: unknown, fallback: unknown) {
  return JSON.stringify(value ?? fallback);
}

async function enqueueProfileMatch(profileId: string) {
  await query(
    `
    INSERT INTO worker_jobs (job_type, queue_name, payload, dedup_key, priority)
    VALUES ('match_profile', 'match', $1::jsonb, $2, 3)
    ON CONFLICT DO NOTHING
    `,
    [JSON.stringify({ profile_id: profileId }), `match_profile:${profileId}`],
  );
}

export async function GET() {
  const tenantId = await requireTenantId();
  const result = await query(
    `
    SELECT *
    FROM company_profiles
    WHERE tenant_id = $1
    ORDER BY is_active DESC, created_at ASC
    `,
    [tenantId],
  );
  return NextResponse.json({ data: result.rows });
}

export async function POST(request: NextRequest) {
  const tenantId = await requireTenantId();
  const body = await request.json();
  const result = await query(
    `
    INSERT INTO company_profiles (
      tenant_id, ruc, razon_social, identity_json, finance_json, experience_json,
      econ_experience_json, team_json, hireable_roles_json, equipment_json,
      certifications_json, company_keywords, profile_hash, is_active, updated_at
    )
    VALUES (
      $1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb,
      $9::jsonb, $10::jsonb, $11::jsonb, $12, md5($13), COALESCE($14, true), now()
    )
    RETURNING *
    `,
    [
      tenantId,
      body.ruc,
      body.razon_social,
      jsonBody(body.identity_json, {}),
      jsonBody(body.finance_json, {}),
      jsonBody(body.experience_json, []),
      jsonBody(body.econ_experience_json, {}),
      jsonBody(body.team_json, []),
      jsonBody(body.hireable_roles_json, []),
      jsonBody(body.equipment_json, []),
      jsonBody(body.certifications_json, []),
      cleanCompanyKeywords(body.company_keywords),
      JSON.stringify(body),
      body.is_active ?? null,
    ],
  );
  await enqueueProfileMatch(result.rows[0].id);
  return NextResponse.json({ data: result.rows[0] }, { status: 201 });
}
