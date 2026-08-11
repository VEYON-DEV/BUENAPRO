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
    artifacts: list(raw.artifacts).map((artifact: any) => ({
      id: String(artifact.id),
      name: String(artifact.name ?? artifact.filename ?? "Documento"),
      mime: String(
        artifact.mime ?? artifact.mime_type ?? "application/octet-stream",
      ),
      sizeBytes: Number(artifact.sizeBytes ?? artifact.size_bytes ?? 0),
      downloadUrl: String(
        artifact.downloadUrl ??
          artifact.download_url ??
          `/api/chat/artifacts/${artifact.id}`,
      ),
    })),
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

type StreamHandlers = {
  onUserMessage?: (message: CopilotMessage) => void;
  onAssistantMessage?: (message: CopilotMessage) => void;
  onStatus?: (message: string) => void;
  onActivity?: (message: string) => void;
};

function activityText(data: any) {
  const text = String(data?.text ?? "").trim();
  if (!text) return "";
  if (data?.itemType !== "agent_message") return text;
  try {
    const parsed = JSON.parse(text);
    const answer = String(parsed.answer ?? "").trim();
    return answer.length <= 180 ? answer : "";
  } catch {
    return text.length <= 180 ? text : "";
  }
}

export async function streamCopilotMessage(
  sessionId: string,
  content: string,
  handlers: StreamHandlers,
) {
  const response = await fetch(
    `/api/chat/sessions/${sessionId}/messages/stream`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error ?? "No se pudo responder.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let streamError: Error | null = null;

  function dispatch(raw: string) {
    const event = raw
      .split("\n")
      .find((line) => line.startsWith("event: "))
      ?.slice(7)
      .trim();
    const dataLine = raw
      .split("\n")
      .find((line) => line.startsWith("data: "));
    if (!event || !dataLine) return;
    const data = JSON.parse(dataLine.slice(6));
    if (event === "user_message") {
      handlers.onUserMessage?.(normalizeMessage(data));
    } else if (event === "assistant_message" && data) {
      handlers.onAssistantMessage?.(normalizeMessage(data));
    } else if (event === "status") {
      handlers.onStatus?.(String(data.message ?? ""));
    } else if (event === "codex_item") {
      const activity = activityText(data);
      if (activity) handlers.onActivity?.(activity);
    } else if (event === "error") {
      streamError = new Error(data.error ?? "No se pudo responder.");
    }
  }

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) dispatch(chunk);
    if (streamError) throw streamError;
  }
  if (buffer.trim()) dispatch(buffer);
  if (streamError) throw streamError;
}

export async function decideChangeSet(
  id: string,
  decision: "confirm" | "reject",
) {
  return apiFetch(`/api/chat/change-sets/${id}/${decision}`, {
    method: "POST",
  });
}
