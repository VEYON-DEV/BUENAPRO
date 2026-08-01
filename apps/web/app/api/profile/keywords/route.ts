import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { query } from "@/server/db/client";
import { cleanCompanyKeywords } from "@/server/services/companyKeywords";

export async function PUT(request: NextRequest) {
  const tenantId = await requireTenantId();
  const body = await request.json();
  const keywords = cleanCompanyKeywords(body.company_keywords);
  if (!keywords.length) return NextResponse.json({ error: "Agrega al menos una keyword de empresa." }, { status: 400 });
  const result = await query(
    `UPDATE company_profiles
     SET company_keywords=$2::text[], profile_hash=md5(coalesce(profile_hash,'') || ($2::text[])::text), updated_at=now()
     WHERE id=(
       SELECT id FROM company_profiles
       WHERE tenant_id=$1 AND is_active=true
       ORDER BY created_at LIMIT 1
     )
     RETURNING id, company_keywords`,
    [tenantId, keywords],
  );
  if (!result.rows[0]) return NextResponse.json({ error: "Profile required" }, { status: 400 });
  await query(
    `INSERT INTO worker_jobs (job_type, queue_name, payload, dedup_key, priority)
     VALUES ('match_profile', 'match', $1::jsonb, $2, 3) ON CONFLICT DO NOTHING`,
    [JSON.stringify({ profile_id: result.rows[0].id }), `match_profile:${result.rows[0].id}`],
  );
  return NextResponse.json({ data: result.rows[0] });
}
