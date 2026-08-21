import type pg from "pg";
import { pool, query } from "@/server/db/client";
import { authenticatedSeaceJson } from "@/server/services/seaceAuthenticated";
import {
  ensureDefaultTasks,
  ensureMatchForContract,
} from "@/server/services/tracking";

type JsonObject = Record<string, unknown>;

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function list(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.map(object) : [];
}

function numberOrNull(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export async function startApplication(
  tenantId: string,
  idContrato: number,
  actorId?: string | null,
) {
  const ensured = await ensureMatchForContract(tenantId, idContrato);
  if ("error" in ensured) return ensured;

  // La llamada externa sucede antes de abrir la transaccion para no retener conexiones DB.
  const [quoteRaw, contractRaw] = await Promise.all([
    authenticatedSeaceJson(
      tenantId,
      `/cotizacion/cotizaciones/listar-completo?id_contrato=${idContrato}`,
    ),
    authenticatedSeaceJson(
      tenantId,
      `/contratacion/contrataciones/obtener-completo?id_contrato=${idContrato}`,
    ),
  ]);
  const quote = object(quoteRaw);
  const contract = object(contractRaw);
  const items = list(quote.uitContratoItemCotizacionProjectionList);
  const requirements = list(quote.uitContratoRtmCotizacionProjectionList);
  const documents = list(quote.contratoArchivoCotizacionProjectionList);

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const matchId = Number(ensured.data.id);
    await client.query(
      "UPDATE matches SET user_state='en_preparacion', updated_at=now() WHERE id=$1",
      [matchId],
    );
    const draftResult = await client.query<{ id: string }>(
      `INSERT INTO application_drafts (match_id,quote_type_id,seace_quote_snapshot,seace_contract_snapshot,synced_at)
       VALUES ($1,$2,$3::jsonb,$4::jsonb,now())
       ON CONFLICT (match_id) DO UPDATE SET
         quote_type_id=EXCLUDED.quote_type_id,
         seace_quote_snapshot=EXCLUDED.seace_quote_snapshot,
         seace_contract_snapshot=EXCLUDED.seace_contract_snapshot,
         synced_at=now(),updated_at=now()
       RETURNING id`,
      [
        matchId,
        numberOrNull(quote.idTipoCotizacion),
        JSON.stringify(quote),
        JSON.stringify(contract),
      ],
    );
    const applicationId = draftResult.rows[0].id;
    await client.query(
      `UPDATE chat_sessions SET match_id=$3,application_id=$4,updated_at=now()
       WHERE tenant_id=$1 AND id_contrato=$2
         AND (match_id IS NULL OR application_id IS NULL)`,
      [tenantId, idContrato, matchId, applicationId],
    );
    await upsertItems(client, applicationId, items);
    await upsertRequirements(client, applicationId, requirements);
    await upsertDocuments(client, applicationId, documents);
    await client.query(
      `INSERT INTO match_events (match_id,event_type,payload,actor_id)
       VALUES ($1,'application_started',$2::jsonb,$3)`,
      [
        matchId,
        JSON.stringify({
          application_id: applicationId,
          id_contrato: idContrato,
        }),
        actorId ?? null,
      ],
    );
    await client.query("COMMIT");
    await ensureDefaultTasks(matchId);
    return {
      data: { matchId, applicationId, url: `/postulaciones/${matchId}` },
      status: 200 as const,
    };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

async function upsertItems(
  client: pg.PoolClient,
  applicationId: string,
  rows: JsonObject[],
) {
  for (const row of rows) {
    const seaceId = numberOrNull(row.idContratoItem);
    if (seaceId == null) continue;
    await client.query(
      `INSERT INTO application_items
       (application_id,seace_item_id,sequence,description,cubso_code,cubso_name,unit_name,quantity,currency_id,currency_name,exchange_rate,unit_price,total_price,source_json)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
       ON CONFLICT (application_id,seace_item_id) DO UPDATE SET
         sequence=EXCLUDED.sequence,description=EXCLUDED.description,cubso_code=EXCLUDED.cubso_code,
         cubso_name=EXCLUDED.cubso_name,unit_name=EXCLUDED.unit_name,quantity=EXCLUDED.quantity,
         currency_id=EXCLUDED.currency_id,currency_name=EXCLUDED.currency_name,exchange_rate=EXCLUDED.exchange_rate,
         source_json=EXCLUDED.source_json,updated_at=now()`,
      [
        applicationId,
        seaceId,
        numberOrNull(row.secuencia),
        stringOrNull(row.descripcionItem) ??
          stringOrNull(row.nomCubso) ??
          "Ítem",
        stringOrNull(row.codCubso),
        stringOrNull(row.nomCubso),
        stringOrNull(row.nomUnidadMedida),
        numberOrNull(row.cantidad),
        numberOrNull(row.idMoneda),
        stringOrNull(row.nomMoneda),
        numberOrNull(row.tipoCambio),
        numberOrNull(row.precioUnitario),
        numberOrNull(row.precioTotal),
        JSON.stringify(row),
      ],
    );
  }
}

async function upsertRequirements(
  client: pg.PoolClient,
  applicationId: string,
  rows: JsonObject[],
) {
  for (const row of rows) {
    const seaceId = numberOrNull(row.idContratoRtm);
    if (seaceId == null) continue;
    await client.query(
      `INSERT INTO application_requirements
       (application_id,seace_requirement_id,sequence,name,requested_value,offered_value,source_json)
       VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
       ON CONFLICT (application_id,seace_requirement_id) DO UPDATE SET
         sequence=EXCLUDED.sequence,name=EXCLUDED.name,requested_value=EXCLUDED.requested_value,
         source_json=EXCLUDED.source_json,updated_at=now()`,
      [
        applicationId,
        seaceId,
        numberOrNull(row.secuencia),
        stringOrNull(row.nomRtm) ?? "Requisito",
        stringOrNull(row.valorConRtm),
        stringOrNull(row.valorCotRtm),
        JSON.stringify(row),
      ],
    );
  }
}

async function upsertDocuments(
  client: pg.PoolClient,
  applicationId: string,
  rows: JsonObject[],
) {
  for (const row of rows) {
    const seaceId = numberOrNull(row.idContratoArchivo);
    if (seaceId == null) continue;
    await client.query(
      `INSERT INTO application_documents
       (application_id,seace_document_id,sequence,type_id,type_name,filename,extension,mime,size_bytes,download_path,source_json)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
       ON CONFLICT (application_id,seace_document_id) DO UPDATE SET
         sequence=EXCLUDED.sequence,type_id=EXCLUDED.type_id,type_name=EXCLUDED.type_name,
         filename=EXCLUDED.filename,extension=EXCLUDED.extension,mime=EXCLUDED.mime,
         size_bytes=EXCLUDED.size_bytes,download_path=EXCLUDED.download_path,source_json=EXCLUDED.source_json,updated_at=now()`,
      [
        applicationId,
        seaceId,
        numberOrNull(row.secuencia),
        numberOrNull(row.idTipoArchivo),
        stringOrNull(row.nomTipoArchivo),
        stringOrNull(row.nombreArchivo) ?? `Documento ${seaceId}`,
        stringOrNull(row.desExtension),
        stringOrNull(row.desMime),
        numberOrNull(row.tamanio),
        stringOrNull(row.desRutaArchivo),
        JSON.stringify(row),
      ],
    );
  }
}

export async function getApplication(tenantId: string, matchId: number) {
  const result = await query(
    `SELECT ad.*,m.id_contrato,m.user_state,m.responsable_id,m.monto_ofertado,m.notas,
            c.codigo,c.descripcion,c.entidad_nombre,c.fec_fin_cotizacion,
            COALESCE((SELECT jsonb_agg(to_jsonb(ai) ORDER BY ai.sequence,ai.id) FROM application_items ai WHERE ai.application_id=ad.id),'[]') AS items,
            COALESCE((SELECT jsonb_agg(to_jsonb(ar) ORDER BY ar.sequence,ar.id) FROM application_requirements ar WHERE ar.application_id=ad.id),'[]') AS requirements,
            COALESCE((SELECT jsonb_agg(to_jsonb(doc) || jsonb_build_object('local_document_id',cd.id) ORDER BY doc.sequence,doc.id)
                      FROM application_documents doc
                      LEFT JOIN contract_documents cd ON cd.id_contrato=m.id_contrato AND cd.id_contrato_archivo=doc.seace_document_id
                      WHERE doc.application_id=ad.id),'[]') AS documents,
            COALESCE((SELECT jsonb_agg(jsonb_build_object(
              'id',aa.id,
              'name',aa.filename,
              'mime',aa.mime_type,
              'sizeBytes',aa.size_bytes,
              'createdAt',aa.created_at,
              'downloadUrl','/api/applications/' || m.id || '/attachments/' || aa.id
            ) ORDER BY aa.created_at DESC)
            FROM application_attachments aa WHERE aa.application_id=ad.id),'[]') AS attachments
     FROM application_drafts ad
     JOIN matches m ON m.id=ad.match_id
     JOIN company_profiles cp ON cp.id=m.profile_id
     JOIN seace_contracts c ON c.id_contrato=m.id_contrato
     WHERE cp.tenant_id=$1 AND m.id=$2`,
    [tenantId, matchId],
  );
  return result.rows[0] ?? null;
}

export type ApplicationAttachmentInput = {
  filename: string;
  mimeType: string;
  sizeBytes: number;
  content: Buffer;
};

export async function createApplicationAttachment(
  tenantId: string,
  matchId: number,
  input: ApplicationAttachmentInput,
) {
  const result = await query<{
    id: string;
    filename: string;
    mime_type: string;
    size_bytes: number;
    created_at: Date;
  }>(
    `INSERT INTO application_attachments (application_id,filename,mime_type,size_bytes,content)
     SELECT ad.id,$3,$4,$5,$6
     FROM application_drafts ad
     JOIN matches m ON m.id=ad.match_id
     JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE cp.tenant_id=$1 AND m.id=$2
     RETURNING id,filename,mime_type,size_bytes,created_at`,
    [
      tenantId,
      matchId,
      input.filename,
      input.mimeType,
      input.sizeBytes,
      input.content,
    ],
  );
  const attachment = result.rows[0];
  if (!attachment) return null;
  return {
    id: attachment.id,
    name: attachment.filename,
    mime: attachment.mime_type,
    sizeBytes: attachment.size_bytes,
    createdAt: attachment.created_at,
    downloadUrl: `/api/applications/${matchId}/attachments/${attachment.id}`,
  };
}

export async function getApplicationAttachment(
  tenantId: string,
  matchId: number,
  attachmentId: string,
) {
  const result = await query<{
    filename: string;
    mime_type: string;
    size_bytes: number;
    content: Buffer;
  }>(
    `SELECT aa.filename,aa.mime_type,aa.size_bytes,aa.content
     FROM application_attachments aa
     JOIN application_drafts ad ON ad.id=aa.application_id
     JOIN matches m ON m.id=ad.match_id
     JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE cp.tenant_id=$1 AND m.id=$2 AND aa.id=$3`,
    [tenantId, matchId, attachmentId],
  );
  return result.rows[0] ?? null;
}

export async function deleteApplicationAttachment(
  tenantId: string,
  matchId: number,
  attachmentId: string,
) {
  const result = await query<{ id: string }>(
    `DELETE FROM application_attachments aa
     USING application_drafts ad,matches m,company_profiles cp
     WHERE aa.application_id=ad.id AND ad.match_id=m.id AND m.profile_id=cp.id
       AND cp.tenant_id=$1 AND m.id=$2 AND aa.id=$3
     RETURNING aa.id`,
    [tenantId, matchId, attachmentId],
  );
  return result.rows[0] ?? null;
}

export async function updateApplication(
  tenantId: string,
  matchId: number,
  body: JsonObject,
  actorId?: string | null,
) {
  const allowedStatus =
    body.status === "draft" || body.status === "ready_for_review"
      ? body.status
      : null;
  const result = await query(
    `UPDATE application_drafts ad SET
       status=COALESCE($3,status),validity_date=COALESCE($4::date,validity_date),
       contact_email=COALESCE($5,contact_email),contact_phone=COALESCE($6,contact_phone),updated_at=now()
     FROM matches m JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE ad.match_id=m.id AND cp.tenant_id=$1 AND m.id=$2 RETURNING ad.*`,
    [
      tenantId,
      matchId,
      allowedStatus,
      stringOrNull(body.validity_date),
      stringOrNull(body.contact_email),
      stringOrNull(body.contact_phone),
    ],
  );
  if (!result.rows[0]) return null;
  await query(
    "INSERT INTO match_events (match_id,event_type,payload,actor_id) VALUES ($1,'application_updated',$2::jsonb,$3)",
    [matchId, JSON.stringify(body), actorId ?? null],
  );
  return result.rows[0];
}

export async function markApplicationSubmitted(
  tenantId: string,
  matchId: number,
  actorId?: string | null,
) {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const locked = await client.query<{ application_id: string }>(
      `SELECT ad.id AS application_id
       FROM application_drafts ad
       JOIN matches m ON m.id=ad.match_id
       JOIN company_profiles cp ON cp.id=m.profile_id
       WHERE cp.tenant_id=$1 AND m.id=$2
       FOR UPDATE OF ad`,
      [tenantId, matchId],
    );
    if (!locked.rows[0]) {
      await client.query("ROLLBACK");
      return null;
    }
    const readiness = await client.query<{
      application_id: string;
      status: string;
      validity_date: Date | null;
      contact_email: string | null;
      contact_phone: string | null;
      selected_items: number;
      priced_items: number;
      requirements: number;
      answered_requirements: number;
      attachments: number;
      total_amount: string | number | null;
    }>(
      `SELECT ad.id AS application_id,ad.status,ad.validity_date,ad.contact_email,ad.contact_phone,
              count(DISTINCT ai.id) FILTER (WHERE ai.selected)::int AS selected_items,
              count(DISTINCT ai.id) FILTER (WHERE ai.selected AND ai.unit_price IS NOT NULL)::int AS priced_items,
              count(DISTINCT ar.id)::int AS requirements,
              count(DISTINCT ar.id) FILTER (WHERE NULLIF(BTRIM(ar.offered_value),'') IS NOT NULL)::int AS answered_requirements,
              count(DISTINCT aa.id)::int AS attachments,
              COALESCE((SELECT SUM(total_price) FROM application_items WHERE application_id=ad.id AND selected),0) AS total_amount
       FROM application_drafts ad
       LEFT JOIN application_items ai ON ai.application_id=ad.id
       LEFT JOIN application_requirements ar ON ar.application_id=ad.id
       LEFT JOIN application_attachments aa ON aa.application_id=ad.id
       WHERE ad.id=$1
       GROUP BY ad.id`,
      [locked.rows[0].application_id],
    );
    const application = readiness.rows[0];
    if (!application) {
      await client.query("ROLLBACK");
      return null;
    }
    const missing: string[] = [];
    if (
      application.selected_items === 0 ||
      application.priced_items !== application.selected_items
    )
      missing.push("oferta y precios");
    if (
      !application.validity_date ||
      !application.contact_email ||
      !application.contact_phone
    )
      missing.push("vigencia y contacto");
    if (application.answered_requirements !== application.requirements)
      missing.push("RTM");
    if (application.attachments === 0) missing.push("propuesta adjunta");
    if (missing.length) {
      await client.query("ROLLBACK");
      return {
        error: `Completa ${missing.join(", ")} antes de registrar la presentación.`,
      };
    }
    const updated = await client.query(
      `UPDATE application_drafts
       SET status='submitted',total_amount=$2,updated_at=now()
       WHERE id=$1
       RETURNING *`,
      [application.application_id, application.total_amount],
    );
    await client.query(
      `UPDATE matches
       SET user_state='postulada',monto_ofertado=$2,updated_at=now()
       WHERE id=$1`,
      [matchId, application.total_amount],
    );
    await client.query(
      `INSERT INTO match_events (match_id,event_type,payload,actor_id)
       VALUES ($1,'application_marked_submitted',$2::jsonb,$3)`,
      [
        matchId,
        JSON.stringify({
          application_id: application.application_id,
          channel: "seace_manual",
        }),
        actorId ?? null,
      ],
    );
    await client.query("COMMIT");
    return { data: updated.rows[0] };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function updateApplicationItem(
  tenantId: string,
  matchId: number,
  body: JsonObject,
) {
  const id = numberOrNull(body.id);
  if (id == null) return null;
  const selected = typeof body.selected === "boolean" ? body.selected : null;
  const unitPrice = numberOrNull(body.unit_price);
  const result = await query(
    `UPDATE application_items ai SET selected=COALESCE($4,selected),unit_price=COALESCE($5,unit_price),
       total_price=CASE WHEN $5::numeric IS NOT NULL THEN ROUND(COALESCE(quantity,1)*$5::numeric,2) ELSE total_price END,updated_at=now()
     FROM application_drafts ad JOIN matches m ON m.id=ad.match_id JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE ai.application_id=ad.id AND cp.tenant_id=$1 AND m.id=$2 AND ai.id=$3 RETURNING ai.*`,
    [tenantId, matchId, id, selected, unitPrice],
  );
  if (!result.rows[0]) return null;
  await recalculateTotal(result.rows[0].application_id as string);
  return result.rows[0];
}

export async function updateApplicationRequirement(
  tenantId: string,
  matchId: number,
  body: JsonObject,
) {
  const id = numberOrNull(body.id);
  if (id == null) return null;
  const result = await query(
    `UPDATE application_requirements ar SET offered_value=$4,updated_at=now()
     FROM application_drafts ad JOIN matches m ON m.id=ad.match_id JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE ar.application_id=ad.id AND cp.tenant_id=$1 AND m.id=$2 AND ar.id=$3 RETURNING ar.*`,
    [
      tenantId,
      matchId,
      id,
      body.offered_value == null ? null : String(body.offered_value),
    ],
  );
  return result.rows[0] ?? null;
}

export async function updateApplicationDocument(
  tenantId: string,
  matchId: number,
  body: JsonObject,
) {
  const id = numberOrNull(body.id);
  if (id == null || typeof body.reviewed !== "boolean") return null;
  const result = await query(
    `UPDATE application_documents doc SET reviewed=$4,updated_at=now()
     FROM application_drafts ad JOIN matches m ON m.id=ad.match_id JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE doc.application_id=ad.id AND cp.tenant_id=$1 AND m.id=$2 AND doc.id=$3 RETURNING doc.*`,
    [tenantId, matchId, id, body.reviewed],
  );
  return result.rows[0] ?? null;
}

async function recalculateTotal(applicationId: string) {
  await query(
    `UPDATE application_drafts SET total_amount=(SELECT COALESCE(SUM(total_price),0) FROM application_items WHERE application_id=$1 AND selected=true),updated_at=now() WHERE id=$1`,
    [applicationId],
  );
}
