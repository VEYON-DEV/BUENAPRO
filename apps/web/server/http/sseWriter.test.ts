import test from "node:test";
import assert from "node:assert/strict";
import { createSseWriter } from "./sseWriter.ts";

test("una desconexión del cliente no lanza ni detiene el trabajo", () => {
  const chunks: string[] = [];
  let closed = false;
  const writer = createSseWriter({
    enqueue(chunk) {
      if (closed) throw new TypeError("Invalid state: Controller is already closed");
      chunks.push(new TextDecoder().decode(chunk));
    },
    close() {
      closed = true;
    },
  });

  assert.equal(writer.send("status", { message: "Procesando" }), true);
  closed = true;
  assert.doesNotThrow(() => writer.send("status", { message: "Terminando" }));
  assert.equal(writer.isOpen(), false);
  assert.doesNotThrow(() => writer.close());
  assert.match(chunks[0], /^event: status/);
});

test("emite heartbeats SSE mientras la conexión permanece abierta", () => {
  const chunks: string[] = [];
  const writer = createSseWriter({
    enqueue(chunk) {
      chunks.push(new TextDecoder().decode(chunk));
    },
    close() {},
  });

  assert.equal(writer.heartbeat(), true);
  assert.deepEqual(chunks, [": keep-alive\n\n"]);
});
