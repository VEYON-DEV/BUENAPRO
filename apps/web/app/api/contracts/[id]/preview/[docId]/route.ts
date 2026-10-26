import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { tenantCanAccessContract } from "@/server/services/crud";
import { getDocumentSource } from "@/server/services/contracts";
import { getSeacePdf, SeaceUnavailable } from "@/server/services/seaceEgress";

export const runtime = "nodejs";

// Se muestra dentro del visor: un 503 con página propia en vez del 502 que Cloudflare reemplaza.
function unavailablePage(originalHref: string) {
  const html = `<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Vista previa no disponible</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f4f2;color:#191c20;font:15px/1.5 system-ui,sans-serif}main{max-width:360px;padding:24px;text-align:center}h1{font-size:17px;margin:0 0 8px}p{margin:0 0 18px;color:#555}a{display:inline-block;background:#191c20;color:#fff;border-radius:8px;padding:9px 14px;font-weight:700;text-decoration:none}</style></head>
<body><main><h1>Vista previa no disponible</h1><p>SEACE no respondió en este momento. Puedes abrir el documento directamente en SEACE.</p><a href="${originalHref}" target="_blank" rel="noopener noreferrer">Abrir en SEACE</a></main></body></html>`;
  return new NextResponse(html, {
    status: 503,
    headers: { "Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8", "Retry-After": "600" },
  });
}

export async function GET(_request: Request, context: { params: Promise<{ id: string; docId: string }> }) {
  const tenantId = await requireTenantId();
  const { id, docId } = await context.params;
  const idContrato = Number(id);
  const documentId = Number(docId);

  if (!(await tenantCanAccessContract(tenantId, idContrato))) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  const document = await getDocumentSource(idContrato, documentId);
  if (!document) return NextResponse.json({ error: "Not found" }, { status: 404 });
  const isPdf = document.mime === "application/pdf" || document.filename.toLowerCase().endsWith(".pdf");
  if (!isPdf) return NextResponse.json({ error: "Preview is only available for PDF documents" }, { status: 415 });

  let pdf: Buffer;
  try {
    pdf = await getSeacePdf(document.seace_download_url);
  } catch (error) {
    if (!(error instanceof SeaceUnavailable)) console.error(error);
    return unavailablePage(`/api/contracts/${idContrato}/original/${documentId}`);
  }

  return new NextResponse(new Uint8Array(pdf), {
    headers: {
      "Cache-Control": "private, max-age=3600",
      "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(document.filename)}`,
      "Content-Length": String(pdf.length),
      "Content-Type": "application/pdf",
    },
  });
}
