import test from "node:test";
import assert from "node:assert/strict";
import { isDocumentDeliveryMessage } from "./documentDelivery.ts";
import type { CopilotMessage } from "./types.ts";

function message(content: string, artifacts: CopilotMessage["artifacts"] = []) {
  return {
    id: "message-1",
    role: "assistant" as const,
    content,
    citations: [],
    artifacts,
  };
}

test("identifica respuestas documentales aunque el intento anterior no tenga artefacto", () => {
  assert.equal(
    isDocumentDeliveryMessage(
      message("Intenté editar la copia DOCX, pero no pude generar el archivo."),
    ),
    true,
  );
});

test("identifica cualquier respuesta que entregue un artefacto", () => {
  assert.equal(
    isDocumentDeliveryMessage(
      message("Listo.", [
        {
          id: "artifact-1",
          name: "cotizacion.docx",
          mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          sizeBytes: 2048,
          downloadUrl: "/api/chat/artifacts/artifact-1",
        },
      ]),
    ),
    true,
  );
});

test("conserva la revisión para cambios normales de la postulación", () => {
  assert.equal(
    isDocumentDeliveryMessage(
      message("Preparé una propuesta de precio y vigencia para revisar."),
    ),
    false,
  );
});
