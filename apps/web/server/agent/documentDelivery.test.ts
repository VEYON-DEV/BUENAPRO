import test from "node:test";
import assert from "node:assert/strict";
import {
  directDocumentAnswer,
  finalDocumentFilename,
  isDocumentDeliveryRequest,
  referencedDocumentFilenames,
} from "./documentDelivery.ts";

test("detecta solicitudes directas de generación documental", () => {
  for (const message of [
    "Genera ahora el DOCX",
    "Rellena la plantilla con los datos disponibles",
    "Prepara los documentos y deja PENDIENTE lo que falta",
    "Edita el archivo Word",
    "Dame el doc",
  ]) {
    assert.equal(
      isDocumentDeliveryRequest({ currentMessage: message }),
      true,
      message,
    );
  }
});

test("recupera un documento existente cuando Codex devuelve su ruta interna", () => {
  assert.deepEqual(
    referencedDocumentFilenames(
      "Está en outputs/generated-documents/oferta_final.docx",
      ["otro.pdf", "oferta_final.docx"],
    ),
    ["oferta_final.docx"],
  );
});

test("elimina borrador del nombre final entregado", () => {
  assert.equal(
    finalDocumentFilename(
      "FORMATOS_ACTUALIZADOS_completado_borrador_VEYON_SAC.docx",
    ),
    "FORMATOS_ACTUALIZADOS_completado_VEYON_SAC.docx",
  );
  assert.equal(finalDocumentFilename("borrador-final.pdf"), "final.pdf");
});

test("mantiene el modo documental en una confirmación breve", () => {
  assert.equal(
    isDocumentDeliveryRequest({
      currentMessage: "Sí, hazlo",
      recentMessages: [
        { role: "user", content: "Quiero rellenar el documento" },
        { role: "assistant", content: "Puedo preparar la plantilla DOCX." },
      ],
    }),
    true,
  );
  assert.equal(
    isDocumentDeliveryRequest({
      currentMessage: "hazlo por usa tu herrameint para editar",
      recentMessages: [
        { role: "assistant", content: "Puedo preparar la plantilla DOCX." },
      ],
    }),
    true,
  );
});

test("no confunde consultas ni cambios explícitos del borrador", () => {
  assert.equal(
    isDocumentDeliveryRequest({
      currentMessage: "¿Qué documentos necesito para postular?",
    }),
    false,
  );
  assert.equal(
    isDocumentDeliveryRequest({
      currentMessage: "Actualiza el precio del borrador",
    }),
    false,
  );
});

test("resume la entrega con el nombre del archivo y sin mencionar borradores", () => {
  const answer = directDocumentAnswer(["cotizacion.docx"]);
  assert.equal(answer, "Listo. Preparé cotizacion.docx para descargar.");
  assert.equal(answer.toLowerCase().includes("borrador"), false);
});
