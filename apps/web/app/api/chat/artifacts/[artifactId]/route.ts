import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { getChatArtifact } from "@/server/services/chatArtifacts";

export const runtime = "nodejs";

export async function GET(
  _request: Request,
  context: { params: Promise<{ artifactId: string }> },
) {
  const tenantId = await requireTenantId();
  const { artifactId } = await context.params;
  const artifact = await getChatArtifact(tenantId, artifactId);
  if (!artifact) {
    return NextResponse.json(
      { error: "Documento no encontrado." },
      { status: 404 },
    );
  }
  return new Response(new Uint8Array(artifact.content), {
    headers: {
      "Content-Type": artifact.mime_type,
      "Content-Length": String(artifact.size_bytes),
      "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(artifact.filename)}`,
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
