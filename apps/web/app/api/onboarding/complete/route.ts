import { NextRequest, NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { pool } from "@/server/db/client";
import { keywordSignals } from "@/server/services/keywordSignals";
import { cleanCompanyKeywords } from "@/server/services/companyKeywords";

function strings(value: unknown, max = 20): string[] {
  return Array.isArray(value)
    ? [...new Set(value.map((item) => String(item).trim()).filter(Boolean))].slice(0, max)
    : [];
}

export async function POST(request: NextRequest) {
  const tenantId = await requireTenantId();
  const body = await request.json();
  const company = body.company ?? {};
  const ruc = String(company.ruc ?? "").replace(/\D/g, "");
  const razonSocial = String(company.name ?? "").trim().slice(0, 160);
  const website = String(company.website ?? "").trim().slice(0, 500);
  const summary = String(body.summary ?? "").trim().slice(0, 500);
  const companyKeywords = cleanCompanyKeywords(body.company_keywords);
  const amount = Math.max(0, Number(body.econ_amount ?? 0) || 0);
  const team = strings(body.team).map((role) => ({ role }));
  const equipment = strings(body.equipment);
  const rawLines = Array.isArray(body.business_lines) ? body.business_lines : [];
  const lines = rawLines.slice(0, 8).map((line: any) => ({
    name: String(line?.name ?? "").trim().slice(0, 90),
    ...keywordSignals(line),
    cubso_segmentos: strings(line?.cubso_segmentos, 3),
  })).filter((line: { name: string; keywords: string[]; cubso_segmentos: string[] }) => line.name && line.keywords.length && line.cubso_segmentos.length);

  if (ruc.length !== 11) return NextResponse.json({ error: "El RUC debe tener 11 dígitos." }, { status: 400 });
  if (!razonSocial) return NextResponse.json({ error: "Ingresa la razón social." }, { status: 400 });
  if (!companyKeywords.length) return NextResponse.json({ error: "Agrega al menos una keyword general de empresa." }, { status: 400 });
  if (!lines.length) return NextResponse.json({ error: "Agrega al menos una línea de negocio con keywords." }, { status: 400 });

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const requestedSegments: string[] = Array.from(
      new Set<string>(lines.flatMap((line: { cubso_segmentos: string[] }) => line.cubso_segmentos)),
    );
    const validSegments = await client.query<{ codigo: string }>(
      "SELECT codigo FROM cat_cubso_segmentos WHERE anio = 2026 AND codigo = ANY($1::text[])",
      [requestedSegments],
    );
    const validCodes = new Set(validSegments.rows.map((row) => row.codigo));
    if (requestedSegments.some((codigo) => !validCodes.has(codigo))) throw new Error("El perfil contiene un segmento CUBSO inválido.");
    const profileResult = await client.query<{ id: string }>(
      `
      INSERT INTO company_profiles (
        tenant_id, ruc, razon_social, identity_json, finance_json, experience_json,
        econ_experience_json, team_json, hireable_roles_json, equipment_json,
        certifications_json, company_keywords, profile_hash, updated_at
      )
      VALUES ($1, $2, $3, $4::jsonb, '{}'::jsonb, '[]'::jsonb, $5::jsonb, $6::jsonb, '[]'::jsonb, $7::jsonb, '[]'::jsonb, $8, md5($9), now())
      ON CONFLICT (tenant_id, ruc)
      DO UPDATE SET
        razon_social = EXCLUDED.razon_social,
        identity_json = company_profiles.identity_json || EXCLUDED.identity_json,
        econ_experience_json = EXCLUDED.econ_experience_json,
        team_json = EXCLUDED.team_json,
        equipment_json = EXCLUDED.equipment_json,
        company_keywords = EXCLUDED.company_keywords,
        profile_hash = EXCLUDED.profile_hash,
        updated_at = now()
      RETURNING id
      `,
      [
        tenantId,
        ruc,
        razonSocial,
        JSON.stringify({ website: website || null, company_summary: summary, onboarding_completed_at: new Date().toISOString() }),
        JSON.stringify({ general: amount }),
        JSON.stringify(team),
        JSON.stringify(equipment),
        companyKeywords,
        JSON.stringify({ ruc, razonSocial, website, summary, companyKeywords, amount, team, equipment, lines }),
      ],
    );
    const profileId = profileResult.rows[0].id;
    await client.query("DELETE FROM business_lines WHERE profile_id = $1", [profileId]);
    for (const line of lines) {
      await client.query(
        `INSERT INTO business_lines (profile_id, nombre, cubso_segmentos, keywords, keyword_phrases, keyword_terms, score_umbral) VALUES ($1, $2, $3, $4, $5, $6, 70)`,
        [profileId, line.name, line.cubso_segmentos, line.keywords, line.keyword_phrases, line.keyword_terms],
      );
    }
    await client.query(
      `INSERT INTO worker_jobs (job_type, queue_name, payload, dedup_key, priority)
       VALUES ('match_profile', 'match', $1::jsonb, $2, 3)
       ON CONFLICT DO NOTHING`,
      [JSON.stringify({ profile_id: profileId }), `match_profile:${profileId}`],
    );
    await client.query("COMMIT");
    return NextResponse.json({ data: { profile_id: profileId, lines: lines.length } });
  } catch (error) {
    await client.query("ROLLBACK");
    console.error("onboarding_complete_failed", error);
    return NextResponse.json({ error: "No pudimos guardar el perfil. Inténtalo nuevamente." }, { status: 500 });
  } finally {
    client.release();
  }
}
