import { query } from "@/server/db/client";
import { getContractForTenant } from "@/server/services/contracts";
import {
  type HistoricalCandidate,
  scoreHistoricalCandidate,
  summarizeComparables,
} from "@/server/services/historicalScoring";

export async function getHistoricalComparables(tenantId: string, idContrato: number) {
  const current = await getContractForTenant(tenantId, idContrato);
  if (!current) return null;
  const contract = current.contract as any;
  const result = await query<HistoricalCandidate>(
    `
    SELECT id_contrato, codigo_completo, descripcion, cubso_item, cubso_segmento,
      entity_name, estado_resultado, supplier_ruc, supplier_name, precio_total,
      fec_publica, source_document_url
    FROM historical_contract_outcomes
    WHERE id_contrato <> $1
      AND (
        cubso_segmento = $2
        OR (cubso_item IS NOT NULL AND $3::text IS NOT NULL AND left(cubso_item, 4) = left($3, 4))
      )
    ORDER BY
      CASE WHEN cubso_item = $3 THEN 0 WHEN left(cubso_item, 4) = left($3, 4) THEN 1 ELSE 2 END,
      similarity(descripcion, $4) DESC,
      fec_publica DESC NULLS LAST
    LIMIT 500
    `,
    [idContrato, contract.cubso_segmento, contract.cubso_item, contract.descripcion],
  );
  const keywordHits = Array.isArray(contract.fit_keyword_hits) ? contract.fit_keyword_hits : [];
  const comparables = result.rows
    .map((candidate) => scoreHistoricalCandidate(candidate, {
      cubsoItem: contract.cubso_item,
      cubsoSegment: contract.cubso_segmento,
      entityName: contract.entidad_nombre,
      keywordHits,
    }))
    .filter((candidate) => candidate.score >= 35 || candidate.keyword_matches.length > 0)
    .sort((a, b) => b.score - a.score || new Date(b.fec_publica ?? 0).getTime() - new Date(a.fec_publica ?? 0).getTime())
    .slice(0, 30);

  return {
    contract: {
      id_contrato: contract.id_contrato,
      codigo: contract.codigo,
      business_line_name: contract.fit_business_line_name,
      keyword_hits: keywordHits,
    },
    metrics: summarizeComparables(comparables),
    comparables,
  };
}
