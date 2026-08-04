import { apiFetch } from "@/lib/api/client";
import type { AlertItem, AlertPayload } from "../model/types";

type NotificationRow = {
  id: number;
  reason?: string | null;
  status?: string | null;
  read_at?: string | Date | null;
  created_at: string | Date;
  payload?: AlertPayload | null;
};

type NotificationResponse = { data: NotificationRow[] };

export async function getAlertInbox(): Promise<AlertItem[]> {
  const response = await apiFetch<NotificationResponse>("/api/notifications?channel=in_app&page_size=40");
  return (response.data ?? []).map((notification) => ({
    id: notification.id,
    reason: notification.reason ?? "automated_evaluation",
    status: notification.status ?? "sent",
    read_at: notification.read_at ?? notification.payload?.read_at ?? null,
    created_at: notification.created_at,
    score: notification.payload?.score ?? null,
    verdict: notification.payload?.verdict ?? notification.payload?.veredicto ?? null,
    id_contrato: notification.payload?.id_contrato ?? null,
    codigo: notification.payload?.codigo ?? null,
    descripcion: notification.payload?.descripcion ?? notification.payload?.title ?? notification.payload?.summary ?? null,
    entidad_nombre: notification.payload?.entidad_nombre ?? notification.payload?.entity ?? null,
    fec_fin_cotizacion: notification.payload?.deadline ?? notification.payload?.cierre ?? null,
    payload: notification.payload ?? null,
  }));
}
