type ConversationMessage = {
  role: "user" | "assistant";
  content: string;
};

function normalized(value: string) {
  return value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

const DOCUMENT_NOUN =
  /\b(documento|documentos|docx|word|xlsx|excel|pdf|formato|formatos|plantilla|plantillas|archivo|archivos|anexo|anexos)\b/;
const DELIVERY_ACTION =
  /\b(genera|generar|generalo|generame|crea|crear|prepara|preparar|rellena|rellenar|completa|completar|edita|editar|modifica|modificar|materializa|materializar)\b/;
const SHORT_CONFIRMATION =
  /^(si[, ]*)?(hazlo|generalo|crealo|preparalo|rellenalo|completalo|editalo|procede|adelante|dale|ok|okay|listo)( por favor| porfa)?[.!]*$/;
const CONTEXTUAL_DELIVERY_ACTION =
  /\b(hazlo|procede|adelante|dale|generalo|crealo|preparalo|rellenalo|completalo|editalo|editar|rellenar|completar)\b/;
const EXPLICIT_DRAFT_CHANGE =
  /\b(aplica|aplicar|actualiza|actualizar|cambia|cambiar|guarda|guardar|modifica|modificar)\b.{0,50}\b(borrador|postulacion|campo|campos|rtm|precio|vigencia|contacto)\b/;

export function isDocumentDeliveryRequest(input: {
  currentMessage: string;
  recentMessages?: ConversationMessage[];
}) {
  const current = normalized(input.currentMessage);
  if (EXPLICIT_DRAFT_CHANGE.test(current)) return false;
  if (DOCUMENT_NOUN.test(current) && DELIVERY_ACTION.test(current)) return true;
  if (
    !SHORT_CONFIRMATION.test(current) &&
    !CONTEXTUAL_DELIVERY_ACTION.test(current)
  ) {
    return false;
  }

  return (input.recentMessages ?? [])
    .slice(-6)
    .some((message) => {
      const content = normalized(message.content);
      return DOCUMENT_NOUN.test(content);
    });
}

export function documentDeliveryInstruction(enabled: boolean) {
  if (!enabled) return "";
  return `
MODO DE ENTREGA DOCUMENTAL ACTIVO:
- El resultado solicitado es uno o más archivos descargables, no cambios en el borrador de PostgreSQL.
- No menciones el borrador, "Aplicar al borrador", change sets ni cambios pendientes de la postulación.
- Devuelve proposedChanges vacío: application sin campos, items [] y requirements [].
- Si faltan datos obligatorios, formula una sola pregunta consolidada y no afirmes que generaste el archivo.
- Si el usuario autorizó usar PENDIENTES o ya hay datos suficientes, crea ahora los archivos reales en outputs/generated-documents/ y valida su estructura antes de responder.
- Una respuesta que solo describe cómo rellenar el documento no completa la solicitud.`;
}

export function directDocumentAnswer(filenames: string[]) {
  if (filenames.length === 1) {
    return `Listo. Preparé ${filenames[0]} para descargar.`;
  }
  return `Listo. Preparé ${filenames.length} documentos para descargar.`;
}
