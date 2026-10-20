import { createHash } from "node:crypto";

export const PROD2_DIN_AFFINITY_POLICY = "din_title_commercial_affinity_v1";
const whitespace = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u;
const compact = (value: string) => value.split(whitespace).filter(Boolean).join(" ");
const sha = (value: string) => createHash("sha256").update(value, "utf8").digest("hex");
export type Prod2CommercialContext = { profileHash: string; commercialHash: string };

/** Same one-company, commercial-only input as the DIN JEV publisher. */
export function prod2CommercialContext(value: { profile_hash?: unknown; commercial_description?: unknown; active_business_lines?: unknown }): Prod2CommercialContext | null {
  if (typeof value.profile_hash !== "string" || !/^[a-f0-9]{64}$/.test(value.profile_hash)
    || typeof value.commercial_description !== "string" || !Array.isArray(value.active_business_lines)
    || value.active_business_lines.some(line => typeof line !== "string")) return null;
  const descripcion = compact(value.commercial_description);
  const lineas = [...new Set((value.active_business_lines as string[]).map(compact).filter(Boolean))]
    .sort((left, right) => Buffer.compare(Buffer.from(left), Buffer.from(right)));
  if (!descripcion && !lineas.length) return null;
  return { profileHash: value.profile_hash, commercialHash: sha(JSON.stringify([{ descripcion, lineas }])) };
}

/** Strict current-profile projection. Legacy joint/global JEV remains separate. */
export function prod2CommercialAffinityJoin(values: unknown[], context: Prod2CommercialContext | null, tenantId: string) {
  if (!context) return `LEFT JOIN LATERAL (SELECT NULL::jsonb AS result_json, NULL::double precision AS affinity_index) commercial_jev ON true`;
  values.push(tenantId, context.profileHash, context.commercialHash);
  const tenant = values.length - 2, profileHash = values.length - 1, commercialHash = values.length;
  return `LEFT JOIN LATERAL (
    SELECT ev.result_json, (ev.result_json->>'affinity_index')::double precision AS affinity_index
    FROM prod2_evaluations ev
    WHERE ev.id_convocatoria = p.id_convocatoria AND ev.kind = 'jev_affinity' AND ev.status = 'done'
      AND ev.profile_id = $1::uuid AND ev.model = 'jev-1.13.0' AND ev.policy_version = '${PROD2_DIN_AFFINITY_POLICY}'
      AND ev.input_tokens >= 0 AND ev.output_tokens >= 0 AND ev.cost_usd >= 0
      AND ev.result_json->>'model' = ev.model
      AND ev.result_json->>'policy_version' = 'prod2_commercial_union_v1'
      AND ev.result_json->'not_qualification_or_win_probability' = 'true'::jsonb
      AND ev.result_json->'meta'->>'scope' = '${PROD2_DIN_AFFINITY_POLICY}'
      AND ev.result_json->'meta'->>'analysis_basis' = 'title'
      AND ev.result_json->'meta'->>'profile_id' = $1::text
      AND ev.result_json->'meta'->>'tenant_id' = $${tenant}::text
      AND ev.result_json->'meta'->>'profile_hash' = $${profileHash}::text
      AND ev.result_json->'meta'->>'commercial_profile_hash' = $${commercialHash}::text
      AND ev.result_json->'meta'->>'id_convocatoria' = p.id_convocatoria::text
      AND ev.result_json->'meta'->>'source_row_hash' = p.row_hash
      AND ev.result_json->'meta'->>'source_description_sha256' = encode(sha256(convert_to(p.description, 'UTF8')), 'hex')
      AND ev.result_json->'meta'->>'canonical_binding_hash' = ev.request_hash
      AND CASE WHEN jsonb_typeof(ev.result_json->'affinity_index') = 'number'
        THEN (ev.result_json->>'affinity_index')::numeric BETWEEN 0 AND 100 ELSE false END
    ORDER BY ev.completed_at DESC, ev.id DESC LIMIT 1
  ) commercial_jev ON true`;
}
