import { query } from "@/server/db/client";

type JsonObject = Record<string, unknown>;

const MAX_TITLE = 160;
const MAX_KIND = 80;
const MAX_DESCRIPTION = 2_000;
const MAX_VALUE = 12_000;
const MAX_TAGS = 20;
const MAX_TAG = 40;

export const COMPANY_LIBRARY_MAX_FILE_SIZE = 10 * 1024 * 1024;

export const COMPANY_LIBRARY_MIME_BY_EXTENSION: Record<string, string> = {
  pdf: "application/pdf",
  doc: "application/msword",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xls: "application/vnd.ms-excel",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
};

function text(value: unknown, max: number) {
  if (typeof value !== "string") return null;
  const normalized = value.replace(/[\u0000-\u001f]/g, " ").trim();
  return normalized ? normalized.slice(0, max) : null;
}

function numberOrNull(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function booleanValue(value: unknown, fallback = true) {
  return typeof value === "boolean" ? value : fallback;
}

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

export function safeFilename(value: string) {
  return value
    .replace(/[\\/\0\r\n]/g, "_")
    .trim()
    .slice(0, 255);
}

export function normalizeTags(value: unknown) {
  const raw = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(",")
      : [];
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const entry of raw) {
    const tag = text(entry, MAX_TAG)?.toLowerCase();
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    tags.push(tag);
    if (tags.length >= MAX_TAGS) break;
  }
  return tags;
}

async function defaultProfileId(tenantId: string) {
  const result = await query<{ id: string }>(
    `SELECT id FROM company_profiles
     WHERE tenant_id=$1 AND is_active=true
     ORDER BY updated_at DESC,created_at DESC LIMIT 1`,
    [tenantId],
  );
  return result.rows[0]?.id ?? null;
}

function mapKnowledge(row: any) {
  return {
    id: row.id,
    kind: row.kind,
    title: row.title,
    description: row.description,
    valueText: row.value_text,
    tags: row.tags ?? [],
    metadata: row.metadata_json ?? {},
    usableForApplications: row.usable_for_applications,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function mapDocument(row: any) {
  return {
    id: row.id,
    title: row.title,
    filename: row.filename,
    mimeType: row.mime_type,
    sizeBytes: row.size_bytes,
    documentType: row.document_type,
    description: row.description,
    tags: row.tags ?? [],
    metadata: row.metadata_json ?? {},
    validUntil: row.valid_until,
    amount: row.amount == null ? null : Number(row.amount),
    entityName: row.entity_name,
    usableForApplications: row.usable_for_applications,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    downloadUrl: `/api/profile/library/documents/${row.id}`,
  };
}

export async function listCompanyLibrary(tenantId: string) {
  const [knowledge, documents] = await Promise.all([
    query(
      `SELECT * FROM company_knowledge_items
       WHERE tenant_id=$1
       ORDER BY updated_at DESC,created_at DESC`,
      [tenantId],
    ),
    query(
      `SELECT id,title,filename,mime_type,size_bytes,document_type,description,tags,
              metadata_json,valid_until,amount,entity_name,usable_for_applications,
              created_at,updated_at
       FROM company_documents
       WHERE tenant_id=$1
       ORDER BY updated_at DESC,created_at DESC`,
      [tenantId],
    ),
  ]);
  return {
    knowledge: knowledge.rows.map(mapKnowledge),
    documents: documents.rows.map(mapDocument),
  };
}

export async function createKnowledgeItem(tenantId: string, input: unknown) {
  const body = object(input);
  const title = text(body.title, MAX_TITLE);
  if (!title) return null;
  const result = await query(
    `INSERT INTO company_knowledge_items
       (tenant_id,profile_id,kind,title,description,value_text,tags,metadata_json,usable_for_applications)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)
     RETURNING *`,
    [
      tenantId,
      await defaultProfileId(tenantId),
      text(body.kind, MAX_KIND) ?? "nota",
      title,
      text(body.description, MAX_DESCRIPTION),
      text(body.valueText ?? body.value_text, MAX_VALUE),
      normalizeTags(body.tags),
      JSON.stringify(object(body.metadata)),
      booleanValue(body.usableForApplications ?? body.usable_for_applications),
    ],
  );
  return mapKnowledge(result.rows[0]);
}

export async function updateKnowledgeItem(
  tenantId: string,
  itemId: string,
  input: unknown,
) {
  const body = object(input);
  const result = await query(
    `UPDATE company_knowledge_items SET
       kind=COALESCE($3,kind),
       title=COALESCE($4,title),
       description=CASE WHEN $5::boolean THEN $6 ELSE description END,
       value_text=CASE WHEN $7::boolean THEN $8 ELSE value_text END,
       tags=COALESCE($9,tags),
       metadata_json=CASE WHEN $10::boolean THEN $11::jsonb ELSE metadata_json END,
       usable_for_applications=COALESCE($12,usable_for_applications),
       updated_at=now()
     WHERE tenant_id=$1 AND id=$2
     RETURNING *`,
    [
      tenantId,
      itemId,
      text(body.kind, MAX_KIND),
      text(body.title, MAX_TITLE),
      "description" in body,
      text(body.description, MAX_DESCRIPTION),
      "valueText" in body || "value_text" in body,
      text(body.valueText ?? body.value_text, MAX_VALUE),
      "tags" in body ? normalizeTags(body.tags) : null,
      "metadata" in body,
      JSON.stringify(object(body.metadata)),
      typeof body.usableForApplications === "boolean"
        ? body.usableForApplications
        : typeof body.usable_for_applications === "boolean"
          ? body.usable_for_applications
          : null,
    ],
  );
  return result.rows[0] ? mapKnowledge(result.rows[0]) : null;
}

export async function deleteKnowledgeItem(tenantId: string, itemId: string) {
  const result = await query<{ id: string }>(
    "DELETE FROM company_knowledge_items WHERE tenant_id=$1 AND id=$2 RETURNING id",
    [tenantId, itemId],
  );
  return result.rows[0] ?? null;
}

export type CompanyDocumentInput = {
  title: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
  content: Buffer;
  documentType?: string | null;
  description?: string | null;
  tags?: unknown;
  validUntil?: string | null;
  amount?: unknown;
  entityName?: string | null;
  usableForApplications?: boolean;
  metadata?: unknown;
};

export async function createCompanyDocument(
  tenantId: string,
  input: CompanyDocumentInput,
) {
  const title = text(input.title, MAX_TITLE);
  if (!title) return null;
  const result = await query(
    `INSERT INTO company_documents
       (tenant_id,profile_id,title,filename,mime_type,size_bytes,content,document_type,
        description,tags,metadata_json,valid_until,amount,entity_name,usable_for_applications)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::date,$13,$14,$15)
     RETURNING id,title,filename,mime_type,size_bytes,document_type,description,tags,
               metadata_json,valid_until,amount,entity_name,usable_for_applications,
               created_at,updated_at`,
    [
      tenantId,
      await defaultProfileId(tenantId),
      title,
      safeFilename(input.filename),
      input.mimeType,
      input.sizeBytes,
      input.content,
      text(input.documentType, MAX_KIND) ?? "documento",
      text(input.description, MAX_DESCRIPTION),
      normalizeTags(input.tags),
      JSON.stringify(object(input.metadata)),
      text(input.validUntil, 10),
      numberOrNull(input.amount),
      text(input.entityName, 180),
      input.usableForApplications ?? true,
    ],
  );
  return mapDocument(result.rows[0]);
}

export async function updateCompanyDocument(
  tenantId: string,
  documentId: string,
  input: unknown,
) {
  const body = object(input);
  const result = await query(
    `UPDATE company_documents SET
       title=COALESCE($3,title),
       document_type=COALESCE($4,document_type),
       description=CASE WHEN $5::boolean THEN $6 ELSE description END,
       tags=COALESCE($7,tags),
       valid_until=CASE WHEN $8::boolean THEN $9::date ELSE valid_until END,
       amount=CASE WHEN $10::boolean THEN $11 ELSE amount END,
       entity_name=CASE WHEN $12::boolean THEN $13 ELSE entity_name END,
       metadata_json=CASE WHEN $14::boolean THEN $15::jsonb ELSE metadata_json END,
       usable_for_applications=COALESCE($16,usable_for_applications),
       updated_at=now()
     WHERE tenant_id=$1 AND id=$2
     RETURNING id,title,filename,mime_type,size_bytes,document_type,description,tags,
               metadata_json,valid_until,amount,entity_name,usable_for_applications,
               created_at,updated_at`,
    [
      tenantId,
      documentId,
      text(body.title, MAX_TITLE),
      text(body.documentType ?? body.document_type, MAX_KIND),
      "description" in body,
      text(body.description, MAX_DESCRIPTION),
      "tags" in body ? normalizeTags(body.tags) : null,
      "validUntil" in body || "valid_until" in body,
      text(body.validUntil ?? body.valid_until, 10),
      "amount" in body,
      numberOrNull(body.amount),
      "entityName" in body || "entity_name" in body,
      text(body.entityName ?? body.entity_name, 180),
      "metadata" in body,
      JSON.stringify(object(body.metadata)),
      typeof body.usableForApplications === "boolean"
        ? body.usableForApplications
        : typeof body.usable_for_applications === "boolean"
          ? body.usable_for_applications
          : null,
    ],
  );
  return result.rows[0] ? mapDocument(result.rows[0]) : null;
}

export async function getCompanyDocument(tenantId: string, documentId: string) {
  const result = await query<{
    filename: string;
    mime_type: string;
    size_bytes: number;
    content: Buffer;
  }>(
    `SELECT filename,mime_type,size_bytes,content
     FROM company_documents
     WHERE tenant_id=$1 AND id=$2`,
    [tenantId, documentId],
  );
  return result.rows[0] ?? null;
}

export async function deleteCompanyDocument(
  tenantId: string,
  documentId: string,
) {
  const result = await query<{ id: string }>(
    "DELETE FROM company_documents WHERE tenant_id=$1 AND id=$2 RETURNING id",
    [tenantId, documentId],
  );
  return result.rows[0] ?? null;
}

export async function agentCompanyLibraryContext(tenantId: string) {
  const [knowledge, documents] = await Promise.all([
    query(
      `SELECT kind,title,description,value_text,tags,metadata_json,updated_at
       FROM company_knowledge_items
       WHERE tenant_id=$1 AND usable_for_applications=true
       ORDER BY updated_at DESC LIMIT 30`,
      [tenantId],
    ),
    query(
      `SELECT id,title,filename,mime_type,size_bytes,document_type,description,tags,
              valid_until,amount,entity_name,metadata_json,updated_at
       FROM company_documents
       WHERE tenant_id=$1 AND usable_for_applications=true
       ORDER BY updated_at DESC LIMIT 40`,
      [tenantId],
    ),
  ]);
  return {
    knowledge: knowledge.rows.map((row) => ({
      kind: row.kind,
      title: row.title,
      description: row.description,
      valueText: row.value_text,
      tags: row.tags ?? [],
      metadata: row.metadata_json ?? {},
      updatedAt: row.updated_at,
    })),
    documents: documents.rows.map((row) => ({
      sourceId: `company-document-${row.id}`,
      title: row.title,
      filename: row.filename,
      mimeType: row.mime_type,
      sizeBytes: row.size_bytes,
      documentType: row.document_type,
      description: row.description,
      tags: row.tags ?? [],
      validUntil: row.valid_until,
      amount: row.amount == null ? null : Number(row.amount),
      entityName: row.entity_name,
      metadata: row.metadata_json ?? {},
      updatedAt: row.updated_at,
    })),
  };
}
