import type { CopilotMessage } from "./types";

const DOCUMENT_DELIVERY_LANGUAGE =
  /(?:\b(?:gener|cre|prepar|rellen|complet|edit|materializ)\w*\b.{0,100}\b(?:docx|word|documento|plantilla|formato|archivo)\b)|(?:\b(?:docx|word|documento|plantilla|formato|archivo)\b.{0,100}\b(?:gener|cre|prepar|rellen|complet|edit|materializ)\w*\b)/i;

export function isDocumentDeliveryMessage(message: CopilotMessage) {
  return (
    message.artifacts.length > 0 ||
    DOCUMENT_DELIVERY_LANGUAGE.test(message.content)
  );
}
