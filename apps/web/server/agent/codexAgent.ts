import { spawn } from "node:child_process";
import crypto from "node:crypto";
import {
  lstat,
  mkdir,
  readdir,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import {
  LICITATION_EXPERT_SYSTEM_PROMPT,
  prepareLicitationAgentRun,
  validateLicitationAgentResult,
  type LicitationAgentInput,
  type LicitationAgentResult,
} from "./licitationAgent";
import {
  acquireCodexSessionLock,
  getCodexHotSession,
  releaseCodexSessionLock,
  setCodexHotSession,
  touchCodexHotSession,
} from "./codexSessionStore";
import { buildCodexArgs, codexSandboxMode } from "./codexCommand";
import { documentDeliveryInstruction } from "./documentDelivery";

type JsonObject = Record<string, unknown>;

export type CodexAgentStreamEvent =
  | { type: "status"; message: string }
  | { type: "codex_thread"; threadId: string; resumed: boolean }
  | { type: "codex_item"; itemType: string; text?: string }
  | {
      type: "done";
      result: LicitationAgentResult;
      usage: JsonObject;
      outputDirectory: string;
      generatedFiles: string[];
    }
  | { type: "error"; message: string };

function clean(value: unknown, max = 8_000) {
  return String(value ?? "")
    .replace(/[\u0000-\u001f]/g, " ")
    .trim()
    .slice(0, max);
}

function summarizeCodexCommand(item: JsonObject) {
  const command = clean(item.command, 2_000).toLowerCase();
  const output = clean(item.aggregated_output, 2_000).toLowerCase();
  const failed = item.status === "failed" || Number(item.exit_code) > 0;

  if (output.includes("could not resolve host")) {
    return "No se pudo resolver el host de SEACE.";
  }
  if (output.includes("pdf document")) {
    return "PDF descargado y validado.";
  }
  if (command.includes("pdftotext")) {
    return failed ? "No se pudo extraer texto del PDF." : "Texto del PDF extraído.";
  }
  if (command.includes("curl")) {
    return failed
      ? "No se pudo descargar el documento oficial."
      : "Documento oficial descargado.";
  }
  if (
    command.includes("file ") ||
    command.includes("pdfinfo") ||
    command.includes("ls -l")
  ) {
    return failed ? "Validación del archivo fallida." : "Validando archivo descargado.";
  }
  if (command.includes("context/current.json")) {
    return "Leyendo contexto de la licitación.";
  }
  return failed ? "Una herramienta del agente falló." : "Ejecutando herramienta del agente.";
}

function safePathPart(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80);
}

function hashJson(value: unknown) {
  return crypto
    .createHash("sha256")
    .update(JSON.stringify(value))
    .digest("hex");
}

const CODEX_WORKSPACE_POLICY_VERSION = 5;
const GENERATED_EXTENSIONS = new Set([".doc", ".docx", ".pdf", ".xls", ".xlsx"]);
const SOURCE_DOCUMENT_LIMIT = 12;
const SOURCE_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024;
const SOURCE_DOCUMENT_TOTAL_BYTES = 30 * 1024 * 1024;
const SOURCE_CACHE_TTL_MS = 30 * 60 * 1000;

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function array(value: unknown) {
  return Array.isArray(value) ? value : [];
}

function sourceDocumentFilename(sourceId: string, filename: string) {
  const extension = path.extname(filename).toLowerCase();
  const stem = path
    .basename(filename, extension)
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
  return `${safePathPart(sourceId)}-${stem || "documento"}${extension}`;
}

function validDocumentBytes(extension: string, content: Buffer) {
  if (extension === ".pdf") return content.subarray(0, 5).toString() === "%PDF-";
  if (extension === ".docx" || extension === ".xlsx") {
    return content[0] === 0x50 && content[1] === 0x4b;
  }
  if (extension === ".doc" || extension === ".xls") {
    return (
      content[0] === 0xd0 &&
      content[1] === 0xcf &&
      content[2] === 0x11 &&
      content[3] === 0xe0
    );
  }
  return false;
}

function officialSeaceUrl(value: string, base?: URL) {
  try {
    const url = base ? new URL(value, base) : new URL(value);
    const hostname = url.hostname.toLowerCase();
    if (
      url.protocol !== "https:" ||
      (hostname !== "seace.gob.pe" && !hostname.endsWith(".seace.gob.pe"))
    ) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

async function fetchOfficialDocument(value: string) {
  const initial = officialSeaceUrl(value);
  if (!initial) throw new Error("invalid SEACE URL");
  let current: URL = initial;
  for (let redirects = 0; redirects <= 3; redirects += 1) {
    const response: Response = await fetch(current, {
      redirect: "manual",
      signal: AbortSignal.timeout(35_000),
    });
    if (response.status >= 300 && response.status < 400) {
      const location: string | null = response.headers.get("location");
      const next: URL | null = location
        ? officialSeaceUrl(location, current)
        : null;
      if (!next) throw new Error("invalid SEACE redirect");
      current = next;
      continue;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response;
  }
  throw new Error("too many SEACE redirects");
}

async function cleanupSourceDocumentCache(directory: string) {
  const now = Date.now();
  const entries = await readdir(directory, { withFileTypes: true });
  await Promise.all(
    entries.map(async (entry) => {
      if (!entry.isFile()) return;
      const filePath = path.join(directory, entry.name);
      const file = await lstat(filePath);
      if (now - file.mtimeMs > SOURCE_CACHE_TTL_MS) {
        await unlink(filePath).catch(() => undefined);
      }
    }),
  );
}

async function materializeSourceDocuments(
  workspacePath: string,
  context: JsonObject,
) {
  const payload = JSON.parse(JSON.stringify(context)) as JsonObject;
  const sources = object(payload.sources);
  const documents = array(sources.documents).map((value) => object(value));
  const directory = path.join(workspacePath, "context", "source-documents");
  await mkdir(directory, { recursive: true });
  await cleanupSourceDocumentCache(directory);

  let totalBytes = 0;
  let materialized = 0;
  const localPathBySource = new Map<string, string>();
  for (const document of documents) {
    const sourceId = clean(document.sourceId, 120);
    const duplicateOf = clean(document.duplicateOf, 120);
    if (duplicateOf && localPathBySource.has(duplicateOf)) {
      document.localPath = localPathBySource.get(duplicateOf);
      document.localStatus = "duplicate";
      continue;
    }
    const filename = clean(document.filename, 255);
    const downloadUrl = clean(document.downloadUrl, 2_000);
    const expectedSize = Number(document.sizeBytes);
    const extension = path.extname(filename).toLowerCase();
    if (
      !sourceId ||
      !downloadUrl ||
      !GENERATED_EXTENSIONS.has(extension) ||
      !Number.isSafeInteger(expectedSize) ||
      expectedSize <= 0 ||
      expectedSize > SOURCE_DOCUMENT_MAX_BYTES ||
      totalBytes + expectedSize > SOURCE_DOCUMENT_TOTAL_BYTES ||
      materialized >= SOURCE_DOCUMENT_LIMIT
    ) {
      document.localStatus = "not-materialized";
      continue;
    }
    const localName = sourceDocumentFilename(sourceId, filename);
    const filePath = path.join(directory, localName);
    const relativePath = path.posix.join(
      "context",
      "source-documents",
      localName,
    );
    const cached = await stat(filePath).catch(() => null);
    if (cached?.isFile() && cached.size === expectedSize) {
      document.localPath = relativePath;
      document.localStatus = "available";
      localPathBySource.set(sourceId, relativePath);
      totalBytes += expectedSize;
      materialized += 1;
      continue;
    }
    try {
      const response = await fetchOfficialDocument(downloadUrl);
      const content = Buffer.from(await response.arrayBuffer());
      if (
        content.length <= 0 ||
        content.length > SOURCE_DOCUMENT_MAX_BYTES ||
        !validDocumentBytes(extension, content)
      ) {
        throw new Error("invalid document");
      }
      await writeFile(filePath, content);
      document.localPath = relativePath;
      document.localStatus = "available";
      localPathBySource.set(sourceId, relativePath);
      totalBytes += content.length;
      materialized += 1;
    } catch {
      document.localStatus = "download-failed";
    }
  }
  sources.documents = documents;
  payload.sources = sources;
  return payload;
}

async function generatedFileSnapshot(outputDirectory: string) {
  const snapshot = new Map<string, string>();
  const entries = await readdir(outputDirectory, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    const extension = path.extname(entry.name).toLowerCase();
    if (!GENERATED_EXTENSIONS.has(extension)) continue;
    const filePath = path.join(outputDirectory, entry.name);
    const file = await lstat(filePath);
    snapshot.set(entry.name, `${file.size}:${file.mtimeMs}`);
  }
  return snapshot;
}

async function temporaryFileSnapshot(directory: string) {
  const snapshot = new Map<string, string>();
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    const filePath = path.join(directory, entry.name);
    const file = await lstat(filePath);
    snapshot.set(entry.name, `${file.size}:${file.mtimeMs}`);
  }
  return snapshot;
}

async function cleanupTemporaryFiles(
  directory: string,
  filesBeforeRun: Map<string, string>,
) {
  const filesAfterRun = await temporaryFileSnapshot(directory);
  await Promise.all(
    [...filesAfterRun.entries()]
      .filter(([name, fingerprint]) => filesBeforeRun.get(name) !== fingerprint)
      .map(([name]) => unlink(path.join(directory, name)).catch(() => undefined)),
  );
}

function codexOutputJsonSchema() {
  return {
    type: "object",
    additionalProperties: false,
    required: ["answer", "citations", "proposedChanges"],
    properties: {
      answer: { type: "string" },
      citations: {
        type: "array",
        maxItems: 12,
        items: {
          type: "object",
          additionalProperties: false,
          required: ["sourceId", "label", "evidence"],
          properties: {
            sourceId: { type: "string" },
            label: { type: "string" },
            evidence: { type: "string" },
          },
        },
      },
      proposedChanges: {
        type: "object",
        additionalProperties: false,
        required: ["application", "items", "requirements"],
        properties: {
          application: {
            type: "object",
            additionalProperties: false,
            required: ["validity_date", "contact_email", "contact_phone"],
            properties: {
              validity_date: { type: ["string", "null"] },
              contact_email: { type: ["string", "null"] },
              contact_phone: { type: ["string", "null"] },
            },
          },
          items: {
            type: "array",
            items: {
              type: "object",
              additionalProperties: false,
              required: ["id", "selected", "unit_price"],
              properties: {
                id: { type: "integer" },
                selected: { type: ["boolean", "null"] },
                unit_price: { type: ["number", "null"], minimum: 0 },
              },
            },
          },
          requirements: {
            type: "array",
            items: {
              type: "object",
              additionalProperties: false,
              required: ["id", "offered_value"],
              properties: {
                id: { type: "integer" },
                offered_value: { type: "string" },
              },
            },
          },
        },
      },
    },
  };
}

async function ensureWorkspace(
  tenantId: string,
  chatSessionId: string,
  context: JsonObject,
) {
  const root =
    process.env.CODEX_AGENT_WORKSPACE_ROOT ??
    path.join(process.cwd(), ".codex-runtime");
  const workspacePath = path.join(
    root,
    "tenants",
    safePathPart(tenantId),
    "chats",
    safePathPart(chatSessionId),
  );
  await mkdir(path.join(workspacePath, "context"), { recursive: true });
  await mkdir(path.join(workspacePath, "outputs", "generated-documents"), {
    recursive: true,
  });
  await mkdir(path.join(workspacePath, "tmp", "downloads"), {
    recursive: true,
  });
  await mkdir(path.join(workspacePath, ".schemas"), { recursive: true });
  const materializedContext = await materializeSourceDocuments(
    workspacePath,
    context,
  );
  await writeFile(
    path.join(workspacePath, "AGENTS.md"),
    `# BuenaPro Codex Workspace

Eres el agente documental de BuenaPro para analizar licitaciones peruanas y preparar paquetes de postulacion revisables.

## Limites de seguridad

- Nunca envies, firmes ni presentes una propuesta en SEACE.
- Nunca apliques proposedChanges al borrador de PostgreSQL sin confirmacion humana.
- Editar una COPIA local de una plantilla Word cuando el usuario lo pide SI esta permitido. No confundas esta edicion documental con aplicar cambios en PostgreSQL o presentar una oferta.
- Usa solo context/current.json, los archivos tenant-scoped de context/source-documents/ y el mensaje del usuario.
- No busques secretos ni leas archivos fuera de este workspace.

## Flujo obligatorio para documentos

1. Lee sources.documentInventory y recorre TODOS los registros de sources.documents antes de decidir el alcance.
2. Deduplica usando duplicateOf. Explica la cantidad de documentos distintos, plantillas editables y documentos de referencia.
3. Un TDR/PDF de referencia se usa para extraer obligaciones; no se "rellena" salvo que el usuario pida expresamente modificarlo. Un archivo con editableTemplate=true es una plantilla candidata a completar.
4. Si el usuario pide preparar, completar o editar "el documento", "los documentos" o "la propuesta" sin nombrar una sola plantilla, el alcance predeterminado incluye TODAS las plantillas editables necesarias para la postulacion. No elijas solo la oferta economica.
5. Antes de editar, abre cada localPath disponible e inventaria sus campos, tablas, casillas, marcadores y secciones "NO LLENAR". Nunca completes una seccion marcada "NO LLENAR / NO REMITIR".
6. Contrasta cada campo con sources.companyProfile, sources.companyLibrary, applicationDraft y el TDR. Clasifica valores como acreditados, proporcionados por el usuario, inferidos o faltantes.
7. Si faltan datos necesarios, formula UNA sola pregunta consolidada y concreta con todos los campos faltantes agrupados por plantilla. No generes un paquete final incompleto, salvo que el usuario pida expresamente una version con PENDIENTES para revisar.
8. Cuando ya existan los datos o el usuario autorice PENDIENTES, copia cada plantilla original y realiza cambios minimos sobre esa copia. Conserva estructura, estilos, tablas, encabezados, pies, casillas y saltos. No recrees desde cero una plantilla descargada.
9. Para DOCX, inspecciona y modifica el paquete OOXML de forma conservadora. Puedes usar unzip/zip y XML; no reemplaces todo el documento cuando basta editar runs/celdas concretas.
10. Mantén acotada la salida de herramientas: nunca imprimas XML OOXML completo ni todo el texto de un PDF largo en la conversación. Usa scripts silenciosos para procesar archivos, limita inventarios con head/rg/sed y devuelve solo campos, coincidencias y resúmenes necesarios. Edita varias plantillas en una sola pasada cuando sea seguro.
11. Guarda cada entregable final directamente en outputs/generated-documents/, sin subcarpetas, con nombre descriptivo. Si hay tres plantillas editables, entrega tres archivos, no uno.
12. Valida cada DOCX con unzip -t y verifica que el texto esperado exista. Si LibreOffice/soffice esta disponible, renderiza y revisa todas las paginas; si no, declara que la QA fue estructural.
13. Antes de responder, compara inventario contra entregables. No afirmes que el paquete esta completo si falta una plantilla editable. Enumera claramente archivos generados y pendientes.

## Acceso a originales

- Prefiere localPath cuando localStatus sea available o duplicate.
- Si un original no fue materializado, puedes intentar downloadUrl con curl en tmp/downloads/ usando reintentos, validar tipo/tamano y extraer su contenido con una herramienta local segura.
- No afirmes lectura o preservacion de formato si no abriste el original real.
- Los documentos finales deben ser archivos reales y validos (.docx, .pdf o .xlsx), no texto con una extension cambiada.
`,
  );
  await writeFile(
    path.join(workspacePath, "memory.md"),
    `# Memoria estable del tenant

La memoria principal vive en PostgreSQL y en la biblioteca de empresa de BuenaPro. Este archivo existe solo como memoria caliente para sesiones Codex activas.
`,
  );
  await writeFile(
    path.join(workspacePath, "context", "current.json"),
    JSON.stringify(materializedContext, null, 2),
  );
  const schemaPath = path.join(
    workspacePath,
    ".schemas",
    "licitation-agent-output.schema.json",
  );
  await writeFile(schemaPath, JSON.stringify(codexOutputJsonSchema(), null, 2));
  return { workspacePath, schemaPath, materializedContext };
}

function initialPrompt(agentPrompt: string, input: LicitationAgentInput) {
  return `${LICITATION_EXPERT_SYSTEM_PROMPT}

El archivo context/current.json contiene el mismo contexto tenant-scoped del turno.

${agentPrompt}

${documentDeliveryInstruction(Boolean(input.documentDeliveryMode))}

Devuelve exclusivamente JSON valido para el esquema solicitado. No uses Markdown.`;
}

function continuationPrompt(input: LicitationAgentInput) {
  return `Continua la conversacion activa de BuenaPro.

MENSAJE ACTUAL DEL USUARIO:
${clean(input.userMessage)}

${documentDeliveryInstruction(Boolean(input.documentDeliveryMode))}

Usa la memoria de esta sesion, context/current.json y los guardrails ya definidos. Si el contexto actual cambió, el backend abrirá una sesion nueva. Devuelve exclusivamente JSON valido para el esquema solicitado.`;
}

function parseAgentJson(text: string): JsonObject {
  const trimmed = text.trim();
  try {
    return JSON.parse(trimmed) as JsonObject;
  } catch {
    const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
    if (fenced) return JSON.parse(fenced) as JsonObject;
    const start = trimmed.indexOf("{");
    const end = trimmed.lastIndexOf("}");
    if (start >= 0 && end > start) {
      return JSON.parse(trimmed.slice(start, end + 1)) as JsonObject;
    }
    throw new Error("Codex no devolvió JSON válido.");
  }
}

async function* spawnCodex(options: {
  prompt: string;
  schemaPath: string;
  workspacePath: string;
  threadId?: string | null;
}): AsyncGenerator<{
  type: "thread" | "item" | "done";
  threadId?: string;
  text?: string;
  itemType?: string;
  usage?: JsonObject;
  error?: string;
}> {
  const child = spawn("codex", buildCodexArgs(options), {
    cwd: options.workspacePath,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  let buffer = "";
  const events: Array<any> = [];
  let notify: (() => void) | null = null;
  let closed = false;
  let exitCode: number | null = null;

  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    stdout += chunk;
    buffer += chunk;
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        events.push(JSON.parse(line));
      } catch {
        events.push({ type: "raw", text: line });
      }
    }
    notify?.();
  });
  child.stderr.on("data", (chunk: string) => {
    stderr += chunk;
  });
  child.on("close", (code) => {
    exitCode = code;
    closed = true;
    if (buffer.trim()) {
      try {
        events.push(JSON.parse(buffer));
      } catch {
        events.push({ type: "raw", text: buffer });
      }
    }
    notify?.();
  });

  while (!closed || events.length) {
    if (!events.length) {
      await new Promise<void>((resolve) => {
        notify = resolve;
      });
      notify = null;
      continue;
    }
    const event = events.shift();
    if (event.type === "thread.started") {
      yield { type: "thread", threadId: String(event.thread_id ?? "") };
    } else if (event.type === "item.completed") {
      const item = event.item ?? {};
      const itemType = String(item.type ?? "item");
      yield {
        type: "item",
        itemType,
        text:
          typeof item.text === "string"
            ? item.text
            : itemType === "command_execution"
              ? summarizeCodexCommand(item)
              : undefined,
      };
    } else if (event.type === "turn.completed") {
      yield { type: "done", usage: event.usage ?? {} };
    } else if (event.type === "error" || event.type === "turn.failed") {
      const message =
        typeof event.message === "string"
          ? event.message
          : typeof event.error?.message === "string"
            ? event.error.message
            : JSON.stringify(event);
      yield { type: "done", error: clean(message, 2_000) };
    }
  }
  if (exitCode !== 0) {
    throw new Error(
      clean(stderr || stdout || `Codex terminó con código ${exitCode}.`, 2_000),
    );
  }
}

export async function* streamCodexLicitationAgent(
  chatSessionId: string,
  input: LicitationAgentInput,
): AsyncGenerator<CodexAgentStreamEvent> {
  const prepared = await prepareLicitationAgentRun(input);
  if (!prepared) throw new Error("No se encontró el expediente de la licitación.");
  const outputSchema = codexOutputJsonSchema();
  const { workspacePath, schemaPath, materializedContext } = await ensureWorkspace(
    input.tenantId,
    chatSessionId,
    prepared.context.payload,
  );
  const contextHash = hashJson({
    context: materializedContext,
    systemPrompt: LICITATION_EXPERT_SYSTEM_PROMPT,
    workspacePolicyVersion: CODEX_WORKSPACE_POLICY_VERSION,
    sandboxMode: codexSandboxMode(),
    outputSchema,
  });
  const outputDirectory = path.join(
    workspacePath,
    "outputs",
    "generated-documents",
  );
  const filesBeforeRun = await generatedFileSnapshot(outputDirectory);
  const temporaryDirectory = path.join(workspacePath, "tmp", "downloads");
  const temporaryFilesBeforeRun = await temporaryFileSnapshot(
    temporaryDirectory,
  );
  const locked = await acquireCodexSessionLock(input.tenantId, chatSessionId);
  if (!locked) {
    throw new Error("Esta conversación ya está procesando otro mensaje.");
  }
  try {
    const hot = await getCodexHotSession(input.tenantId, chatSessionId);
    const canResume =
      hot?.codexThreadId &&
      hot.contextHash === contextHash &&
      hot.workspacePath === workspacePath;
    const prompt = canResume
      ? continuationPrompt(input)
      : initialPrompt(prepared.prompt, input);
    let threadId = canResume ? hot.codexThreadId : null;
    let assistantText = "";
    let usage: JsonObject = {};
    yield {
      type: "status",
      message: canResume
        ? "Reanudando sesión Codex caliente."
        : "Iniciando sesión Codex con contexto completo.",
    };
    for await (const event of spawnCodex({
      prompt,
      schemaPath,
      workspacePath,
      threadId,
    })) {
      if (event.type === "thread" && event.threadId) {
        threadId = event.threadId;
        yield { type: "codex_thread", threadId, resumed: Boolean(canResume) };
      } else if (event.type === "item") {
        if (event.itemType === "agent_message" && event.text) {
          assistantText = event.text;
        }
        yield {
          type: "codex_item",
          itemType: event.itemType ?? "item",
          text: event.text,
        };
      } else if (event.type === "done") {
        if (event.error) throw new Error(event.error);
        usage = event.usage ?? {};
      }
    }
    if (!assistantText) throw new Error("Codex no devolvió una respuesta.");
    const parsed = parseAgentJson(assistantText);
    const result = validateLicitationAgentResult(
      parsed,
      prepared.context,
      process.env.CODEX_AGENT_MODEL ?? "codex-cli",
    );
    if (threadId) {
      await setCodexHotSession({
        status: "active",
        tenantId: input.tenantId,
        chatSessionId,
        codexThreadId: threadId,
        workspacePath,
        contextHash,
        lastActivityAt: new Date().toISOString(),
      });
    } else {
      await touchCodexHotSession(input.tenantId, chatSessionId);
    }
    const filesAfterRun = await generatedFileSnapshot(outputDirectory);
    const generatedFiles = [...filesAfterRun.entries()]
      .filter(([name, fingerprint]) => filesBeforeRun.get(name) !== fingerprint)
      .map(([name]) => path.join(outputDirectory, name));
    yield {
      type: "done",
      result,
      usage,
      outputDirectory,
      generatedFiles,
    };
  } catch (error) {
    yield {
      type: "error",
      message:
        error instanceof Error
          ? error.message
          : "Codex no pudo responder en este momento.",
    };
  } finally {
    await cleanupTemporaryFiles(
      temporaryDirectory,
      temporaryFilesBeforeRun,
    ).catch(() => undefined);
    await releaseCodexSessionLock(input.tenantId, chatSessionId);
  }
}
