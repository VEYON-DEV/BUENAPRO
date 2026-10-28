import { seaceFetch } from "./seaceEgress";

const METADATA_URL = "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc";
const ALFRESCO_CONTEXT = "https://alfprod.seace.gob.pe/alfresco/";
const CALLBACK = "buenaproDocument";
const MAX_METADATA_BYTES = 64 * 1024;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export class Prod2OfficialDocumentError extends Error {
  constructor() { super("SEACE no pudo proporcionar el enlace de descarga. Inténtalo nuevamente o abre la ficha oficial."); }
}

/** The persisted reference identifies a resource; download tickets must be resolved afresh. */
export function prod2OfficialDocumentReference(documentCode: string) {
  if (!uuidPattern.test(documentCode)) return null;
  return `${METADATA_URL}?id=${documentCode.toLowerCase()}`;
}

function downloadLocation(payload: string) {
  const text = payload.trim();
  // Only decode JSON/our exact JSONP callback. Never execute upstream JavaScript.
  const wrapped = text.match(new RegExp(`^${CALLBACK}\\s*\\(([\\s\\S]*)\\)\\s*;?$`));
  const value: unknown = JSON.parse(wrapped ? wrapped[1] : text);
  if (!value || typeof value !== "object" || Array.isArray(value)
    || !("downloadUrl" in value) || typeof value.downloadUrl !== "string" || !value.downloadUrl.trim()) throw new Prod2OfficialDocumentError();
  const raw = value.downloadUrl.trim();
  // Alfresco's /service/... is relative to its /alfresco context, not the host root.
  const url = new URL(raw.startsWith("/service/") ? raw.slice(1) : raw, ALFRESCO_CONTEXT);
  if (url.protocol !== "https:" || !url.hostname.endsWith(".seace.gob.pe") || url.username || url.password
    || (url.port && url.port !== "443") || url.hash) throw new Prod2OfficialDocumentError();
  return url.toString();
}

async function boundedMetadata(response: Awaited<ReturnType<typeof seaceFetch>>) {
  const length = response.headers.get("content-length");
  if (length && (!/^\d+$/.test(length) || Number(length) > MAX_METADATA_BYTES)) {
    await response.body?.cancel();
    throw new Prod2OfficialDocumentError();
  }
  if (!response.body) throw new Prod2OfficialDocumentError();
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let bytes = 0, text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_METADATA_BYTES) throw new Prod2OfficialDocumentError();
      text += decoder.decode(value, { stream: true });
    }
    return text + decoder.decode();
  } finally {
    try { await reader.cancel(); } catch { /* Preserve the safe resolver error. */ }
    reader.releaseLock();
  }
}

/**
 * Read metadata only; never download/store the binary or retain/log the short-lived ticket.
 * The request leaves through the Peruvian egress (`seaceEgress`), never straight from the VM: when it is
 * paused, busy or down the caller gets the same safe error as for any other upstream failure.
 */
export async function resolveProd2OfficialDocument(documentCode: string, fetcher: typeof seaceFetch = seaceFetch) {
  const reference = prod2OfficialDocumentReference(documentCode);
  if (!reference) throw new Prod2OfficialDocumentError();
  const endpoint = new URL(reference);
  endpoint.searchParams.set("doc", CALLBACK);
  endpoint.searchParams.set("guest", "false");
  try {
    const response = await fetcher(endpoint.toString(), { redirect: "error", timeoutMs: 10_000, interactive: true,
      headers: { Accept: "application/json, application/javascript, text/javascript" } });
    if (!response.ok) {
      await response.body?.cancel();
      throw new Prod2OfficialDocumentError();
    }
    return downloadLocation(await boundedMetadata(response));
  } catch {
    // Tickets, cookies, upstream HTML and provider error bodies must never reach logs or API errors.
    throw new Prod2OfficialDocumentError();
  }
}
