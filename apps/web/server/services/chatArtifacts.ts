import { lstat, readFile, realpath, unlink } from "node:fs/promises";
import path from "node:path";
import type { PoolClient } from "pg";
import { query } from "@/server/db/client";

const MAX_ARTIFACT_BYTES = 15 * 1024 * 1024;
const MIME_BY_EXTENSION: Record<string, string> = {
  ".doc": "application/msword",
  ".docx":
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".pdf": "application/pdf",
  ".xls": "application/vnd.ms-excel",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

export type GeneratedArtifactInput = {
  filename: string;
  mimeType: string;
  sizeBytes: number;
  content: Buffer;
};

export type ChatArtifact = {
  id: string;
  name: string;
  mime: string;
  sizeBytes: number;
  createdAt: Date | string;
  downloadUrl: string;
};

function artifactView(row: {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  created_at: Date | string;
}): ChatArtifact {
  return {
    id: row.id,
    name: row.filename,
    mime: row.mime_type,
    sizeBytes: row.size_bytes,
    createdAt: row.created_at,
    downloadUrl: `/api/chat/artifacts/${row.id}`,
  };
}

export async function loadGeneratedArtifacts(
  outputDirectory: string,
  generatedFiles: string[],
) {
  const safeRoot = await realpath(outputDirectory);
  const artifacts: GeneratedArtifactInput[] = [];

  for (const candidate of generatedFiles.slice(0, 8)) {
    const resolved = await realpath(candidate).catch(() => null);
    if (!resolved || path.dirname(resolved) !== safeRoot) continue;
    const extension = path.extname(resolved).toLowerCase();
    const mimeType = MIME_BY_EXTENSION[extension];
    if (!mimeType) continue;
    const file = await lstat(resolved);
    if (!file.isFile() || file.size <= 0 || file.size > MAX_ARTIFACT_BYTES)
      continue;
    const filename = path.basename(resolved).slice(0, 255);
    artifacts.push({
      filename,
      mimeType,
      sizeBytes: file.size,
      content: await readFile(resolved),
    });
  }

  return artifacts;
}

export async function cleanupGeneratedArtifacts(
  outputDirectory: string,
  generatedFiles: string[],
) {
  const safeRoot = await realpath(outputDirectory);
  await Promise.all(
    generatedFiles.map(async (candidate) => {
      const resolved = await realpath(candidate).catch(() => null);
      if (!resolved || path.dirname(resolved) !== safeRoot) return;
      await unlink(resolved).catch(() => undefined);
    }),
  );
}

export async function insertChatArtifacts(
  client: PoolClient,
  input: {
    tenantId: string;
    sessionId: string;
    messageId: string;
    runId: string;
    artifacts: GeneratedArtifactInput[];
  },
) {
  const created: ChatArtifact[] = [];
  for (const artifact of input.artifacts) {
    const result = await client.query<{
      id: string;
      filename: string;
      mime_type: string;
      size_bytes: number;
      created_at: Date;
    }>(
      `INSERT INTO chat_artifacts
         (tenant_id,session_id,message_id,run_id,filename,mime_type,size_bytes,content)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
       RETURNING id,filename,mime_type,size_bytes,created_at`,
      [
        input.tenantId,
        input.sessionId,
        input.messageId,
        input.runId,
        artifact.filename,
        artifact.mimeType,
        artifact.sizeBytes,
        artifact.content,
      ],
    );
    created.push(artifactView(result.rows[0]));
  }
  return created;
}

export async function getChatArtifact(tenantId: string, artifactId: string) {
  const result = await query<{
    filename: string;
    mime_type: string;
    size_bytes: number;
    content: Buffer;
  }>(
    `SELECT ca.filename,ca.mime_type,ca.size_bytes,ca.content
     FROM chat_artifacts ca
     JOIN chat_sessions cs ON cs.id=ca.session_id
     WHERE ca.tenant_id=$1 AND cs.tenant_id=$1 AND ca.id=$2`,
    [tenantId, artifactId],
  );
  return result.rows[0] ?? null;
}
