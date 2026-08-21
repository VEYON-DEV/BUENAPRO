export type SseController = Pick<
  ReadableStreamDefaultController<Uint8Array>,
  "enqueue" | "close"
>;

function eventPayload(event: string, data: unknown) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

export function createSseWriter(
  controller: SseController,
  encoder = new TextEncoder(),
) {
  let open = true;

  function enqueue(payload: string) {
    if (!open) return false;
    try {
      controller.enqueue(encoder.encode(payload));
      return true;
    } catch {
      open = false;
      return false;
    }
  }

  return {
    send(event: string, data: unknown) {
      return enqueue(eventPayload(event, data));
    },
    heartbeat() {
      return enqueue(": keep-alive\n\n");
    },
    disconnect() {
      open = false;
    },
    close() {
      if (!open) return;
      try {
        controller.close();
      } catch {
        // El cliente puede cerrar el stream mientras el agente sigue trabajando.
      } finally {
        open = false;
      }
    },
    isOpen() {
      return open;
    },
  };
}
