import { apiFetch } from "@/lib/api/client";
import type { ApplicationData } from "../model/types";

const array = (value: unknown) => (Array.isArray(value) ? value : []);
const text = (...values: unknown[]) =>
  String(
    values.find(
      (value) => value !== undefined && value !== null && value !== "",
    ) ?? "",
  );
const number = (...values: unknown[]) => {
  const value = values.find(
    (item) => item !== undefined && item !== null && item !== "",
  );
  return value == null ? null : Number(value);
};

export function normalizeApplication(
  payload: any,
  matchId: string,
): ApplicationData {
  const root = payload?.application ?? payload?.data ?? payload ?? {};
  const contract = root.contract ?? root.contrato ?? {};
  const draft = root.draft ?? root.borrador ?? root;
  return {
    matchId,
    contractId:
      number(
        root.contractId,
        root.idContrato,
        root.id_contrato,
        contract.idContrato,
        contract.id_contrato,
      ) ?? undefined,
    code: text(
      contract.code,
      contract.codigo,
      root.code,
      root.codigo,
      "Proceso SEACE",
    ),
    title: text(
      contract.title,
      contract.description,
      contract.descripcion,
      root.title,
      root.descripcion,
      "Postulación",
    ),
    entity: text(
      contract.entity,
      contract.entidadNombre,
      contract.entidad_nombre,
      root.entity,
      root.entidad_nombre,
    ),
    deadline:
      text(
        contract.deadline,
        contract.fecFinCotizacion,
        contract.fec_fin_cotizacion,
        root.deadline,
        root.fec_fin_cotizacion,
      ) || undefined,
    status: text(
      draft.status,
      draft.estado,
      root.userState,
      root.user_state,
      "draft",
    ),
    responsibleId:
      text(root.responsibleId, root.responsableId, root.responsable_id) ||
      undefined,
    validity:
      text(
        draft.validity,
        draft.vigenciaCotizacion,
        draft.validityDate,
        draft.validity_date,
      ) || undefined,
    contactEmail:
      text(draft.contactEmail, draft.correoContacto, draft.contact_email) ||
      undefined,
    contactPhone:
      text(draft.contactPhone, draft.celularContacto, draft.contact_phone) ||
      undefined,
    currency: text(draft.currency, draft.moneda, "PEN"),
    total:
      number(draft.total, draft.montoTotal, draft.total_amount) ?? undefined,
    items: array(root.items ?? draft.items ?? root.quotationItems).map(
      (item: any, index) => ({
        id: text(item.id, item.itemId, item.idDetalle, index),
        selected: Boolean(item.selected ?? item.seleccionado),
        description: text(
          item.description,
          item.descripcion,
          item.nomItem,
          `Ítem ${index + 1}`,
        ),
        unit: text(
          item.unit,
          item.unidadMedida,
          item.nomUnidadMedida,
          item.unit_name,
        ),
        quantity: number(item.quantity, item.cantidad) ?? 0,
        currency: text(item.currency, item.moneda, item.currency_name, "SOLES"),
        unitPrice: number(item.unitPrice, item.precioUnitario, item.unit_price),
        total: number(item.total, item.precioTotal, item.total_price),
      }),
    ),
    requirements: array(
      root.requirements ?? draft.requirements ?? root.rtms,
    ).map((item: any, index) => ({
      id: text(item.id, item.requirementId, item.idRtm, index),
      description: text(
        item.description,
        item.descripcion,
        item.rtm,
        item.name,
        `Requisito ${index + 1}`,
      ),
      requested: text(
        item.requested,
        item.valorSolicitado,
        item.rtmSolicitado,
        item.requested_value,
      ),
      offered: text(
        item.offered,
        item.valorCotRtm,
        item.rtmOfertado,
        item.offered_value,
      ),
      completed: Boolean(item.completed),
    })),
    documents: array(root.documents ?? draft.documents).map(
      (doc: any, index) => ({
        id: text(doc.id, doc.documentId, index),
        localDocumentId: number(doc.local_document_id) ?? undefined,
        name: text(
          doc.name,
          doc.filename,
          doc.nombre,
          `Documento ${index + 1}`,
        ),
        mime: text(doc.mime, doc.contentType),
        category: text(doc.category, doc.categoria, doc.type_name),
        downloadUrl: text(doc.downloadUrl, doc.url) || undefined,
        status: text(doc.status, doc.estado, doc.type_name),
        reviewed: Boolean(doc.reviewed),
      }),
    ),
    attachments: array(root.attachments ?? draft.attachments).map(
      (file: any, index) => ({
        id: text(file.id, index),
        name: text(file.name, file.filename, `Adjunto ${index + 1}`),
        mime: text(file.mime, file.content_type),
        sizeBytes: number(file.sizeBytes, file.size_bytes) ?? undefined,
        createdAt: text(file.createdAt, file.created_at) || undefined,
        downloadUrl: text(
          file.downloadUrl,
          file.download_url,
          `/api/applications/${matchId}/attachments/${file.id}`,
        ),
      }),
    ),
    events: array(root.events ?? root.activity),
  };
}

export async function getApplication(matchId: string) {
  return normalizeApplication(
    await apiFetch(`/api/applications/${matchId}`),
    matchId,
  );
}
export async function patchApplication(matchId: string, json: any) {
  return apiFetch(`/api/applications/${matchId}`, {
    method: "PATCH",
    json: {
      status: json.status,
      validity_date: json.validity,
      contact_email: json.contactEmail,
      contact_phone: json.contactPhone,
    },
  });
}
export async function patchItem(matchId: string, itemId: string, json: any) {
  return apiFetch(`/api/applications/${matchId}/items`, {
    method: "PATCH",
    json: { id: itemId, selected: json.selected, unit_price: json.unitPrice },
  });
}
export async function patchRequirement(
  matchId: string,
  requirementId: string,
  json: any,
) {
  return apiFetch(`/api/applications/${matchId}/requirements`, {
    method: "PATCH",
    json: { id: requirementId, offered_value: json.offered },
  });
}
export async function patchDocument(
  matchId: string,
  documentId: string,
  reviewed: boolean,
) {
  return apiFetch(`/api/applications/${matchId}/documents`, {
    method: "PATCH",
    json: { id: documentId, reviewed },
  });
}
export async function uploadAttachment(matchId: string, file: File) {
  const form = new FormData();
  form.set("file", file);
  const response = await fetch(`/api/applications/${matchId}/attachments`, {
    method: "POST",
    body: form,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(payload?.error ?? "No se pudo subir el archivo.");
  return payload;
}
export async function deleteAttachment(matchId: string, attachmentId: string) {
  const response = await fetch(
    `/api/applications/${matchId}/attachments/${attachmentId}`,
    { method: "DELETE" },
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(payload?.error ?? "No se pudo eliminar el archivo.");
  return payload;
}
