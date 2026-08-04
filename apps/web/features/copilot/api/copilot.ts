import { apiFetch } from "@/lib/api/client";
import type { CopilotChangeSet, CopilotMessage, CopilotSession } from "../model/types";

const list = (value: unknown) => (Array.isArray(value) ? value : []);

function normalizeChangeSet(raw: any): CopilotChangeSet | null {
  if (!raw?.id) return null;
  return {
    id: String(raw.id),
    status: raw.status ?? "pending",
    changes: raw.changes ?? raw.changes_json ?? {},
    summary: raw.summary ?? undefined,
  };
}

function normalizeMessage(raw: any): CopilotMessage {
  return {
    id: String(raw.id),
    role: raw.role,
    content: String(raw.content ?? ""),
    citations: list(raw.citations ?? raw.citations_json).map((item: any) => ({
      label: String(item.label ?? item.title ?? item.source ?? "Fuente"),
      source: item.source ? String(item.source) : undefined,
      excerpt: item.excerpt ? String(item.excerpt) : undefined,
    })),
    createdAt: raw.createdAt ?? raw.created_at,
    changeSet: normalizeChangeSet(raw.changeSet ?? raw.change_set),
  };
}

export async function openCopilotSession(contractId: number, matchId?: string) {
  const existingPayload: any = await apiFetch(
    `/api/contracts/${contractId}/chat/sessions`,
  );
  const existing = list(existingPayload?.data ?? existingPayload);
  if (existing[0]?.id) return getCopilotSession(String(existing[0].id));
  const payload: any = await apiFetch(
    `/api/contracts/${contractId}/chat/sessions`,
    {
      method: "POST",
      json: { matchId },
    },
  );
  const created = payload?.data ?? payload;
  const root = created?.id
    ? await getCopilotSession(String(created.id))
    : created;
  return {
    id: String(root.id),
    title: root.title,
    messages: list(root.messages).map(normalizeMessage),
  } satisfies CopilotSession;
}

export async function getCopilotSession(sessionId: string) {
  const payload: any = await apiFetch(`/api/chat/sessions/${sessionId}`);
  const root = payload?.data ?? payload;
  return {
    id: String(root.id),
    title: root.title,
    messages: list(root.messages).map(normalizeMessage),
  } satisfies CopilotSession;
}

export async function sendCopilotMessage(sessionId: string, content: string) {
  const payload: any = await apiFetch(
    `/api/chat/sessions/${sessionId}/messages`,
    {
      method: "POST",
      json: { content },
    },
  );
  const root = payload?.data ?? payload;
  return {
    userMessage: normalizeMessage(root.userMessage ?? root.user_message),
    assistantMessage: normalizeMessage(
      root.assistantMessage ?? root.assistant_message,
    ),
  };
}

export async function decideChangeSet(
  id: string,
  decision: "confirm" | "reject",
) {
  return apiFetch(`/api/chat/change-sets/${id}/${decision}`, {
    method: "POST",
  });
}
