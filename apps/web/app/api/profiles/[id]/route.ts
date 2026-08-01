import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { query } from "@/server/db/client";
import { buildPatch, jsonError } from "@/server/services/crud";
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

export async function GET(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  const result = await query("SELECT * FROM company_profiles WHERE tenant_id = $1 AND id = $2", [tenantId, id]);
  if (!result.rows[0]) return jsonError("Not found", 404);
  return NextResponse.json({ data: result.rows[0] });
}

export async function PATCH(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  const body = await request.json();
  if (body.company_keywords != null && !cleanCompanyKeywords(body.company_keywords).length) {
    return NextResponse.json({ error: "Agrega al menos una keyword de empresa válida." }, { status: 400 });
  }
  const patch = buildPatch(
    body,
    [
      { column: "ruc" },
      { column: "razon_social" },
      { column: "identity_json", cast: "::jsonb", transform: (value) => jsonBody(value, {}) },
      { column: "finance_json", cast: "::jsonb", transform: (value) => jsonBody(value, {}) },
      { column: "experience_json", cast: "::jsonb", transform: (value) => jsonBody(value, []) },
      { column: "econ_experience_json", cast: "::jsonb", transform: (value) => jsonBody(value, {}) },
      { column: "team_json", cast: "::jsonb", transform: (value) => jsonBody(value, []) },
      { column: "hireable_roles_json", cast: "::jsonb", transform: (value) => jsonBody(value, []) },
      { column: "equipment_json", cast: "::jsonb", transform: (value) => jsonBody(value, []) },
      { column: "certifications_json", cast: "::jsonb", transform: (value) => jsonBody(value, []) },
      { column: "company_keywords", transform: (value) => cleanCompanyKeywords(value) },
      { column: "is_active" },
    ],
    3,
  );
  if (!patch.sets.length) return jsonError("No patch fields provided");

  const result = await query(
    `
    UPDATE company_profiles
    SET ${patch.sets.join(", ")},
        profile_hash = md5($${patch.values.length + 3}::text),
        updated_at = now()
    WHERE tenant_id = $1 AND id = $2
    RETURNING *
    `,
    [tenantId, id, ...patch.values, JSON.stringify(body)],
  );
  if (!result.rows[0]) return jsonError("Not found", 404);
  await enqueueProfileMatch(id);
  return NextResponse.json({ data: result.rows[0] });
}

export async function DELETE(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const tenantId = await requireTenantId();
  const { id } = await context.params;
  const result = await query(
    `
    UPDATE company_profiles
    SET is_active = false, updated_at = now()
    WHERE tenant_id = $1 AND id = $2
    RETURNING *
    `,
    [tenantId, id],
  );
  if (!result.rows[0]) return jsonError("Not found", 404);
  return NextResponse.json({ data: result.rows[0] });
}
