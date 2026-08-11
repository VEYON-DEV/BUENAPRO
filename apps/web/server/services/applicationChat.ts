import { pool, query } from "@/server/db/client";
import {
  insertChatArtifacts,
  type GeneratedArtifactInput,
} from "@/server/services/chatArtifacts";

type JsonObject = Record<string, unknown>;

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function text(value: unknown, max: number) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized ? normalized.slice(0, max) : null;
}

export async function listChatSessions(tenantId: string, idContrato: number) {
  const result = await query(
    `SELECT cs.id,cs.id_contrato,cs.match_id,cs.application_id,cs.title,cs.summary,
            cs.created_by,cs.created_at,cs.updated_at,
            COALESCE((SELECT count(*)::int FROM chat_messages cm WHERE cm.session_id=cs.id),0) AS message_count
     FROM chat_sessions cs
     WHERE cs.tenant_id=$1 AND cs.id_contrato=$2
     ORDER BY cs.updated_at DESC,cs.created_at DESC`,
    [tenantId, idContrato],
  );
  return result.rows.map(mapSession);
}

export async function createChatSession(
  tenantId: string,
  idContrato: number,
  actorId: string | null,
  title?: unknown,
) {
  // Vincula el match/borrador cuando ya existen; el chat también puede comenzar
  // desde el detalle de una oportunidad antes de crear la postulación.
  const result = await query(
    `INSERT INTO chat_sessions
       (tenant_id,id_contrato,match_id,application_id,title,created_by)
     SELECT $1,$2,m.id,ad.id,$3,$4
     FROM seace_contracts c
     LEFT JOIN company_profiles cp ON cp.tenant_id=$1
     LEFT JOIN matches m ON m.id_contrato=c.id_contrato AND m.profile_id=cp.id
     LEFT JOIN application_drafts ad ON ad.match_id=m.id
     WHERE c.id_contrato=$2
     ORDER BY cp.created_at,m.created_at
     LIMIT 1
     RETURNING *`,
    [tenantId, idContrato, text(title, 120) ?? "Nueva conversación", actorId],
  );
  return result.rows[0] ? mapSession(result.rows[0]) : null;
}

export async function getChatSession(tenantId: string, sessionId: string) {
  const result = await query(
    `SELECT cs.*,
      COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'id',cm.id,'role',cm.role,'content',cm.content,
        'citations',cm.citations_json,'metadata',cm.metadata_json,
        'createdBy',cm.created_by,'createdAt',cm.created_at,
        'artifacts',COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'id',ca.id,'name',ca.filename,'mime',ca.mime_type,
          'sizeBytes',ca.size_bytes,'createdAt',ca.created_at,
          'downloadUrl','/api/chat/artifacts/' || ca.id
        ) ORDER BY ca.created_at,ca.id) FROM chat_artifacts ca
        WHERE ca.message_id=cm.id),'[]'::jsonb),
        'changeSet',(SELECT jsonb_build_object(
          'id',acs.id,'runId',acs.run_id,'changes',acs.changes_json,
          'status',acs.status,'confirmedBy',acs.confirmed_by,
          'confirmedAt',acs.confirmed_at,'rejectedBy',acs.rejected_by,
          'rejectedAt',acs.rejected_at,'createdAt',acs.created_at
        ) FROM agent_change_sets acs
        WHERE acs.id=(cm.metadata_json->>'changeSetId')::uuid)
      ) ORDER BY cm.created_at,cm.id) FROM chat_messages cm WHERE cm.session_id=cs.id),'[]') AS messages,
      COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'id',acs.id,'runId',acs.run_id,'changes',acs.changes_json,
        'status',acs.status,'confirmedBy',acs.confirmed_by,
        'confirmedAt',acs.confirmed_at,'rejectedBy',acs.rejected_by,
        'rejectedAt',acs.rejected_at,'createdAt',acs.created_at
      ) ORDER BY acs.created_at)
      FROM agent_change_sets acs JOIN agent_runs ar ON ar.id=acs.run_id
      WHERE ar.session_id=cs.id),'[]') AS change_sets
     FROM chat_sessions cs WHERE cs.tenant_id=$1 AND cs.id=$2`,
    [tenantId, sessionId],
  );
  const row = result.rows[0];
  if (!row) return null;
  return {
    ...mapSession(row),
    messages: row.messages,
    changeSets: row.change_sets,
  };
}

export async function listChatMessages(tenantId: string, sessionId: string) {
  const result = await query(
    `SELECT cm.id,cm.role,cm.content,cm.citations_json,cm.metadata_json,
            cm.created_by,cm.created_at
     FROM chat_messages cm
     JOIN chat_sessions cs ON cs.id=cm.session_id
     WHERE cs.tenant_id=$1 AND cs.id=$2
     ORDER BY cm.created_at,cm.id`,
    [tenantId, sessionId],
  );
  return result.rows;
}

export async function createUserChatMessage(
  tenantId: string,
  sessionId: string,
  actorId: string,
  content: unknown,
) {
  const normalized = text(content, 20_000);
  if (!normalized) return null;
  const result = await query(
    `WITH inserted AS (
       INSERT INTO chat_messages (session_id,role,content,created_by)
       SELECT cs.id,'user',$3,$4 FROM chat_sessions cs
       WHERE cs.tenant_id=$1 AND cs.id=$2
       RETURNING *
     ), touched AS (
       UPDATE chat_sessions SET updated_at=now()
       WHERE id=(SELECT session_id FROM inserted) RETURNING id
     )
     SELECT * FROM inserted`,
    [tenantId, sessionId, normalized, actorId],
  );
  return result.rows[0] ?? null;
}

export async function beginAgentRun(
  tenantId: string,
  sessionId: string,
  actorId: string,
  content: unknown,
  model?: string | null,
) {
  const normalized = text(content, 20_000);
  if (!normalized) return null;
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const session = await client.query<{ id: string }>(
      "SELECT id FROM chat_sessions WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
      [tenantId, sessionId],
    );
    if (!session.rows[0]) {
      await client.query("ROLLBACK");
      return null;
    }
    const message = await client.query(
      `INSERT INTO chat_messages (session_id,role,content,created_by)
       VALUES ($1,'user',$2,$3) RETURNING *`,
      [sessionId, normalized, actorId],
    );
    const run = await client.query(
      `INSERT INTO agent_runs
       (session_id,user_message_id,status,model,started_at)
       VALUES ($1,$2,'running',$3,now()) RETURNING *`,
      [sessionId, message.rows[0].id, text(model, 120)],
    );
    await client.query(
      "UPDATE chat_sessions SET updated_at=now() WHERE id=$1",
      [sessionId],
    );
    await client.query("COMMIT");
    return { message: message.rows[0], run: run.rows[0] };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function completeAgentRun(
  tenantId: string,
  runId: string,
  input: {
    content: unknown;
    citations?: unknown;
    metadata?: unknown;
    usage?: unknown;
    changes?: unknown;
    artifacts?: GeneratedArtifactInput[];
  },
) {
  const content = text(input.content, 40_000);
  if (!content) throw new Error("EMPTY_ASSISTANT_MESSAGE");
  const changes =
    input.changes == null ? null : normalizeAgentChanges(input.changes);
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const run = await client.query<{ session_id: string; status: string }>(
      `SELECT ar.session_id,ar.status FROM agent_runs ar
       JOIN chat_sessions cs ON cs.id=ar.session_id
       WHERE cs.tenant_id=$1 AND ar.id=$2 FOR UPDATE OF ar`,
      [tenantId, runId],
    );
    const current = run.rows[0];
    if (!current) {
      await client.query("ROLLBACK");
      return null;
    }
    if (current.status !== "running" && current.status !== "pending")
      throw new Error("AGENT_RUN_ALREADY_FINISHED");
    const message = await client.query(
      `INSERT INTO chat_messages
       (session_id,role,content,citations_json,metadata_json)
       VALUES ($1,'assistant',$2,$3::jsonb,$4::jsonb) RETURNING *`,
      [
        current.session_id,
        content,
        JSON.stringify(Array.isArray(input.citations) ? input.citations : []),
        JSON.stringify(object(input.metadata)),
      ],
    );
    let changeSet = null;
    if (changes && hasAgentChanges(changes)) {
      const created = await client.query(
        `INSERT INTO agent_change_sets (run_id,changes_json)
         VALUES ($1,$2::jsonb) RETURNING *`,
        [runId, JSON.stringify(changes)],
      );
      changeSet = created.rows[0];
    }
    const linkedMetadata = {
      ...object(input.metadata),
      agentRunId: runId,
      changeSetId: changeSet?.id ?? null,
    };
    await client.query(
      "UPDATE chat_messages SET metadata_json=$2::jsonb WHERE id=$1",
      [message.rows[0].id, JSON.stringify(linkedMetadata)],
    );
    message.rows[0].metadata_json = linkedMetadata;
    const artifacts = await insertChatArtifacts(client, {
      tenantId,
      sessionId: current.session_id,
      messageId: message.rows[0].id,
      runId,
      artifacts: input.artifacts ?? [],
    });
    message.rows[0].artifacts = artifacts;
    await client.query(
      `UPDATE agent_runs SET status='completed',usage_json=$2::jsonb,
       completed_at=now() WHERE id=$1`,
      [runId, JSON.stringify(object(input.usage))],
    );
    await client.query(
      "UPDATE chat_sessions SET updated_at=now() WHERE id=$1",
      [current.session_id],
    );
    await client.query("COMMIT");
    return { message: message.rows[0], changeSet };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function failAgentRun(
  tenantId: string,
  runId: string,
  error: unknown,
) {
  const result = await query(
    `UPDATE agent_runs ar SET status='failed',error=$3,completed_at=now()
     FROM chat_sessions cs
     WHERE ar.session_id=cs.id AND cs.tenant_id=$1 AND ar.id=$2
       AND ar.status IN ('pending','running')
     RETURNING ar.*`,
    [
      tenantId,
      runId,
      text(error instanceof Error ? error.message : error, 2_000),
    ],
  );
  return result.rows[0] ?? null;
}

export async function updateChatSummary(
  tenantId: string,
  sessionId: string,
  summary: unknown,
) {
  const normalized = summary == null ? null : text(summary, 12_000);
  const result = await query(
    `UPDATE chat_sessions SET summary=$3,updated_at=now()
     WHERE tenant_id=$1 AND id=$2 RETURNING id,summary,updated_at`,
    [tenantId, sessionId, normalized],
  );
  return result.rows[0] ?? null;
}

type ChangeDecision = "confirm" | "reject";

export async function decideAgentChangeSet(
  tenantId: string,
  changeSetId: string,
  actorId: string,
  decision: ChangeDecision,
) {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const found = await client.query<{
      id: string;
      status: string;
      changes_json: JsonObject;
      match_id: string | null;
      application_id: string | null;
    }>(
      `SELECT acs.id,acs.status,acs.changes_json,cs.match_id,cs.application_id
       FROM agent_change_sets acs
       JOIN agent_runs ar ON ar.id=acs.run_id
       JOIN chat_sessions cs ON cs.id=ar.session_id
       WHERE cs.tenant_id=$1 AND acs.id=$2
       FOR UPDATE OF acs`,
      [tenantId, changeSetId],
    );
    const row = found.rows[0];
    if (!row) {
      await client.query("ROLLBACK");
      return { kind: "not_found" as const };
    }
    if (row.status !== "pending") {
      await client.query("ROLLBACK");
      return { kind: "already_decided" as const, status: row.status };
    }

    if (decision === "reject") {
      const rejected = await client.query(
        `UPDATE agent_change_sets SET status='rejected',rejected_by=$2,
           rejected_at=now(),updated_at=now() WHERE id=$1 RETURNING *`,
        [changeSetId, actorId],
      );
      await client.query("COMMIT");
      return { kind: "ok" as const, data: rejected.rows[0] };
    }

    if (!row.match_id || !row.application_id) {
      await client.query("ROLLBACK");
      return { kind: "application_required" as const };
    }

    const changes = object(row.changes_json);
    await applyDraftFields(client, tenantId, row.match_id, changes.application);
    await applyItemChanges(client, tenantId, row.match_id, changes.items);
    await applyRequirementChanges(
      client,
      tenantId,
      row.match_id,
      changes.requirements,
    );
    const applied = await client.query(
      `UPDATE agent_change_sets SET status='applied',confirmed_by=$2,
         confirmed_at=now(),updated_at=now() WHERE id=$1 RETURNING *`,
      [changeSetId, actorId],
    );
    await client.query(
      `INSERT INTO match_events (match_id,event_type,payload,actor_id)
       VALUES ($1,'agent_changes_confirmed',$2::jsonb,$3)`,
      [
        row.match_id,
        JSON.stringify({ change_set_id: changeSetId, changes }),
        actorId,
      ],
    );
    await client.query("COMMIT");
    return { kind: "ok" as const, data: applied.rows[0] };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

async function applyDraftFields(
  client: import("pg").PoolClient,
  tenantId: string,
  matchId: string,
  raw: unknown,
) {
  if (raw == null) return;
  const value = object(raw);
  const permitted = new Set([
    "validity_date",
    "contact_email",
    "contact_phone",
  ]);
  rejectUnknownKeys(value, permitted, "application");
  await client.query(
    `UPDATE application_drafts ad SET
       validity_date=CASE WHEN $3::boolean THEN $4::date ELSE validity_date END,
       contact_email=CASE WHEN $5::boolean THEN $6 ELSE contact_email END,
       contact_phone=CASE WHEN $7::boolean THEN $8 ELSE contact_phone END,
       status='draft',updated_at=now()
     FROM matches m JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE ad.match_id=m.id AND cp.tenant_id=$1 AND m.id=$2`,
    [
      tenantId,
      matchId,
      "validity_date" in value,
      nullableText(value.validity_date, 10),
      "contact_email" in value,
      nullableText(value.contact_email, 320),
      "contact_phone" in value,
      nullableText(value.contact_phone, 40),
    ],
  );
}

async function applyItemChanges(
  client: import("pg").PoolClient,
  tenantId: string,
  matchId: string,
  raw: unknown,
) {
  if (raw == null) return;
  if (!Array.isArray(raw)) throw new Error("INVALID_CHANGE_SET_ITEMS");
  for (const entry of raw) {
    const value = object(entry);
    rejectUnknownKeys(value, new Set(["id", "selected", "unit_price"]), "item");
    const id = positiveInteger(value.id);
    const selected =
      typeof value.selected === "boolean" ? value.selected : null;
    const unitPrice = nonNegativeNumber(value.unit_price);
    const updated = await client.query(
      `UPDATE application_items ai SET
         selected=COALESCE($4,selected),unit_price=COALESCE($5,unit_price),
         total_price=CASE WHEN $5::numeric IS NOT NULL
           THEN ROUND(COALESCE(quantity,1)*$5::numeric,2) ELSE total_price END,
         updated_at=now()
       FROM application_drafts ad
       JOIN matches m ON m.id=ad.match_id
       JOIN company_profiles cp ON cp.id=m.profile_id
       WHERE ai.application_id=ad.id AND cp.tenant_id=$1 AND m.id=$2 AND ai.id=$3
       RETURNING ai.id`,
      [tenantId, matchId, id, selected, unitPrice],
    );
    if (!updated.rows[0]) throw new Error("CHANGE_SET_ITEM_NOT_FOUND");
  }
  await client.query(
    `UPDATE application_drafts ad SET total_amount=(
       SELECT COALESCE(SUM(ai.total_price),0) FROM application_items ai
       WHERE ai.application_id=ad.id AND ai.selected=true
     ),status='draft',updated_at=now()
     FROM matches m JOIN company_profiles cp ON cp.id=m.profile_id
     WHERE ad.match_id=m.id AND cp.tenant_id=$1 AND m.id=$2`,
    [tenantId, matchId],
  );
}

async function applyRequirementChanges(
  client: import("pg").PoolClient,
  tenantId: string,
  matchId: string,
  raw: unknown,
) {
  if (raw == null) return;
  if (!Array.isArray(raw)) throw new Error("INVALID_CHANGE_SET_REQUIREMENTS");
  for (const entry of raw) {
    const value = object(entry);
    rejectUnknownKeys(value, new Set(["id", "offered_value"]), "requirement");
    const id = positiveInteger(value.id);
    const offered = nullableText(value.offered_value, 10_000);
    const updated = await client.query(
      `UPDATE application_requirements ar SET offered_value=$4,updated_at=now()
       FROM application_drafts ad
       JOIN matches m ON m.id=ad.match_id
       JOIN company_profiles cp ON cp.id=m.profile_id
       WHERE ar.application_id=ad.id AND cp.tenant_id=$1 AND m.id=$2 AND ar.id=$3
       RETURNING ar.id`,
      [tenantId, matchId, id, offered],
    );
    if (!updated.rows[0]) throw new Error("CHANGE_SET_REQUIREMENT_NOT_FOUND");
  }
}

function rejectUnknownKeys(
  value: JsonObject,
  permitted: Set<string>,
  section: string,
) {
  if (Object.keys(value).some((key) => !permitted.has(key))) {
    throw new Error(`INVALID_CHANGE_SET_${section.toUpperCase()}_FIELD`);
  }
}

function nullableText(value: unknown, max: number): string | null {
  if (value == null || value === "") return null;
  if (typeof value !== "string" || value.length > max)
    throw new Error("INVALID_CHANGE_SET_TEXT");
  return value.trim() || null;
}

function positiveInteger(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0)
    throw new Error("INVALID_CHANGE_SET_ID");
  return parsed;
}

function nonNegativeNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 999_999_999_999)
    throw new Error("INVALID_CHANGE_SET_PRICE");
  return parsed;
}

export type NormalizedAgentChanges = {
  application: {
    validity_date?: string | null;
    contact_email?: string | null;
    contact_phone?: string | null;
  };
  items: Array<{ id: number; selected?: boolean; unit_price?: number | null }>;
  requirements: Array<{ id: number; offered_value: string | null }>;
};

export function normalizeAgentChanges(raw: unknown): NormalizedAgentChanges {
  const value = object(raw);
  rejectUnknownKeys(
    value,
    new Set(["application", "items", "requirements"]),
    "root",
  );
  const applicationRaw = object(value.application);
  rejectUnknownKeys(
    applicationRaw,
    new Set(["validity_date", "contact_email", "contact_phone"]),
    "application",
  );
  const application: NormalizedAgentChanges["application"] = {};
  if ("validity_date" in applicationRaw)
    application.validity_date = nullableText(applicationRaw.validity_date, 10);
  if ("contact_email" in applicationRaw)
    application.contact_email = nullableText(applicationRaw.contact_email, 320);
  if ("contact_phone" in applicationRaw)
    application.contact_phone = nullableText(applicationRaw.contact_phone, 40);

  if (value.items != null && !Array.isArray(value.items))
    throw new Error("INVALID_CHANGE_SET_ITEMS");
  const items = (Array.isArray(value.items) ? value.items : []).map((entry) => {
    const item = object(entry);
    rejectUnknownKeys(item, new Set(["id", "selected", "unit_price"]), "item");
    const normalized: NormalizedAgentChanges["items"][number] = {
      id: positiveInteger(item.id),
    };
    if ("selected" in item) {
      if (typeof item.selected !== "boolean")
        throw new Error("INVALID_CHANGE_SET_SELECTED");
      normalized.selected = item.selected;
    }
    if ("unit_price" in item)
      normalized.unit_price = nonNegativeNumber(item.unit_price);
    return normalized;
  });

  if (value.requirements != null && !Array.isArray(value.requirements))
    throw new Error("INVALID_CHANGE_SET_REQUIREMENTS");
  const requirements = (
    Array.isArray(value.requirements) ? value.requirements : []
  ).map((entry) => {
    const requirement = object(entry);
    rejectUnknownKeys(
      requirement,
      new Set(["id", "offered_value"]),
      "requirement",
    );
    return {
      id: positiveInteger(requirement.id),
      offered_value: nullableText(requirement.offered_value, 10_000),
    };
  });
  return { application, items, requirements };
}

function hasAgentChanges(changes: NormalizedAgentChanges) {
  return (
    Object.keys(changes.application).length > 0 ||
    changes.items.length > 0 ||
    changes.requirements.length > 0
  );
}

function mapSession(row: Record<string, unknown>) {
  return {
    id: row.id,
    contractId: row.id_contrato,
    matchId: row.match_id,
    applicationId: row.application_id,
    title: row.title,
    summary: row.summary,
    createdBy: row.created_by,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    ...(row.message_count == null ? {} : { messageCount: row.message_count }),
  };
}
