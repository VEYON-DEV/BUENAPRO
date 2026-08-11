import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import {
  deleteCompanyDocument,
  getCompanyDocument,
  updateCompanyDocument,
} from "@/server/services/companyLibrary";

type Params = Promise<{ documentId: string }>;

function documentHeaders(filename: string, mimeType: string, sizeBytes: number) {
  return {
    "Content-Type": mimeType,
    "Content-Length": String(sizeBytes),
    "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
  };
}

export async function GET(_request: Request, context: { params: Params }) {
  const tenantId = await requireTenantId();
  const { documentId } = await context.params;
  const document = await getCompanyDocument(tenantId, documentId);
  if (!document) {
    return NextResponse.json(
      { error: "Documento no encontrado" },
      { status: 404 },
    );
  }
  return new Response(new Uint8Array(document.content), {
    headers: documentHeaders(
      document.filename,
      document.mime_type,
      document.size_bytes,
    ),
  });
}

export async function PATCH(request: Request, context: { params: Params }) {
  const tenantId = await requireTenantId();
  const { documentId } = await context.params;
  const data = await updateCompanyDocument(
    tenantId,
    documentId,
    await request.json().catch(() => ({})),
  );
  return data
    ? NextResponse.json({ data })
    : NextResponse.json({ error: "Documento no encontrado" }, { status: 404 });
}

export async function DELETE(_request: Request, context: { params: Params }) {
  const tenantId = await requireTenantId();
  const { documentId } = await context.params;
  const deleted = await deleteCompanyDocument(tenantId, documentId);
  return deleted
    ? NextResponse.json({ data: { id: deleted.id } })
    : NextResponse.json({ error: "Documento no encontrado" }, { status: 404 });
}
