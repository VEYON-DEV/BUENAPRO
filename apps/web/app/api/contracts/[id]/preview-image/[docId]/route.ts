import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import { tenantCanAccessContract } from "@/server/services/crud";
import { getDocumentSource } from "@/server/services/contracts";
import { getSeacePdf, SeaceUnavailable } from "@/server/services/seaceEgress";

export const runtime = "nodejs";

const execFileAsync = promisify(execFile);

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
    return NextResponse.json(
      { error: "Preview unavailable" },
      { status: 503, headers: { "Cache-Control": "no-store", "Retry-After": "600" } },
    );
  }

  const dir = await mkdtemp(join(tmpdir(), "buenapro-pdf-preview-"));
  try {
    const pdfPath = join(dir, "source.pdf");
    const outputBase = join(dir, "page");
    await writeFile(pdfPath, pdf);
    await execFileAsync("pdftoppm", ["-f", "1", "-singlefile", "-png", "-r", "128", pdfPath, outputBase], {
      timeout: 20_000,
    });
    const image = await readFile(`${outputBase}.png`);
    return new NextResponse(image, {
      headers: {
        "Cache-Control": "private, max-age=600",
        "Content-Type": "image/png",
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Could not render preview" },
      { status: 500 },
    );
  } finally {
    await rm(dir, { force: true, recursive: true });
  }
}
