import { opsQuery } from "./db.ts";
import type { ItemLabel } from "./model.ts";
import type { OpsSource } from "./types.ts";

type MinorLabelRow = {
  item_id: string;
  codigo: string | null;
  entidad: string | null;
  descripcion: string | null;
  scores: Array<{ profile_id: string; score: string | number }> | null;
};

type MajorLabelRow = {
  item_id: string;
  codigo: string | null;
  entidad: string | null;
  descripcion: string | null;
  affinity: string | null;
};

/**
 * Menores: los rechazados y pendientes no están en seace_contracts; su ficha vive en el listing_json de la
 * decisión (claves de SEACE: nomEntidad, desContratacion). El puntaje JEV es el vigente por empresa.
 */
const MINOR_LABELS_SQL = `
SELECT ids.id::text AS item_id,
       COALESCE(c.codigo, dl.codigo) AS codigo,
       COALESCE(c.entidad_nombre, dl.listing_json->>'nomEntidad') AS entidad,
       COALESCE(c.descripcion, dl.listing_json->>'desContratacion') AS descripcion,
       jev.scores
FROM unnest($1::bigint[]) AS ids(id)
LEFT JOIN seace_contracts c ON c.id_contrato = ids.id
LEFT JOIN LATERAL (
  SELECT d.codigo, d.listing_json FROM prod6_profile_decisions d WHERE d.id_contrato = ids.id ORDER BY d.id DESC LIMIT 1
) dl ON true
LEFT JOIN LATERAL (
  SELECT jsonb_agg(jsonb_build_object('profile_id', x.profile_id, 'score', x.score) ORDER BY x.profile_id) AS scores
  FROM (
    SELECT DISTINCT ON (d.profile_id) d.profile_id, d.score
    FROM prod6_profile_decisions d
    WHERE d.id_contrato = ids.id AND d.decided_by = 'jev' AND d.score IS NOT NULL AND d.decision <> 'superseded'
    ORDER BY d.profile_id, d.id DESC
  ) x
) jev ON true`;

const MAJOR_LABELS_SQL = `
SELECT ids.id::text AS item_id, p.nomenclature AS codigo, p.buyer_name AS entidad, p.description AS descripcion,
       (SELECT CASE WHEN ev.result_json->>'affinity_index' ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN ev.result_json->>'affinity_index' END
        FROM prod2_evaluations ev
        WHERE ev.id_convocatoria = ids.id AND ev.kind = 'jev_affinity' AND ev.status = 'done'
          AND ev.policy_version = 'prod2_commercial_union_v1'
        ORDER BY ev.completed_at DESC NULLS LAST, ev.id DESC LIMIT 1) AS affinity
FROM unnest($1::bigint[]) AS ids(id)
JOIN prod2_discoveries p ON p.id_convocatoria = ids.id`;

/** Ficha corta (código, entidad, descripción y puntaje JEV) de cada licitación de una lista. */
export async function loadItemLabels(source: OpsSource, ids: readonly string[]): Promise<Map<string, ItemLabel>> {
  const unique = [...new Set(ids.filter((id) => /^[1-9]\d{0,18}$/.test(id)))];
  const labels = new Map<string, ItemLabel>();
  if (!unique.length) return labels;
  if (source === "menores") {
    const rows = await opsQuery<MinorLabelRow>(MINOR_LABELS_SQL, [unique]);
    for (const row of rows) {
      labels.set(row.item_id, {
        code: row.codigo,
        entity: row.entidad,
        description: row.descripcion,
        jev: (row.scores ?? []).map((entry) => ({ profileId: entry.profile_id, score: Number(entry.score) })).filter((entry) => Number.isFinite(entry.score)),
      });
    }
  } else {
    const rows = await opsQuery<MajorLabelRow>(MAJOR_LABELS_SQL, [unique]);
    for (const row of rows) {
      const affinity = row.affinity == null ? null : Number(row.affinity);
      labels.set(row.item_id, {
        code: row.codigo,
        entity: row.entidad,
        description: row.descripcion,
        jev: affinity != null && Number.isFinite(affinity) ? [{ profileId: null, score: affinity }] : [],
      });
    }
  }
  return labels;
}
