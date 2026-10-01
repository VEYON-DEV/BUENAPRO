import { createHash } from "node:crypto";
import { query } from "@/server/db/client";
import { prod6QuotationWindow } from "@/lib/procurementSchedule";

const SEACE_BASE_URL = process.env.SEACE_BASE_URL ?? "https://prod6.seace.gob.pe/v1/s8uit-services";
const DETAIL_TTL_MS = 6 * 60 * 60 * 1000; // 6 horas
const FETCH_TIMEOUT_MS = 8000;

type AnyRecord = Record<string, any>;

function canonicalHash(payload: unknown): string {
  const encoded = JSON.stringify(sortKeys(payload));
  return createHash("sha256").update(encoded, "utf8").digest("hex");
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value as AnyRecord)
        .sort()
        .map((key) => [key, sortKeys((value as AnyRecord)[key])]),
    );
  }
  return value;
}

function locationPart(value: string | null | undefined, index: number): string | null {
  if (!value) return null;
  const parts = value.split("/").map((part) => part.trim()).filter(Boolean);
  return parts[index] ?? null;
}

/**
 * Refresca el detalle SEACE bajo demanda (al abrir la vista de detalle).
 * Si el detalle cacheado tiene menos de 6 h, no toca SEACE.
 * Nunca lanza: ante error de red se sirve el detalle cacheado.
 */
export async function refreshContractDetailIfStale(idContrato: number): Promise<void> {
  const current = await query(
    "SELECT detail_fetched_at, raw_detail_json IS NULL AS missing FROM seace_contracts WHERE id_contrato = $1",
    [idContrato],
  );
  const row = current.rows[0];
  if (!row) return;
  const fetchedAt = row.detail_fetched_at ? new Date(row.detail_fetched_at).getTime() : 0;
  if (!row.missing && Date.now() - fetchedAt < DETAIL_TTL_MS) return;

  let detail: AnyRecord;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    const response = await fetch(
      `${SEACE_BASE_URL}/buscadorpublico/contrataciones/listar-completo?id_contrato=${idContrato}`,
      {
        headers: { "User-Agent": "BuenaPro/0.1 (worker)" },
        signal: controller.signal,
        cache: "no-store",
      },
    );
    clearTimeout(timer);
    if (!response.ok) return;
    detail = await response.json();
  } catch {
    return;
  }

  const projection: AnyRecord = detail?.uitContratoCompletoProjection ?? {};
  const items: AnyRecord[] = Array.isArray(detail?.uitContratoItemProjectionList)
    ? detail.uitContratoItemProjectionList
    : [];
  const item = items[0] ?? {};
  const etapas = Array.isArray(detail?.uitContratoEtapaProjectionList)
    ? detail.uitContratoEtapaProjectionList
    : [];
  const hashDetail = canonicalHash(detail);
  const quotationWindow = prod6QuotationWindow(detail);

  await query(
    `
    UPDATE seace_contracts
    SET estado_codigo = COALESCE($2, estado_codigo),
        entidad_nombre = COALESCE($3, entidad_nombre),
        cubso_item = COALESCE($4, cubso_item),
        departamento = COALESCE($5, departamento),
        provincia = COALESCE($6, provincia),
        distrito = COALESCE($7, distrito),
        cronograma = $8::jsonb,
        hash_detail = $9,
        raw_detail_json = $10::jsonb,
        fec_ini_cotizacion = $11::timestamptz,
        fec_fin_cotizacion = $12::timestamptz,
        detail_fetched_at = now(),
        schedule_fetched_at = now(),
        updated_at = CASE WHEN hash_detail IS DISTINCT FROM $9 THEN now() ELSE updated_at END
    WHERE id_contrato = $1
    `,
    [
      idContrato,
      projection.idEstadoContrato ?? null,
      projection.nomEntidad ?? null,
      item.codCubso ?? null,
      locationPart(item.nomDistritoExt, 0),
      locationPart(item.nomDistritoExt, 1),
      locationPart(item.nomDistritoExt, 2),
      JSON.stringify({ etapas }),
      hashDetail,
      JSON.stringify(detail),
      quotationWindow.startsAt,
      quotationWindow.endsAt,
    ],
  );
}
