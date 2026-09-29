import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { query } from "@/server/db/client";

const MAX_DOCUMENT_BYTES = 80 * 1024 * 1024;
const OFFICIAL_DOCUMENT_ORIGIN = "https://prod1.seace.gob.pe";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string; code: string }> },
) {
  await requireTenantId();
  const { id, code } = await context.params;
  if (!/^[1-9]\d{0,18}$/.test(id) || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(code)) {
    return NextResponse.json({ error: "Invalid document" }, { status: 400 });
  }
  const result = await query<{ name: string | null; extension: string | null }>(
    `SELECT d.name, d.extension
     FROM prod4_documents d
     JOIN prod4_processes p ON p.id_procedimiento = d.id_procedimiento
     WHERE d.id_procedimiento = $1 AND d.codigo_alfresco = $2
       AND p.technology_relevant = true`,
    [id, code],
  );
  const document = result.rows[0];
  if (!document) return NextResponse.json({ error: "Not found" }, { status: 404 });
  if (document.extension?.toLowerCase().replace(/^\./, "") !== "pdf") {
    return NextResponse.json({ error: "Only PDF preview is supported" }, { status: 415 });
  }

  const url = new URL("/SeaceWeb-PRO/SdescargarArchivoAlfresco", OFFICIAL_DOCUMENT_ORIGIN);
  url.searchParams.set("fileCode", code);
  let upstream: Response;
  try {
    upstream = await fetch(url, {
      redirect: "manual",
      signal: AbortSignal.timeout(120_000),
      headers: {
        Accept: "application/pdf,*/*",
        Referer: "https://prod4.seace.gob.pe/openegocio/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
      },
    });
  } catch {
    return NextResponse.json({ error: "Official document unavailable" }, { status: 502 });
  }
  if (!upstream.ok || !upstream.body) {
    return NextResponse.json({ error: "Official document unavailable" }, { status: 502 });
  }
  const declaredLength = Number(upstream.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_DOCUMENT_BYTES) {
    await upstream.body.cancel();
    return NextResponse.json({ error: "Document exceeds preview limit" }, { status: 413 });
  }
  const reader = upstream.body.getReader();
  const openingChunks: Uint8Array[] = [];
  let openingLength = 0;
  while (openingLength < 5) {
    const next = await reader.read();
    if (next.done) break;
    openingChunks.push(next.value);
    openingLength += next.value.byteLength;
  }
  const signature = new Uint8Array(Math.min(openingLength, 5));
  let signatureOffset = 0;
  for (const chunk of openingChunks) {
    const part = chunk.subarray(0, signature.byteLength - signatureOffset);
    signature.set(part, signatureOffset);
    signatureOffset += part.byteLength;
    if (signatureOffset === signature.byteLength) break;
  }
  if (new TextDecoder().decode(signature) !== "%PDF-") {
    await reader.cancel();
    return NextResponse.json({ error: "Official file is not a PDF" }, { status: 415 });
  }
  let received = openingLength;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of openingChunks) controller.enqueue(chunk);
    },
    async pull(controller) {
      try {
        const next = await reader.read();
        if (next.done) {
          controller.close();
          return;
        }
        received += next.value.byteLength;
        if (received > MAX_DOCUMENT_BYTES) {
          await reader.cancel();
          controller.error(new Error("Document exceeds preview limit"));
          return;
        }
        controller.enqueue(next.value);
      } catch (error) {
        controller.error(error);
      }
    },
    cancel() {
      void reader.cancel();
    },
  });
  const filename = (document.name || `bases-${id}.pdf`).replace(/[\r\n"\\/]/g, "_");
  return new NextResponse(body, {
    headers: {
      "Content-Type": "application/pdf",
      "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(filename)}`,
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
