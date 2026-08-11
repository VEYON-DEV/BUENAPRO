import { currentUserId, requireTenantId } from "@/server/auth/tenant";
import {
  maybeCompactConversationMemory,
  runLicitationAgent,
  type AgentMessage,
  type ProposedApplicationChanges,
} from "@/server/agent";
import {
  beginAgentRun,
  completeAgentRun,
  failAgentRun,
  getChatSession,
  updateChatSummary,
} from "@/server/services/applicationChat";
import {
  cleanupGeneratedArtifacts,
  loadGeneratedArtifacts,
} from "@/server/services/chatArtifacts";

export const runtime = "nodejs";
export const maxDuration = 300;

function changesForPersistence(changes: ProposedApplicationChanges) {
  return changes;
}

function agentMessages(messages: any[]): AgentMessage[] {
  return messages
    .filter(
      (message) => message.role === "user" || message.role === "assistant",
    )
    .map((message) => ({
      role: message.role,
      content: String(message.content ?? "").slice(0, 8_000),
    }));
}

function sse(event: string, data: unknown) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

export async function POST(
  request: Request,
  context: { params: Promise<{ sessionId: string }> },
) {
  const tenantId = await requireTenantId();
  const actorId = await currentUserId();
  if (!actorId) {
    return Response.json(
      { error: "Inicia sesión para usar el copiloto." },
      { status: 401 },
    );
  }
  const { sessionId } = await context.params;
  const body = await request.json().catch(() => ({}));
  const content = String(body.content ?? "")
    .trim()
    .slice(0, 8_000);
  if (!content) {
    return Response.json(
      { error: "Escribe una pregunta para el copiloto." },
      { status: 400 },
    );
  }

  const session = await getChatSession(tenantId, sessionId);
  if (!session) {
    return Response.json(
      { error: "Conversación no encontrada." },
      { status: 404 },
    );
  }
  const memory = await maybeCompactConversationMemory(
    session.summary as string | null,
    agentMessages(session.messages as any[]),
  );
  if (memory.compacted) {
    await updateChatSummary(tenantId, sessionId, memory.summary);
  }

  const started = await beginAgentRun(tenantId, sessionId, actorId, content);
  if (!started) {
    return Response.json(
      { error: "Conversación no encontrada." },
      { status: 404 },
    );
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const send = (event: string, data: unknown) => {
        controller.enqueue(encoder.encode(sse(event, data)));
      };
      send("user_message", started.message);
      try {
        const agentInput = {
          tenantId,
          matchId: session.matchId ? Number(session.matchId) : null,
          idContrato: Number(session.contractId),
          userMessage: content,
          conversationSummary: memory.summary,
          recentMessages: memory.retainedMessages,
        };
        const useCodex = process.env.CODEX_AGENT_ENABLED !== "0";
        if (useCodex) {
          const { streamCodexLicitationAgent } = await import(
            "@/server/agent/codexAgent"
          );
          for await (const event of streamCodexLicitationAgent(
            sessionId,
            agentInput,
          )) {
            if (event.type === "status") send("status", event);
            if (event.type === "codex_thread") send("codex_thread", event);
            if (event.type === "codex_item") {
              send("codex_item", {
                itemType: event.itemType,
                text: event.text,
              });
            }
            if (event.type === "error") throw new Error(event.message);
            if (event.type === "done") {
              const artifacts = await loadGeneratedArtifacts(
                event.outputDirectory,
                event.generatedFiles,
              );
              const persisted = await completeAgentRun(
                tenantId,
                String(started.run.id),
                {
                  content: event.result.answer,
                  citations: event.result.citations.map((citation) => ({
                    label: citation.label,
                    source: citation.sourceId,
                    excerpt: citation.evidence,
                  })),
                  metadata: {
                    model: event.result.model,
                    engine: "codex-cli",
                    usage: event.usage,
                  },
                  usage: event.usage,
                  changes: changesForPersistence(
                    event.result.proposedChanges,
                  ),
                  artifacts,
                },
              );
              await cleanupGeneratedArtifacts(
                event.outputDirectory,
                event.generatedFiles,
              );
              send("assistant_message", persisted?.message ?? null);
              send("done", { ok: true });
            }
          }
        } else {
          const result = await runLicitationAgent(agentInput);
          if (!result)
            throw new Error("No se encontró el expediente de la licitación.");
          const persisted = await completeAgentRun(
            tenantId,
            String(started.run.id),
            {
              content: result.answer,
              citations: result.citations.map((citation) => ({
                label: citation.label,
                source: citation.sourceId,
                excerpt: citation.evidence,
              })),
              metadata: { model: result.model, engine: "gemini" },
              changes: changesForPersistence(result.proposedChanges),
            },
          );
          send("assistant_message", persisted?.message ?? null);
          send("done", { ok: true });
        }
      } catch (error) {
        await failAgentRun(tenantId, String(started.run.id), error);
        send("error", {
          error:
            error instanceof Error
              ? error.message
              : "El copiloto no pudo responder en este momento.",
        });
      } finally {
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
    },
  });
}
