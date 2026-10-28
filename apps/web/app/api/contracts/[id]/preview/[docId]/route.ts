import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { seaceUnavailablePage } from "@/server/http/seaceUnavailable";
import { tenantCanAccessContract } from "@/server/services/crud";
import { getDocumentSource } from "@/server/services/contracts";
import { getSeacePdf, SeaceUnavailable } from "@/server/services/seaceEgress";

export const runtime = "nodejs";

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
    return seaceUnavailablePage(`/api/contracts/${idContrato}/original/${documentId}`);
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
