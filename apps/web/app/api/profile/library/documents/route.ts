import { NextResponse } from "next/server";
import { requireTenantId } from "@/server/auth/tenant";
import {
  COMPANY_LIBRARY_MAX_FILE_SIZE,
  COMPANY_LIBRARY_MIME_BY_EXTENSION,
  createCompanyDocument,
  safeFilename,
} from "@/server/services/companyLibrary";

export async function POST(request: Request) {
  const tenantId = await requireTenantId();
  const formData = await request.formData();
  const file = formData.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json(
      { error: "Se requiere un archivo en el campo file." },
      { status: 400 },
    );
  }

  const filename = safeFilename(file.name);
  const extension = filename.split(".").pop()?.toLowerCase() ?? "";
  const mimeType = COMPANY_LIBRARY_MIME_BY_EXTENSION[extension];
  if (!filename || !mimeType) {
    return NextResponse.json(
      { error: "Formato no permitido. Usa PDF, Word, Excel o imagen." },
      { status: 415 },
    );
  }
  if (file.size <= 0 || file.size > COMPANY_LIBRARY_MAX_FILE_SIZE) {
    return NextResponse.json(
      { error: "El archivo debe pesar como maximo 10 MB." },
      { status: 413 },
    );
  }
  if (
    file.type &&
    file.type !== "application/octet-stream" &&
    file.type !== mimeType
  ) {
    return NextResponse.json(
      { error: "El contenido del archivo no coincide con su extension." },
      { status: 415 },
    );
  }

  const data = await createCompanyDocument(tenantId, {
    title: String(formData.get("title") || filename),
    filename,
    mimeType,
    sizeBytes: file.size,
    content: Buffer.from(await file.arrayBuffer()),
    documentType: String(formData.get("documentType") || "documento"),
    description: String(formData.get("description") || ""),
    tags: String(formData.get("tags") || ""),
    validUntil: String(formData.get("validUntil") || ""),
    amount: String(formData.get("amount") || ""),
    entityName: String(formData.get("entityName") || ""),
    usableForApplications: formData.get("usableForApplications") === "on",
  });
  return data
    ? NextResponse.json({ data }, { status: 201 })
    : NextResponse.json({ error: "No se pudo guardar el documento." }, { status: 400 });
}
